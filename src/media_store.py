"""Content-addressed local assets, versioned analyses and cross-message reuse."""
from __future__ import annotations

from datetime import datetime,timezone,timedelta
import hashlib
import json
import re
import time
import uuid
from pathlib import Path

TZ=timezone(timedelta(hours=8))


def init_schema(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS media_assets (
            hash TEXT PRIMARY KEY,mime TEXT NOT NULL,bytes INTEGER NOT NULL,
            original_path TEXT NOT NULL,input_path TEXT NOT NULL,
            animated INTEGER NOT NULL DEFAULT 0,active_analysis TEXT
        );
        CREATE TABLE IF NOT EXISTS media_links (
            message_id TEXT PRIMARY KEY REFERENCES messages(id),
            asset TEXT NOT NULL REFERENCES media_assets(hash),category TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS media_links_asset ON media_links(asset);
        CREATE TABLE IF NOT EXISTS media_sources (
            account TEXT NOT NULL,category TEXT NOT NULL,md5 TEXT NOT NULL,
            asset TEXT NOT NULL REFERENCES media_assets(hash),PRIMARY KEY(account,category,md5)
        );
        CREATE TABLE IF NOT EXISTS media_cipher_sources (
            source_hash TEXT PRIMARY KEY,asset TEXT NOT NULL REFERENCES media_assets(hash)
        );
        CREATE TABLE IF NOT EXISTS media_cdn_sources (
            account TEXT NOT NULL,request_key TEXT NOT NULL,asset TEXT NOT NULL REFERENCES media_assets(hash),
            PRIMARY KEY(account,request_key)
        );
        CREATE TABLE IF NOT EXISTS media_analyses (
            id TEXT PRIMARY KEY,asset TEXT NOT NULL REFERENCES media_assets(hash),
            model TEXT NOT NULL,signature TEXT NOT NULL,data TEXT NOT NULL,text TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS media_jobs (
            asset TEXT PRIMARY KEY REFERENCES media_assets(hash),owner TEXT NOT NULL,expires REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS media_reprocess (
            asset TEXT PRIMARY KEY REFERENCES media_assets(hash),requested_at TEXT NOT NULL
        );
    """)


def atomic_bytes(path, data):
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name(path.name+"."+uuid.uuid4().hex+".tmp")
    try:
        tmp.write_bytes(data)
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def analysis_text(data):
    lines=[]
    labels={"summary":"描述","visible_text":"可见文字","numbers_dates":"数字与日期",
        "tone":"可能语气（推测）","keywords":"关键词","uncertainty":"不确定内容"}
    for key,label in labels.items():
        value=data.get(key)
        if isinstance(value,list):
            value="；".join(str(x) for x in value)
        if value:
            lines.append(label+"："+str(value))
    return "\n".join(lines)


class MediaStore:
    def __init__(self, store):
        self.store=store
        self.root=store.path.parent

    def get(self, digest):
        with self.store.connect() as conn:
            row=conn.execute("SELECT * FROM media_assets WHERE hash=?",(digest,)).fetchone()
        return dict(row) if row else None

    def for_message(self, uid):
        with self.store.connect() as conn:
            row=conn.execute("""SELECT a.*,l.category FROM media_links l JOIN media_assets a ON a.hash=l.asset
                WHERE l.message_id=?""",(uid,)).fetchone()
        return dict(row) if row else None

    def by_md5(self, account, category, md5):
        if not re.fullmatch(r"[0-9a-fA-F]{32}",md5 or ""):
            return None
        with self.store.connect() as conn:
            row=conn.execute("""SELECT a.* FROM media_sources s JOIN media_assets a ON a.hash=s.asset
                WHERE s.account=? AND s.category=? AND s.md5=?""",(account,category,md5.lower())).fetchone()
        return dict(row) if row else None

    def by_source_hash(self, digest):
        with self.store.connect() as conn:
            row=conn.execute("""SELECT a.* FROM media_cipher_sources s JOIN media_assets a ON a.hash=s.asset
                WHERE s.source_hash=?""",(digest,)).fetchone()
        return dict(row) if row else None

    def by_cdn(self,account,key):
        with self.store.connect() as conn:
            row=conn.execute("""SELECT a.* FROM media_cdn_sources s JOIN media_assets a ON a.hash=s.asset
                WHERE s.account=? AND s.request_key=?""",(account,key)).fetchone()
        return dict(row) if row else None

    def remember_cdn(self,account,key,asset):
        with self.store.connect() as conn:
            conn.execute("INSERT OR REPLACE INTO media_cdn_sources VALUES(?,?,?)",(account,key,asset["hash"]))

    def register(self, raw, prepared, mime, *, animated=False, source_bytes=None):
        digest=hashlib.sha256(raw).hexdigest()
        ext={"image/png":".png","image/jpeg":".jpg","image/gif":".gif","image/webp":".webp"}.get(mime,".bin")
        folder=self.root/"assets"/digest
        original=folder/"original.bin"
        input_path=folder/("input"+ext)
        if not original.exists():
            atomic_bytes(original,raw)
        if prepared and not input_path.exists():
            atomic_bytes(input_path,prepared)
        if source_bytes is not None and source_bytes!=raw and not (folder/"source.bin").exists():
            atomic_bytes(folder/"source.bin",source_bytes)
        with self.store.connect() as conn:
            conn.execute("""INSERT INTO media_assets(hash,mime,bytes,original_path,input_path,animated)
                VALUES(?,?,?,?,?,?) ON CONFLICT(hash) DO UPDATE SET
                mime=excluded.mime,original_path=excluded.original_path,input_path=excluded.input_path,animated=excluded.animated""",
                (digest,mime,len(raw),str(original.relative_to(self.root)),
                 str(input_path.relative_to(self.root)) if prepared else "",int(animated)))
            conn.execute("INSERT OR IGNORE INTO media_cipher_sources VALUES(?,?)",
                (hashlib.sha256(source_bytes if source_bytes is not None else raw).hexdigest(),digest))
        return self.get(digest)

    def link(self, uid, asset, category, *, account=None, md5=None, raw=None):
        """Only map a source MD5 after checking it against the actual file bytes."""
        with self.store.connect() as conn:
            conn.execute("""INSERT INTO media_links VALUES(?,?,?) ON CONFLICT(message_id) DO UPDATE SET
                asset=excluded.asset,category=excluded.category
                WHERE media_links.asset!=excluded.asset OR media_links.category!=excluded.category""",(uid,asset["hash"],category))
            if account and md5 and raw is not None and hashlib.md5(raw).hexdigest()==md5.lower():
                conn.execute("INSERT OR REPLACE INTO media_sources VALUES(?,?,?,?)",(account,category,md5.lower(),asset["hash"]))

    def input_bytes(self, asset):
        path=asset.get("input_path")
        if not path:
            return None
        try:
            return (self.root/path).read_bytes()
        except FileNotFoundError:
            # A missing derivative can be rebuilt from the retained local source.
            try:
                raw=(self.root/asset["original_path"]).read_bytes()
            except FileNotFoundError:
                return None
            if hashlib.sha256(raw).hexdigest()!=asset["hash"]:
                raise ValueError("本地素材副本校验失败")
            from vision import prepare_image
            prepared,mime=prepare_image(raw)
            repaired=self.register(raw,prepared,mime)
            return (self.root/repaired["input_path"]).read_bytes()

    def active(self, digest):
        with self.store.connect() as conn:
            row=conn.execute("""SELECT n.* FROM media_assets a JOIN media_analyses n ON n.id=a.active_analysis
                WHERE a.hash=?""",(digest,)).fetchone()
        if not row:
            return None
        result=dict(row)
        result["data"]=json.loads(result["data"])
        return result

    def reprocess_requested(self, digest):
        with self.store.connect() as conn:
            return bool(conn.execute("SELECT 1 FROM media_reprocess WHERE asset=?",(digest,)).fetchone())

    def claim(self, digest, owner, ttl=600):
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("""INSERT INTO media_jobs VALUES(?,?,?) ON CONFLICT(asset) DO UPDATE SET
                owner=excluded.owner,expires=excluded.expires WHERE media_jobs.expires<?""",
                (digest,owner,time.time()+ttl,time.time()))
            row=conn.execute("SELECT owner FROM media_jobs WHERE asset=?",(digest,)).fetchone()
            return bool(row and row["owner"]==owner)

    def release(self, digest, owner):
        with self.store.connect() as conn:
            conn.execute("DELETE FROM media_jobs WHERE asset=? AND owner=?",(digest,owner))

    def save_analysis(self, digest, data, model, signature, owner=""):
        uid=uuid.uuid4().hex
        stamp=datetime.now(TZ).isoformat(timespec="seconds")
        text=analysis_text(data)
        document={"id":uid,"asset":digest,"model":model,"signature":signature,"created_at":stamp,
            "ai_generated":True,"data":data}
        # Save the complete version before promoting it in the index.
        atomic_bytes(self.root/"analysis"/digest/(uid+".json"),json.dumps(document,ensure_ascii=False,indent=2).encode())
        with self.store.connect() as conn:
            conn.execute("INSERT INTO media_analyses VALUES(?,?,?,?,?,?,?)",(uid,digest,model,signature,json.dumps(data,ensure_ascii=False),text,stamp))
            conn.execute("UPDATE media_assets SET active_analysis=? WHERE hash=?",(uid,digest))
            conn.execute("DELETE FROM media_reprocess WHERE asset=?",(digest,))
        self.apply_analysis(digest)
        return self.active(digest)

    def apply_analysis(self, digest, message_id=None):
        analysis=self.active(digest)
        asset=self.get(digest)
        if not analysis:
            return
        with self.store.connect() as conn:
            links=conn.execute("SELECT message_id,category FROM media_links WHERE asset=?"+
                (" AND message_id=?" if message_id else ""),(digest,message_id) if message_id else (digest,)).fetchall()
            for link in links:
                emoji=link["category"]=="emoji"
                kind="静态表情" if emoji else "图片"
                marker="[静态表情解读（AI 生成）]" if emoji else "[图片解读（AI 生成）]"
                content=f"[{kind} #{digest[:16]}]\n{marker}\n"+analysis["text"]
                conn.execute("""INSERT INTO image_results VALUES(?,'done',?,?,?,?,?,?)
                    ON CONFLICT(message_id) DO UPDATE SET status='done',description=excluded.description,
                    bytes=excluded.bytes,model=excluded.model,error='',analyzed_at=excluded.analyzed_at,cache_key=excluded.cache_key
                    WHERE image_results.status!='done' OR image_results.description!=excluded.description
                    OR image_results.model!=excluded.model OR image_results.analyzed_at!=excluded.analyzed_at
                    OR image_results.cache_key!=excluded.cache_key""",
                    (link["message_id"],analysis["text"],asset["bytes"],analysis["model"],"",analysis["created_at"],digest))
                conn.execute("UPDATE messages SET kind=?,content=? WHERE id=? AND (kind!=? OR content!=?)",
                    (kind,content,link["message_id"],kind,content))

    def enrich(self, rows):
        if not rows:
            return rows
        ids=[r["id"] for r in rows]
        with self.store.connect() as conn:
            found={r["message_id"]:dict(r) for r in conn.execute("""SELECT l.message_id,l.asset,l.category,
                a.animated,n.id AS version,n.data,n.text AS description FROM media_links l
                JOIN media_assets a ON a.hash=l.asset LEFT JOIN media_analyses n ON n.id=a.active_analysis
                WHERE l.message_id IN ("""+",".join("?" for _ in ids)+")",ids)}
        for row in rows:
            value=found.get(row["id"])
            if value:
                row.update(asset_id=value["asset"],asset_category=value["category"],asset_animated=bool(value["animated"]),
                    asset_version=value["version"],asset_description=value["description"],
                    asset_analysis=json.loads(value["data"]) if value["data"] else None)
        return rows

    def request_reanalysis(self,include_emoji=True):
        if self.store.scope is None:
            return 0
        scope,args=self.store.scope_sql()
        with self.store.connect() as conn:
            rows=conn.execute("""SELECT DISTINCT a.hash FROM messages m JOIN chats c ON c.id=m.chat
                JOIN media_links l ON l.message_id=m.id JOIN media_assets a ON a.hash=l.asset
                WHERE c.selected=1 AND a.animated=0 AND a.active_analysis IS NOT NULL"""+scope+
                (" AND l.category='image'" if not include_emoji else ""),args).fetchall()
            for row in rows:
                conn.execute("INSERT OR REPLACE INTO media_reprocess VALUES(?,?)",
                    (row["hash"],datetime.now(TZ).isoformat(timespec="seconds")))
        return len(rows)


def citation_text(row):
    if row.get("asset_id"):
        kind="动态表情" if row.get("asset_animated") else ("静态表情" if row.get("asset_category")=="emoji" else "图片")
        return f"[{kind} #{row['asset_id'][:16]}]"+("（按设置不识别）" if row.get("asset_animated") else "")
    return row["content"]


def dictionary_lines(rows):
    seen=set()
    lines=[]
    for row in rows:
        key=row.get("asset_id")
        if key and key not in seen and row.get("asset_description"):
            seen.add(key)
            lines.append(f"\n素材 #{key[:16]} · {'静态表情' if row.get('asset_category')=='emoji' else '图片'}\n"+row["asset_description"])
    return (["\n素材词典 · 图片解读（AI 生成）及静态表情解读："]+lines) if lines else []
