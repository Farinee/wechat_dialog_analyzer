"""Local, selected-chat-only message index and transactional sync checkpoints."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import zlib
from datetime import datetime, timezone, timedelta
from pathlib import Path
from contextlib import contextmanager
from message_text import compact_emoji, compact_picture, compact_reply, EMOJI_KINDS, REPLY_KINDS
from media_store import MediaStore, init_schema

TZ = timezone(timedelta(hours=8), "北京时间")
TIMEZONE_LABEL = "北京时间（UTC+8）"


def epoch(value: str | None, end=False):
    if not value:
        return None
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}",value):
        raise ValueError("日期格式必须为 YYYY-MM-DD")
    stamp = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=TZ)
    return int((stamp + timedelta(days=1) if end else stamp).timestamp())


def display_time(stamp):
    return datetime.fromtimestamp(int(stamp), TZ).strftime("%Y-%m-%d %H:%M:%S")


def display_sync_time(value):
    if not value:
        return "尚未更新"
    try:
        stamp=datetime.fromisoformat(value)
        if stamp.tzinfo is None:
            stamp=stamp.replace(tzinfo=TZ)
        return stamp.astimezone(TZ).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError,ValueError):
        return "更新时间格式异常"


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.scope = None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS chats (
                    id TEXT PRIMARY KEY, account TEXT NOT NULL, username TEXT NOT NULL,
                    name TEXT NOT NULL, selected INTEGER NOT NULL DEFAULT 0,
                    cursor INTEGER NOT NULL DEFAULT 0, since INTEGER NOT NULL DEFAULT 0,
                    synced_at TEXT
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY, chat TEXT NOT NULL REFERENCES chats(id),
                    shard TEXT NOT NULL, local_id TEXT NOT NULL, seq INTEGER NOT NULL,
                    timestamp INTEGER NOT NULL, sender TEXT NOT NULL,
                    kind TEXT NOT NULL, content TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS messages_chat_time ON messages(chat, timestamp);
                CREATE INDEX IF NOT EXISTS messages_chat_seq ON messages(chat, seq);
                CREATE TABLE IF NOT EXISTS message_payloads (
                    message_id TEXT PRIMARY KEY REFERENCES messages(id),
                    codec TEXT NOT NULL, payload BLOB NOT NULL
                );
                CREATE TABLE IF NOT EXISTS image_results (
                    message_id TEXT PRIMARY KEY REFERENCES messages(id),
                    status TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
                    bytes INTEGER NOT NULL DEFAULT 0, model TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '', analyzed_at TEXT NOT NULL,
                    cache_key TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS image_cache (
                    cache_key TEXT PRIMARY KEY,description TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sync_ranges (
                    chat TEXT NOT NULL REFERENCES chats(id), start INTEGER NOT NULL,
                    end INTEGER NOT NULL, synced_at TEXT NOT NULL,
                    PRIMARY KEY(chat,start,end)
                );
                CREATE TABLE IF NOT EXISTS dirty_days (
                    chat TEXT NOT NULL REFERENCES chats(id), day TEXT NOT NULL,
                    PRIMARY KEY(chat,day)
                );
                CREATE TABLE IF NOT EXISTS archive_meta (key TEXT PRIMARY KEY,value TEXT);
                DROP TRIGGER IF EXISTS archive_insert;
                DROP TRIGGER IF EXISTS archive_update;
                DROP TRIGGER IF EXISTS archive_image_insert;
                DROP TRIGGER IF EXISTS archive_image_update;
                CREATE TRIGGER archive_insert AFTER INSERT ON messages BEGIN
                    INSERT INTO dirty_days VALUES(new.chat,date(new.timestamp,'unixepoch','+8 hours')) ON CONFLICT DO NOTHING;
                END;
                CREATE TRIGGER archive_update AFTER UPDATE ON messages BEGIN
                    INSERT INTO dirty_days VALUES(old.chat,date(old.timestamp,'unixepoch','+8 hours')) ON CONFLICT DO NOTHING;
                    INSERT INTO dirty_days VALUES(new.chat,date(new.timestamp,'unixepoch','+8 hours')) ON CONFLICT DO NOTHING;
                END;
                CREATE TRIGGER archive_image_insert AFTER INSERT ON image_results BEGIN
                    INSERT INTO dirty_days SELECT chat,date(timestamp,'unixepoch','+8 hours')
                    FROM messages WHERE id=new.message_id ON CONFLICT DO NOTHING;
                END;
                CREATE TRIGGER archive_image_update AFTER UPDATE ON image_results BEGIN
                    INSERT INTO dirty_days SELECT chat,date(timestamp,'unixepoch','+8 hours')
                    FROM messages WHERE id=new.message_id ON CONFLICT DO NOTHING;
                END;
                CREATE TRIGGER IF NOT EXISTS archive_name AFTER UPDATE OF name ON chats
                    WHEN old.name!=new.name BEGIN
                    INSERT INTO dirty_days SELECT DISTINCT chat,date(timestamp,'unixepoch','+8 hours')
                    FROM messages WHERE chat=new.id ON CONFLICT DO NOTHING;
                END;
            """)
            if not conn.execute("SELECT 1 FROM archive_meta WHERE key='text_migration'").fetchone():
                conn.execute("INSERT OR IGNORE INTO dirty_days SELECT DISTINCT chat,date(timestamp,'unixepoch','+8 hours') FROM messages")
                conn.execute("INSERT INTO archive_meta VALUES('text_migration','1')")
            try:
                conn.executescript("""
                    CREATE VIRTUAL TABLE IF NOT EXISTS message_search USING fts5(
                        content, content='messages', content_rowid='rowid', tokenize='trigram'
                    );
                    CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
                        INSERT INTO message_search(rowid, content) VALUES (new.rowid,new.content);
                    END;
                    CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
                        INSERT INTO message_search(message_search,rowid,content)
                        VALUES('delete',old.rowid,old.content);
                    END;
                    CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
                        INSERT INTO message_search(message_search,rowid,content)
                        VALUES('delete',old.rowid,old.content);
                        INSERT INTO message_search(rowid, content) VALUES(new.rowid,new.content);
                    END;
                """)
                self.fts = True
            except sqlite3.OperationalError:
                self.fts = False
            init_schema(conn)
            if not conn.execute("SELECT 1 FROM archive_meta WHERE key='emoji_compact_v4'").fetchone():
                self._compact_existing_emoji(conn)
                conn.execute("INSERT INTO archive_meta VALUES('emoji_compact_v4','1')")
            if not conn.execute("SELECT 1 FROM archive_meta WHERE key='picture_compact_v1'").fetchone():
                rows=conn.execute("SELECT id,content FROM messages WHERE kind='图片' AND content LIKE '%<img%'").fetchall()
                for row in rows:
                    compact=compact_picture(row["content"])
                    if compact!=row["content"]:
                        self._save_payload(conn,row["id"],row["content"])
                        conn.execute("UPDATE messages SET content=? WHERE id=?",(compact,row["id"]))
                conn.execute("INSERT INTO archive_meta VALUES('picture_compact_v1','1')")
            if not conn.execute("SELECT 1 FROM archive_meta WHERE key='reply_compact_v1'").fetchone():
                self._compact_existing_replies(conn)
                conn.execute("INSERT INTO archive_meta VALUES('reply_compact_v1','1')")
        self.media=MediaStore(self)

    def _compact_existing_replies(self, conn):
        last=""
        while True:
            rows=conn.execute("""SELECT id,kind,content FROM messages WHERE id>?
                AND kind IN (?,?) AND content LIKE '%<appmsg%' ORDER BY id LIMIT 200""",
                (last,*REPLY_KINDS)).fetchall()
            if not rows:
                break
            for row in rows:
                compact=compact_reply(row["kind"],row["content"])
                if compact is not None:
                    self._save_payload(conn,row["id"],row["content"])
                    conn.execute("UPDATE messages SET kind='引用回复',content=? WHERE id=?",(compact,row["id"]))
            last=rows[-1]["id"]

    @staticmethod
    def _save_payload(conn, uid, content):
        payload=zlib.compress(content.encode("utf-8"),level=9)
        conn.execute("""INSERT INTO message_payloads VALUES(?,'zlib',?)
            ON CONFLICT(message_id) DO UPDATE SET codec=excluded.codec,payload=excluded.payload
            WHERE message_payloads.payload!=excluded.payload""",(uid,payload))

    def _compact_existing_emoji(self, conn):
        last=""
        while True:
            rows=conn.execute("""SELECT m.id,m.kind,m.content,p.payload FROM messages m
                LEFT JOIN message_payloads p ON p.message_id=m.id
                LEFT JOIN image_results i ON i.message_id=m.id
                WHERE m.id>? AND m.kind IN ("""+",".join("?" for _ in EMOJI_KINDS)+""" )
                AND (m.content LIKE '%<emoji%' OR p.payload IS NOT NULL)
                AND (i.status IS NULL OR i.status!='done') ORDER BY m.id LIMIT 300""",
                (last,*EMOJI_KINDS)).fetchall()
            if not rows:
                break
            for row in rows:
                raw=zlib.decompress(row["payload"]).decode("utf-8") if row["payload"] else row["content"]
                content=compact_emoji(row["kind"],raw)
                if content is not None:
                    self._save_payload(conn,row["id"],raw)
                    if content!=row["content"]:
                        conn.execute("UPDATE messages SET content=? WHERE id=?",(content,row["id"]))
            last=rows[-1]["id"]

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    @staticmethod
    def chat_id(account, username):
        return hashlib.sha256(f"{account}\0{username}".encode()).hexdigest()[:24]

    def set_selection(self, account, groups, selected, since):
        with self.connect() as conn:
            conn.execute("UPDATE chats SET selected=0")
            for group in groups:
                name, username = group["name"], group["username"]
                chat = self.chat_id(account, username)
                conn.execute("""INSERT INTO chats(id,account,username,name,selected,since)
                    VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name, selected=excluded.selected,
                    cursor=CASE WHEN excluded.since < chats.since THEN 0 ELSE chats.cursor END,
                    since=MIN(chats.since,excluded.since)
                """, (chat, account, username, name, int(username in selected), since))

    def selected(self):
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM chats WHERE selected=1 ORDER BY name")]

    def set_scope(self, chat, start, end):
        """An additional hard boundary, independent of model-supplied filters."""
        if start >= end:
            raise ValueError("起始日期不能晚于结束日期")
        self.scope = (chat, int(start), int(end))

    def scope_sql(self):
        if self.scope is None:
            return "", ()
        return " AND m.chat=? AND m.timestamp>=? AND m.timestamp<?", self.scope

    def refresh_names(self, account, groups):
        """Rename existing chats without changing their selection or sync history."""
        with self.connect() as conn:
            for group in groups:
                conn.execute("UPDATE chats SET name=? WHERE id=?",
                    (group["name"],self.chat_id(account,group["username"])))

    def put(self, chat, rows):
        added = 0
        with self.connect() as conn:
            for row in rows:
                stamp = int(row.get("create_time") or 0)
                if stamp > 100_000_000_000:
                    stamp //= 1000
                uid = hashlib.sha256((chat + "\0" + str(row.get("shard", "")) +
                    "\0" + str(row["local_id"])).encode()).hexdigest()[:32]
                content = str(row.get("content") or "")
                kind = str(row.get("type") or "未知类型")
                original = content
                compact = compact_emoji(kind,content)
                reply = compact_reply(kind,content)
                if compact is not None:
                    content = compact
                elif reply is not None:
                    kind,content="引用回复",reply
                elif kind=="图片":
                    content = compact_picture(content)
                analysis=conn.execute("""SELECT m.content,m.kind FROM messages m
                    LEFT JOIN image_results i ON i.message_id=m.id LEFT JOIN media_links l ON l.message_id=m.id
                    LEFT JOIN media_assets a ON a.hash=l.asset WHERE m.id=?
                    AND (i.status IN ('done','skipped_animated') OR a.active_analysis IS NOT NULL)""",(uid,)).fetchone()
                if analysis:
                    content,kind=analysis["content"],analysis["kind"]
                old = conn.execute("SELECT 1 FROM messages WHERE id=?", (uid,)).fetchone()
                conn.execute("""INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET sender=excluded.sender,
                    kind=excluded.kind,content=excluded.content,timestamp=excluded.timestamp,seq=excluded.seq
                    WHERE messages.sender!=excluded.sender OR messages.kind!=excluded.kind
                    OR messages.content!=excluded.content OR messages.timestamp!=excluded.timestamp
                    OR messages.seq!=excluded.seq
                """, (uid, chat, str(row.get("shard", "")), str(row["local_id"]),
                    int(row.get("sort_seq") or 0), stamp,
                    str(row.get("sender_name") or row.get("sender_username") or "未知发言人"),
                    kind, content))
                if compact is not None or reply is not None or kind=="图片":
                    self._save_payload(conn,uid,original)
                added += not bool(old)
        return added

    def checkpoint(self, chat, seq):
        with self.connect() as conn:
            conn.execute("UPDATE chats SET cursor=MAX(cursor,?),synced_at=? WHERE id=?",
                (seq, datetime.now(TZ).isoformat(timespec="seconds"), chat))

    def raw_payload(self, uid):
        with self.connect() as conn:
            row=conn.execute("SELECT codec,payload FROM message_payloads WHERE message_id=?",(uid,)).fetchone()
        return zlib.decompress(row["payload"]).decode("utf-8") if row and row["codec"]=="zlib" else ""

    def preview_days(self):
        if self.scope is None:
            return []
        with self.connect() as conn:
            return [dict(r) for r in conn.execute("""SELECT date(timestamp,'unixepoch','+8 hours') AS day,COUNT(*) AS count
                FROM messages WHERE chat=? AND timestamp>=? AND timestamp<? GROUP BY day ORDER BY day""",self.scope)]

    @contextmanager
    def analysis_snapshot(self,scope=None):
        """Complete searchable text, newest first, within one frozen read snapshot."""
        bounds=self.scope if scope is None else scope
        if bounds is None or len(bounds)!=3 or not bounds[0] or bounds[1]>=bounds[2]:
            raise ValueError("请先选择一个聊天及日期范围，再分析全部消息")
        bounds=tuple(bounds)
        with self.connect() as conn:
            conn.execute("BEGIN")
            group=conn.execute("SELECT * FROM chats WHERE id=? AND selected=1",(bounds[0],)).fetchone()
            if group is None:
                raise ValueError("所选聊天无效")
            totals=conn.execute("""SELECT COUNT(*) AS count,MIN(timestamp) AS earliest,MAX(timestamp) AS latest
                FROM messages WHERE chat=? AND timestamp>=? AND timestamp<?""",bounds).fetchone()
            coverage=dict(group,**dict(totals),selected_start_date=display_time(bounds[1])[:10],
                selected_end_date=display_time(bounds[2]-1)[:10])
            cursor=conn.execute("""SELECT m.*,c.name AS group_name,l.asset AS asset_id,l.category AS asset_category,
                a.animated AS asset_animated,n.text AS asset_description,n.id AS asset_version
                FROM messages m JOIN chats c ON c.id=m.chat
                LEFT JOIN media_links l ON l.message_id=m.id LEFT JOIN media_assets a ON a.hash=l.asset
                LEFT JOIN media_analyses n ON n.id=a.active_analysis
                WHERE m.chat=? AND m.timestamp>=? AND m.timestamp<?
                ORDER BY m.timestamp DESC,m.seq DESC,m.id DESC""",bounds)
            def records():
                while True:
                    batch=cursor.fetchmany(200)
                    if not batch:
                        return
                    for record in batch:
                        value=dict(record)
                        value["time"]=display_time(value["timestamp"])
                        yield value
            yield coverage,records()

    def preview_page(self, day, page=0, page_size=200):
        if self.scope is None or page<0 or not 1<=page_size<=200:
            return []
        chat,start,end=self.scope
        start,end=max(start,epoch(day)),min(end,epoch(day,end=True))
        if start>=end:
            return []
        with self.connect() as conn:
            rows=[dict(r) for r in conn.execute("""SELECT * FROM messages
                WHERE chat=? AND timestamp>=? AND timestamp<? ORDER BY timestamp,seq,id LIMIT ? OFFSET ?""",
                (chat,start,end,page_size,page*page_size))]
        return self.media.enrich(rows)

    def original_records(self, ids):
        """Read retained source records for the UI only, within the current scope."""
        ids=list(dict.fromkeys(ids))
        if not ids or self.scope is None:
            return []
        scope,args=self.scope_sql()
        found={}
        with self.connect() as conn:
            for offset in range(0,len(ids),200):
                batch=ids[offset:offset+200]
                rows=conn.execute("""SELECT m.*,p.codec,p.payload,l.category,
                    a.hash AS asset_id,a.original_path,a.input_path,i.status AS media_status
                    FROM messages m JOIN chats c ON c.id=m.chat
                    LEFT JOIN message_payloads p ON p.message_id=m.id
                    LEFT JOIN media_links l ON l.message_id=m.id
                    LEFT JOIN media_assets a ON a.hash=l.asset
                    LEFT JOIN image_results i ON i.message_id=m.id
                    WHERE c.selected=1 AND m.id IN ("""+",".join("?" for _ in batch)+")"+scope,
                    (*batch,*args))
                for record in rows:
                    value=dict(record)
                    if value.pop("codec")=="zlib":
                        value["original_content"]=zlib.decompress(value.pop("payload")).decode("utf-8")
                    else:
                        value.pop("payload")
                        media=value["kind"] in ("图片",*EMOJI_KINDS) or value["asset_id"]
                        value["original_content"]="[未留存原始消息正文；请在可检索文字层查看已有记录]" if media else value["content"]
                    found[value["id"]]=value
        return [found[uid] for uid in ids if uid in found]

    def pending_images(self, account, include_emoji=True, *, scope=None):
        bounds=self.scope if scope is None else scope
        if bounds is None:
            return []
        chat,start,end=bounds
        if not chat or start>=end:
            raise ValueError("素材处理的聊天或日期范围无效")
        scope=" AND m.chat=? AND m.timestamp>=? AND m.timestamp<?"
        args=(chat,int(start),int(end))
        kinds=("图片",*EMOJI_KINDS) if include_emoji else ("图片",)
        with self.connect() as conn:
            rows=conn.execute("""SELECT m.*,c.name AS group_name,c.username
                FROM messages m JOIN chats c ON c.id=m.chat
                LEFT JOIN image_results i ON i.message_id=m.id
                WHERE c.selected=1 AND c.account=? AND m.kind IN ("""+",".join("?" for _ in kinds)+""" )
                AND (i.status IS NULL OR i.status NOT IN ('done','skipped_size','skipped_animated')
                    OR EXISTS(SELECT 1 FROM media_links l JOIN media_reprocess r ON r.asset=l.asset WHERE l.message_id=m.id))
                """+scope+" ORDER BY m.timestamp DESC,m.id",(account,*kinds,*args)).fetchall()
            return [self.present(row) for row in rows]

    def cached_image(self, key):
        with self.connect() as conn:
            row=conn.execute("SELECT description FROM image_cache WHERE cache_key=?",(key,)).fetchone()
            return row["description"] if row else None

    def legacy_image_analysis(self, digest):
        with self.connect() as conn:
            row=conn.execute("SELECT description FROM image_cache WHERE cache_key LIKE ? LIMIT 1",(digest+":%",)).fetchone()
        return row["description"] if row else None

    def save_image_result(self, message_id, status, description="",size=0,model="",error="",cache_key=""):
        with self.connect() as conn:
            conn.execute("""INSERT INTO image_results VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(message_id) DO UPDATE SET status=excluded.status,
                description=excluded.description,bytes=excluded.bytes,model=excluded.model,
                error=excluded.error,analyzed_at=excluded.analyzed_at,cache_key=excluded.cache_key
                WHERE image_results.status!=excluded.status OR image_results.description!=excluded.description
                OR image_results.bytes!=excluded.bytes OR image_results.model!=excluded.model
                OR image_results.error!=excluded.error OR image_results.cache_key!=excluded.cache_key""",
                (message_id,status,description,size,model,error,datetime.now(TZ).isoformat(timespec="seconds"),cache_key))
            if status=="done":
                conn.execute("UPDATE messages SET content=? WHERE id=?",
                    ("[图片]\n[图片解读（AI 生成）]\n"+description,message_id))
                if cache_key:
                    conn.execute("INSERT OR REPLACE INTO image_cache VALUES(?,?)",(cache_key,description))

    def image_status(self):
        scope,args=self.scope_sql()
        kinds=("图片",*EMOJI_KINDS)
        with self.connect() as conn:
            return [dict(row) for row in conn.execute("""SELECT COALESCE(i.status,'pending') AS status,COUNT(*) AS count
                FROM messages m JOIN chats c ON c.id=m.chat
                LEFT JOIN image_results i ON i.message_id=m.id
                WHERE c.selected=1 AND m.kind IN ("""+",".join("?" for _ in kinds)+")"+scope+
                " GROUP BY COALESCE(i.status,'pending')",(*kinds,*args))]

    def image_failures(self):
        scope,args=self.scope_sql()
        with self.connect() as conn:
            return [dict(row) for row in conn.execute("""SELECT c.name AS chat,i.status,i.error
                FROM image_results i JOIN messages m ON m.id=i.message_id
                JOIN chats c ON c.id=m.chat WHERE c.selected=1 AND i.status='failed'
                """+scope+" ORDER BY i.analyzed_at DESC LIMIT 5",args)]

    def stats(self):
        scope,args=self.scope_sql()
        selected=" AND c.id=?" if self.scope else ""
        with self.connect() as conn:
            rows = [dict(r) for r in conn.execute("""SELECT c.*,COUNT(m.id) AS count,
                MIN(m.timestamp) AS earliest,MAX(m.timestamp) AS latest
                FROM chats c LEFT JOIN messages m ON m.chat=c.id"""+scope+
                " WHERE c.selected=1"+selected+" GROUP BY c.id ORDER BY c.name",
                (*args,self.scope[0]) if self.scope else ())]
        if self.scope:
            for row in rows:
                row["selected_start_date"] = display_time(self.scope[1])[:10]
                row["selected_end_date"] = display_time(self.scope[2]-1)[:10]
        return rows

    @staticmethod
    def present(row):
        result = dict(row)
        result["time"] = display_time(result["timestamp"])
        result["content"] = result["content"][:2400]
        return result

    def search(self, keywords=None, chat_names=None, start_date=None, end_date=None, limit=30, order="newest"):
        if order not in ("newest","oldest"):
            raise ValueError("order 只能是 newest 或 oldest")
        if keywords is None:
            keywords = []
        if not isinstance(keywords, list) or not all(isinstance(x, str) for x in keywords):
            raise ValueError("keywords 必须是文字数组")
        terms = list(dict.fromkeys(x.strip()[:100] for x in keywords if x.strip()))[:12]
        if chat_names is not None and (not isinstance(chat_names, list) or
                not all(isinstance(x, str) for x in chat_names)):
            raise ValueError("chat_names 必须是文字数组")
        limit = max(1, min(int(limit), 60))
        where = ["c.selected=1"]
        args = []
        scope,scope_args=self.scope_sql()
        if scope:
            where.append(scope.removeprefix(" AND "))
            args.extend(scope_args)
        if chat_names:
            where.append("c.name IN (%s)" % ",".join("?" for _ in chat_names))
            args.extend(chat_names)
        for value, op, end in ((start_date, ">=", False), (end_date, "<", True)):
            stamp = epoch(value, end)
            if stamp is not None:
                where.append(f"m.timestamp {op} ?")
                args.append(stamp)
        join = ""
        if terms:
            if self.fts and all(len(x) >= 3 for x in terms):
                join = "JOIN message_search ON message_search.rowid=m.rowid"
                where.append("message_search MATCH ?")
                args.append(" OR ".join('"' + t.replace('"', '""') + '"' for t in terms))
            else:
                where.append("(" + " OR ".join("m.content LIKE ? ESCAPE '\\'" for _ in terms) + ")")
                args.extend("%" + t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%" for t in terms)
        source=f"FROM messages m JOIN chats c ON c.id=m.chat {join} WHERE {' AND '.join(where)}"
        direction="ASC" if order=="oldest" else "DESC"
        select="SELECT m.*,c.name AS group_name "+source
        with self.connect() as conn:
            conn.execute("BEGIN")
            totals=conn.execute("SELECT COUNT(*) AS count,MIN(m.timestamp) AS earliest,MAX(m.timestamp) AS latest "+source,args).fetchone()
            rows=[self.present(r) for r in conn.execute(select+
                f" ORDER BY m.timestamp {direction},m.seq {direction},m.id {direction} LIMIT ?",(*args,limit))]
            boundaries=[]
            for sort in (direction,"DESC" if direction=="ASC" else "ASC"):
                row=conn.execute(select+f" ORDER BY m.timestamp {sort},m.seq {sort},m.id {sort} LIMIT 1",args).fetchone()
                if row and all(r["id"]!=row["id"] for r in boundaries):
                    boundaries.append(self.present(row))
        self.media.enrich(rows)
        self.media.enrich(boundaries)
        return {"messages": rows, "has_more": totals["count"]>limit,"boundary_messages":boundaries,
            "statistics":{"count":totals["count"],"is_exact":True,"limited_by_message_cap":False,
                "basis":"本地满足本轮聊天、日期和关键词条件的全部消息；不受返回条数限制",
                "earliest_time":display_time(totals["earliest"]) if totals["earliest"] is not None else None,
                "latest_time":display_time(totals["latest"]) if totals["latest"] is not None else None},
            "coverage": self.stats(), "note": "消息正文有返回条数限制；统计覆盖全部匹配记录。没有匹配不代表历史中不存在。"}

    def neighbors(self, message_id, radius=4):
        radius = max(1, min(int(radius), 10))
        scope,args=self.scope_sql()
        with self.connect() as conn:
            center = conn.execute("""SELECT m.*,c.name AS group_name FROM messages m
                JOIN chats c ON c.id=m.chat WHERE m.id=? AND c.selected=1"""+scope, (message_id,*args)).fetchone()
            if center is None:
                return {"messages": [], "note": "消息不存在或不在当前选择的群中"}
            position = (center["timestamp"],center["seq"],center["id"])
            prior = conn.execute("""SELECT m.*,c.name AS group_name FROM messages m
                JOIN chats c ON c.id=m.chat WHERE m.chat=?
                AND (m.timestamp,m.seq,m.id)<(?,?,?)
                """+scope+" ORDER BY m.timestamp DESC,m.seq DESC,m.id DESC LIMIT ?",
                (center["chat"],*position,*args,radius)).fetchall()
            after = conn.execute("""SELECT m.*,c.name AS group_name FROM messages m
                JOIN chats c ON c.id=m.chat WHERE m.chat=?
                AND (m.timestamp,m.seq,m.id)>(?,?,?)
                """+scope+" ORDER BY m.timestamp,m.seq,m.id LIMIT ?",
                (center["chat"],*position,*args,radius)).fetchall()
            rows = list(reversed(prior)) + [center] + list(after)
            return {"messages": self.media.enrich([self.present(r) for r in rows])}


def sync_groups(store, reader, progress=lambda text: None, page_size=300, stop=None):
    result = {"added": 0, "groups": 0, "errors": []}
    for group in store.selected():
        if group["account"] != reader.account:
            result["errors"].append(f"{group['name']}：当前微信账号与保存的账号不一致")
            continue
        try:
            reader.refresh()
            offset, newest, added = 0, group["cursor"], 0
            while True:
                if stop is not None and stop.is_set():
                    raise RuntimeError("已停止更新，下次启动会重新补齐")
                rows = reader.messages(group["username"], page_size, offset)
                if not rows:
                    break
                if offset == 0:
                    newest = max(newest, max(int(r.get("sort_seq") or 0) for r in rows))
                usable = []
                past_boundary = False
                for row in rows:
                    stamp = int(row.get("create_time") or 0)
                    if stamp > 100_000_000_000:
                        stamp //= 1000
                    seq = int(row.get("sort_seq") or 0)
                    if seq > newest:
                        # Arrivals during this launch belong to the next launch's sync.
                        continue
                    if (group["cursor"] and seq < group["cursor"]) or stamp < group["since"]:
                        past_boundary = True
                        continue
                    usable.append(row)
                added += store.put(group["id"], usable)
                progress(f"正在更新 {group['name']} · 本轮新增 {added} 条")
                offset += len(rows)
                if len(rows) < page_size or past_boundary:
                    break
            store.checkpoint(group["id"], newest)
            result["added"] += added
            result["groups"] += 1
        except Exception as exc:
            result["errors"].append(f"{group['name']}：{exc}")
    return result
