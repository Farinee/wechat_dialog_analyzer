"""Adapt the reviewed DB reader without importing sending/UI automation code."""
from __future__ import annotations

import json
import tempfile
import hashlib
import re
from pathlib import Path
from secrets_local import save_secret, load_secret
from source_cache import SourceCache


def readable_name(username, *candidates):
    """An internal WeChat identifier is not a display name."""
    for value in candidates:
        if isinstance(value, bytes):
            value = value.decode("utf-8",errors="replace")
        if not isinstance(value,str):
            continue
        value=value.strip()
        if value and value != username and not value.endswith("@chatroom") and not value.startswith("wxid_"):
            return value
    return ""


class Source:
    def __init__(self, data_dir, account=None, db_dir=None):
        from wechatauto.db import WeChatDB

        root = Path(data_dir)
        self.data_dir=root
        self._image_files={}
        self._image_reader=None
        (root / "cache").mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="snapshot-", dir=root / "cache")
        key_dir = root / "wechat-keys"
        key_dir.mkdir(parents=True, exist_ok=True)

        class PrivateReader(WeChatDB):
            def _stable_key_file(self, account=None):
                return str(key_dir / ((account or self.account) + ".bin"))

            def _stable_key_dirs(self):
                return []

            def _load_key_cache(self, path):
                key_path = key_dir / (self.account + ".bin")
                try:
                    raw = load_secret(key_path)
                    return {k: bytes.fromhex(v) for k,v in json.loads(raw).items()} if raw else {}
                except (OSError, ValueError):
                    return {}

            def _save_keys(self):
                if self._keys:
                    save_secret(key_dir / (self.account + ".bin"),
                        json.dumps({k:v.hex() for k,v in self._keys.items()}).encode())

        try:
            self.db = PrivateReader(db_dir=db_dir or None, account=account or None,
                workdir=self.temp.name)
            self.account = self.db.account
            self.nicknames = {}
        except Exception:
            self.temp.cleanup()
            raise

    def groups(self):
        conn = self.db._contact_conn()
        if conn is None:
            raise RuntimeError("未能读取群列表，请确认微信已登录且版本受支持")
        try:
            rows = conn.execute("""SELECT username,nick_name,remark FROM contact
                WHERE username LIKE '%@chatroom' ORDER BY nick_name""").fetchall()
            groups=[]
            for row in rows:
                username=row["username"]
                name=readable_name(username,row["remark"],row["nick_name"])
                named=bool(name)
                if not name:
                    members=conn.execute("""SELECT c.username,c.remark,c.nick_name
                        FROM chatroom_member m JOIN chat_room r ON r.id=m.room_id
                        JOIN contact c ON c.id=m.member_id
                        WHERE r.username=? ORDER BY m.member_id LIMIT 8""",(username,)).fetchall()
                    names=[]
                    for member in members:
                        member_name=readable_name(member["username"],member["remark"],member["nick_name"])
                        if member_name and member_name not in names:
                            names.append(member_name[:16])
                        if len(names)==3:
                            break
                    name="未命名群" + ("（"+"、".join(names)+"等）" if names else "")
                groups.append({"username":username,"kind":"群聊","name":name,"name_resolved":named})
            return groups
        finally:
            conn.close()

    def contacts(self):
        conn = self.db._contact_conn()
        if conn is None:
            raise RuntimeError("未能读取联系人列表")
        try:
            rows = conn.execute("""SELECT username,nick_name,remark FROM contact
                WHERE username NOT LIKE '%@chatroom' AND username NOT LIKE 'gh_%'
                AND username NOT IN ('filehelper','fmessage','weixin','newsapp')
                ORDER BY nick_name""").fetchall()
            return [{"username":r["username"],"kind":"个人",
                "name":readable_name(r["username"],r["remark"],r["nick_name"]) or "未命名联系人"} for r in rows]
        finally:
            conn.close()

    def messages(self, username, limit, offset):
        if not isinstance(username,str) or not username:
            raise ValueError("聊天标识不能为空")
        rows = self.db.get_messages(username, limit, offset)
        return self._resolve_senders(rows)

    def _resolve_senders(self, rows):
        for row in rows:
            sender = row.get("sender_username")
            if sender:
                if sender not in self.nicknames:
                    self.nicknames[sender] = self.db.get_nickname(sender) or sender
                row["sender_name"] = self.nicknames[sender]
        return rows

    def message_batches(self, username, start, end, page_size=300):
        """Read only the requested dates, with each shard held at one snapshot.

        Cursors avoid rescanning/decoding preceding pages and remain stable when
        new messages arrive in WeChat while this import is running.
        """
        if not username or start >= end:
            raise ValueError("聊天或日期范围无效")
        if any("message" in str(rel).lower() for rel in self.db.unkeyed):
            raise RuntimeError("部分消息分片尚未解锁，本轮不标记范围完成，请重新连接微信后重试")
        tables = self.db._msg_conns(username)
        try:
            maps = self.db._shard_maps(tables)
            for conn,table in tables:
                shard = self.db._shard_label(conn)
                cursor = conn.execute("""SELECT local_id,local_type,real_sender_id,create_time,
                    message_content,source,packed_info_data,compress_content,server_id,sort_seq,? AS shard
                    FROM %s WHERE (create_time>=? AND create_time<?)
                    OR (create_time>=? AND create_time<?) ORDER BY create_time,local_id""" % table,
                    (shard,start,end,start*1000,end*1000))
                while rows := cursor.fetchmany(page_size):
                    yield self._resolve_senders([self.db._msg_row_to_dict(row,maps) for row in rows])
        finally:
            for conn,_ in tables:
                conn.close()

    def chat_statistics(self,groups,progress=lambda text:None,stop=None):
        """Aggregate source record counts and stored body bytes, never attachments."""
        by_table={"Msg_"+hashlib.md5(group["username"].encode()).hexdigest():group["username"] for group in groups}
        totals={group["username"]:{"message_count":0,"content_bytes":0} for group in groups}
        databases=self.db._message_dbs()
        for index,rel in enumerate(databases):
            if stop is not None and stop.is_set():
                raise RuntimeError("已停止聊天记录统计")
            progress(f"正在统计聊天记录 {index+1}/{len(databases)}，请稍候…")
            conn=self.db._open(rel)
            try:
                tables=[row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
                for table in tables:
                    username=by_table.get(table)
                    if username is None or not re.fullmatch(r"Msg_[0-9a-fA-F]{32}",table):
                        continue
                    if stop is not None and stop.is_set():
                        raise RuntimeError("已停止聊天记录统计")
                    row=conn.execute(f'''SELECT COUNT(*),
                        COALESCE(SUM(COALESCE(length(CAST(message_content AS BLOB)),0)),0)
                        FROM "{table}"''').fetchone()
                    totals[username]["message_count"]+=int(row[0])
                    totals[username]["content_bytes"]+=int(row[1])
            finally:
                conn.close()
        return [dict(group,**totals[group["username"]]) for group in groups]

    def refresh(self):
        self.db._refresh_db_files()

    def set_media_cache(self,lookup):
        self._media_cache_lookup=lookup

    def source_cache(self):
        if not hasattr(self,"_source_cache"):
            self._source_cache=SourceCache(getattr(self,"data_dir",None))
        return self._source_cache

    def retry_media_failures(self):
        self._retry_media_failures=True

    def _cached_media(self,data):
        lookup=getattr(self,"_media_cache_lookup",None)
        asset=lookup(hashlib.sha256(data).hexdigest()) if lookup else None
        return {"asset_hash":asset["hash"],"size":asset["bytes"]} if asset else None

    def image_asset(self, username, local_id, shard):
        from vision import mime_type,MAX_IMAGE_BYTES
        row=self.db.get_message_row(username,int(local_id),local_type=3,shard=shard)
        if not row:
            return None
        digests=[]
        for blob in (row.get("packed_info"),row.get("content")):
            if isinstance(blob,str):
                blob=blob.encode()
            if isinstance(blob,bytes):
                for match in re.findall(rb"[0-9a-fA-F]{32}",blob):
                    digest=match.decode().lower()
                    if digest not in digests:
                        digests.append(digest)
        if username not in self._image_files:
            base=Path(self.db.account_dir)/"msg"/"attach"/hashlib.md5(username.encode()).hexdigest()
            self._image_files[username]={p.name.lower():p for p in self.source_cache().files(base) if p.suffix.lower()==".dat"}
        files=self._image_files[username]
        chosen=None
        for suffix,quality in (("_h.dat","original"),(".dat","cached"),("_t.dat","thumbnail")):
            for digest in digests:
                if digest+suffix in files:
                    chosen=(files[digest+suffix],quality)
                    break
            if chosen:
                break
        if not chosen:
            return None
        path,quality=chosen
        if path.stat().st_size>MAX_IMAGE_BYTES+65536:
            return {"bytes":b"","size":path.stat().st_size,"quality":quality}
        data=path.read_bytes()
        cached=self._cached_media(data)
        if cached:
            return cached
        source_data=data
        if not mime_type(data) and not data.startswith(b"wxgf"):
            crypto=self._crypto_reader()
            crypto._probe_ct(str(path))
            try:
                data=crypto.decrypt_image(str(path))
            except RuntimeError as exc:
                if "AES" in str(exc) and "密钥" in str(exc):
                    exc.stop_batch=True
                raise
        return {"bytes":data,"source_bytes":source_data,"quality":quality,"size":len(data)}

    def emoji_asset(self, username, local_id, shard, metadata=None):
        """Resolve a selected message's full local sticker, never its thumbnail.

        Cache files are often extensionless. The database's sticker type does
        not tell whether the full file is animated, so callers inspect frames.
        """
        from message_text import emoji_metadata
        from vision import mime_type,MAX_IMAGE_BYTES
        metadata=metadata or {}
        if not metadata:
            row=self.db.get_message_row(username,int(local_id),shard=shard)
            blob=(row or {}).get("content","")
            if isinstance(blob,bytes):
                from wechatauto.db import WeChatDB
                blob=WeChatDB._friendly_content(blob,"动画表情")
            metadata=emoji_metadata(blob)
        digests=[]
        for key in ("md5","androidmd5","s60v3md5","s60v5md5","externmd5"):
            value=metadata.get(key,"").lower()
            if re.fullmatch(r"[0-9a-f]{32}",value) and value not in digests:
                digests.append(value)
        if not digests:
            return None
        if not hasattr(self,"_emoji_files"):
            base=Path(self.db.account_dir)
            roots=[base/"business"/"emoticon"/"Persist",base/"business"/"emoticon"/"PersistStore"]
            roots+=sorted((base/"cache").glob("*/Emoticon"),reverse=True)
            self._emoji_files={}
            for root in roots:
                for path in self.source_cache().files(root):
                    if path.suffix.lower() not in ("",".gif",".png",".webp",".jpg",".jpeg",".dat"):
                        continue
                    stem=path.stem.lower() if path.suffix else path.name.lower()
                    if re.fullmatch(r"[0-9a-f]{32}",stem):
                        self._emoji_files.setdefault(stem,[]).append(path)
        failures=[]
        secret=Path(getattr(self,"data_dir","."))/"wechat-keys"/(getattr(self,"account","")+".emoji.bin")
        key_stamp=secret.stat().st_mtime_ns if secret.is_file() else 0
        if getattr(self,"_emoji_key_stamp",None)!=key_stamp:
            self.__dict__.pop("_emoji_key_attempted",None)
            self.__dict__.pop("_emoji_cache_key",None)
            self._emoji_key_stamp=key_stamp
        failure_tag=str(metadata.get("aeskey",""))+":"+str(key_stamp)
        for digest in digests:
            for path in self._emoji_files.get(digest,[]):
                if not path.is_file():
                    continue
                if self.source_cache().failure(path,failure_tag,retry=getattr(self,"_retry_media_failures",False)):
                    failures.append(path)
                    continue
                size=path.stat().st_size
                if size>=MAX_IMAGE_BYTES+128:
                    return {"bytes":b"","size":size,"quality":"local-sticker"}
                source=path.read_bytes()
                cached=self._cached_media(source)
                if cached:
                    return cached
                data=source
                if not mime_type(data):
                    key=metadata.get("aeskey","")
                    if re.fullmatch(r"[0-9a-fA-F]{32}",key) and len(data)%16==0:
                        from cryptography.hazmat.primitives.ciphers import Cipher,algorithms,modes
                        raw_key=bytes.fromhex(key)
                        for mode in (modes.CBC(raw_key),modes.CBC(bytes(16)),modes.ECB()):
                            decryptor=Cipher(algorithms.AES(raw_key),mode).decryptor()
                            candidate=decryptor.update(data)+decryptor.finalize()
                            pad=candidate[-1] if candidate else 0
                            if 0<pad<=16 and candidate[-pad:]==bytes([pad])*pad:
                                candidate=candidate[:-pad]
                            if mime_type(candidate) or candidate.startswith(b"wxgf"):
                                data=candidate
                                break
                if not mime_type(data) and not data.startswith(b"wxgf") and hasattr(self.db,"_find_weixin_pids"):
                    data=self._decrypt_sticker_cache(source) or data
                if mime_type(data) or data.startswith(b"wxgf"):
                    return {"bytes":data,"size":len(data),"source_bytes":source,"quality":"local-sticker"}
                self.source_cache().remember_failure(path,failure_tag,"本机表情加密或格式暂不支持")
                failures.append(path)
        if failures:
            raise RuntimeError("本机表情文件存在，但格式或加密方式暂不支持；未上传素材")
        return None

    def _decrypt_sticker_cache(self, data):
        """Try an existing local key only; unsupported stickers never trigger a scan."""
        if not data or len(data)%16:
            return None
        from vision import mime_type
        if not getattr(self,"_emoji_key_attempted",False):
            self._emoji_key_attempted=True
            secret=self.data_dir/"wechat-keys"/(self.account+".emoji.bin")
            raw=load_secret(secret)
            self._emoji_cache_key=raw.decode("ascii") if raw and len(raw)==16 else None
        key=getattr(self,"_emoji_cache_key",None)
        if not key:
            return None
        from cryptography.hazmat.primitives.ciphers import Cipher,algorithms,modes
        for mode in (modes.ECB(),modes.CBC(bytes(16))):
            decryptor=Cipher(algorithms.AES(key.encode()),mode).decryptor()
            plain=decryptor.update(data)+decryptor.finalize()
            pad=plain[-1] if plain else 0
            if 0<pad<=16 and plain[-pad:]==bytes([pad])*pad:
                plain=plain[:-pad]
            if mime_type(plain) or plain.startswith(b"wxgf"):
                return plain
        return None

    def _crypto_reader(self):
        if self._image_reader is not None:
            return self._image_reader
        from wechatauto.image_crypto import MediaDownloader
        secret_path=self.data_dir/"wechat-keys"/(self.account+".image.bin")
        class PrivateImages(MediaDownloader):
            def _load_persisted_key(inner):
                raw=load_secret(secret_path)
                key=raw.decode() if raw else None
                return key if key and inner._validate_key(key) else None
            def _persist_key(inner,key):
                save_secret(secret_path,key.encode())
        self._image_reader=PrivateImages(self.db,save_dir=self.temp.name)
        return self._image_reader

    def close(self):
        self.temp.cleanup()
