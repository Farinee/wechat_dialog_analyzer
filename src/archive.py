"""Date-range imports and durable, incremental UTF-8 text archives."""
from __future__ import annotations

from datetime import datetime
from contextlib import closing
from pathlib import Path

from storage import TZ, epoch, display_time
from vision import process_images
from media_store import citation_text,dictionary_lines


def covered_ranges(store, chat):
    with store.connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT start,end,synced_at FROM sync_ranges WHERE chat=? ORDER BY start", (chat,))]


def missing_ranges(store, chat, start, end):
    missing, position = [], start
    for row in covered_ranges(store,chat):
        left,right = row["start"],row["end"]
        if right <= position:
            continue
        if left >= end:
            break
        if left > position:
            missing.append((position,min(left,end)))
        position = max(position,right)
    if position < end:
        missing.append((position,end))
    return missing


def mark_covered(store, chat, start, end):
    with store.connect() as conn:
        overlaps = conn.execute("SELECT start,end FROM sync_ranges WHERE chat=? AND start<=? AND end>=?",
            (chat,end,start)).fetchall()
        for row in overlaps:
            start,end = min(start,row["start"]),max(end,row["end"])
        conn.execute("DELETE FROM sync_ranges WHERE chat=? AND start<=? AND end>=?",(chat,end,start))
        conn.execute("INSERT INTO sync_ranges VALUES(?,?,?,?)",
            (chat,start,end,datetime.now(TZ).isoformat(timespec="seconds")))
        conn.execute("UPDATE chats SET synced_at=? WHERE id=?",
            (datetime.now(TZ).isoformat(timespec="seconds"),chat))


def archive_dir(store, chat):
    return store.path.parent / "transcripts" / chat


def export_dirty_days(store, *, scope=None):
    """Rewrite only changed days, atomically; the durable dirty queue permits retry.

    IDs come from account/chat/shard/local_id, so overlapping imports cannot
    duplicate text. SQLite remains the local search index for these same records.
    """
    exported = 0
    labels = {"pending":"待解读", "unavailable":"本机图片缺失", "failed":"解读失败，待重试",
        "skipped_size":"大小不符合解读条件","skipped_animated":"动态素材按设置不识别"}
    where,args="",()
    if scope is not None:
        chat,start,end=scope
        if not chat or start>=end:
            raise ValueError("文本导出的聊天或日期范围无效")
        where=" WHERE d.chat=? AND d.day>=? AND d.day<=?"
        args=(chat,display_time(start)[:10],display_time(end-1)[:10])
    with store.connect() as conn:
        days = conn.execute("SELECT d.*,c.name FROM dirty_days d JOIN chats c ON c.id=d.chat"+where+" ORDER BY d.chat,d.day",args).fetchall()
        for day in days:
            rows = [dict(r) for r in conn.execute("""SELECT m.*,COALESCE(i.status,'pending') AS image_status,i.error
                FROM messages m LEFT JOIN image_results i ON i.message_id=m.id
                WHERE chat=? AND timestamp>=? AND timestamp<? ORDER BY timestamp,seq,id""",
                (day["chat"],epoch(day["day"]),epoch(day["day"],end=True))).fetchall()]
            store.media.enrich(rows)
            folder = archive_dir(store,day["chat"])
            folder.mkdir(parents=True,exist_ok=True)
            path = folder / (day["day"]+".txt")
            tmp = path.with_suffix(".txt.tmp")
            with tmp.open("w",encoding="utf-8",newline="\n") as output:
                output.write(f"聊天：{day['name']}\n日期：{day['day']}\n时区：北京时间（UTC+8）\n条数：{len(rows)}\n")
                for row in rows:
                    output.write(f"\n[{display_time(row['timestamp'])}] {row['sender']} · {row['kind']}\n消息ID：{row['id']}\n{citation_text(row)}\n")
                    if (row["kind"] == "图片" or "表情" in row["kind"]) and row["image_status"] != "done":
                        output.write("[素材状态："+labels.get(row["image_status"],row["image_status"])+"]\n")
                output.write("\n".join(dictionary_lines(rows))+"\n")
            tmp.replace(path)
            conn.execute("DELETE FROM dirty_days WHERE chat=? AND day=?",(day["chat"],day["day"]))
            exported += 1
    return exported


def _batches(reader, username, start, end, page_size):
    if hasattr(reader,"message_batches"):
        yield from reader.message_batches(username,start,end,page_size)
        return
    # Compatibility for older adapters and synthetic test readers.
    offset = 0
    while rows := reader.messages(username,page_size,offset):
        yield rows
        offset += len(rows)
        if len(rows) < page_size:
            break


class LazyMediaReader:
    """Open WeChat only when an uncached local asset must actually be read."""
    def __init__(self,account,factory,cache_lookup=None):
        self.account,self.factory,self.reader=account,factory,None
        self.cache_lookup=cache_lookup

    def _get(self):
        if self.reader is None:
            self.reader=self.factory()
            if self.reader.account!=self.account:
                self.reader.close()
                self.reader=None
                raise RuntimeError("当前微信账号与所选聊天账号不一致")
            if hasattr(self.reader,"set_media_cache"):
                self.reader.set_media_cache(self.cache_lookup)
        return self.reader

    def image_asset(self,*args):
        return self._get().image_asset(*args)

    def emoji_asset(self,*args):
        return self._get().emoji_asset(*args)

    def close(self):
        if self.reader is not None:
            self.reader.close()


def sync_range(store, source_factory, *, client_factory=lambda:None, vision_enabled=True,
               force=False, progress=lambda text:None, stop=None, now=None, page_size=300,emoji_enabled=True,
               vlm_concurrency=200,confirm_vlm=None,emoji_cdn_enabled=False,download_concurrency=8):
    if store.scope is None:
        raise ValueError("请先选择一个聊天及日期范围")
    chat,start,end = store.scope
    job_scope=(chat,start,end)
    groups = [g for g in store.selected() if g["id"] == chat]
    if len(groups) != 1:
        raise ValueError("每次只能同步一个聊天")
    group = groups[0]
    now = int(datetime.now(TZ).timestamp()) if now is None else int(now)
    until = min(end,now+1)
    if start >= until:
        raise ValueError("不能同步尚未到来的日期")
    gaps = [(start,until)] if force else missing_ranges(store,chat,start,until)
    # Revisit a small live tail, including tied timestamps and delayed arrivals.
    today = epoch(datetime.fromtimestamp(now,TZ).strftime("%Y-%m-%d"))
    if not force and gaps and start <= now < end:
        if gaps[-1][1] == until:
            tail_start = max(start,today,gaps[-1][0]-300)
            gaps.append((tail_start,until))
            # Coalesce the potentially overlapping missing and tail intervals.
            merged=[]
            for a,b in sorted(gaps):
                if merged and a<=merged[-1][1]:
                    merged[-1]=(merged[-1][0],max(merged[-1][1],b))
                else:
                    merged.append((a,b))
            gaps=merged
    result = {"added":0,"groups":0,"errors":[],"files":0,"reused":not bool(gaps),
        "scope":{"name":group["name"],"start":display_time(start)[:10],"end":display_time(end-1)[:10]}}
    reader = None
    media_reader=None
    try:
        if gaps:
            progress("正在读取所选聊天的未保存日期…")
            reader = source_factory()
            if reader.account != group["account"]:
                raise RuntimeError("当前微信账号与所选聊天账号不一致，请重新加载列表")
            if hasattr(reader,"set_media_cache"):
                reader.set_media_cache(store.media.by_source_hash)
            reader.refresh()
            if force and hasattr(reader,"retry_media_failures"):
                reader.retry_media_failures()
            for left,right in gaps:
                with closing(_batches(reader,group["username"],left,right,page_size)) as batches:
                    for rows in batches:
                        if stop is not None and stop.is_set():
                            raise RuntimeError("已停止同步，已保存的文字会保留，下次可继续补齐")
                        usable=[]
                        for row in rows:
                            stamp=int(row.get("create_time") or 0)
                            if stamp>100_000_000_000:
                                stamp//=1000
                            if left<=stamp<right:
                                usable.append(row)
                        result["added"]+=store.put(chat,usable)
                        progress(f"正在保存 {group['name']} · 新增 {result['added']} 条")
                if stop is not None and stop.is_set():
                    raise RuntimeError("已停止同步，下次可继续补齐")
                result["files"]+=export_dirty_days(store,scope=job_scope)
                mark_covered(store,chat,left,right)
        result["groups"]=1
        if vision_enabled and not (stop is not None and stop.is_set()):
            pending=store.pending_images(group["account"],include_emoji=emoji_enabled,scope=job_scope)
            if pending:
                progress(f"本次素材范围：{group['name']} · {display_time(start)[:10]} 至 {display_time(end-1)[:10]}（北京时间）· 待处理 {len(pending)} 条")
                client=client_factory()
                media_reader=reader or LazyMediaReader(group["account"],source_factory,store.media.by_source_hash)
                result["vision"]=process_images(store,media_reader,client,progress,stop,emoji_enabled,scope=job_scope,
                    concurrency=vlm_concurrency,confirm=confirm_vlm,emoji_cdn_enabled=emoji_cdn_enabled,
                    download_concurrency=download_concurrency)
    except Exception as exc:
        result["errors"].append(str(exc))
    finally:
        try:
            result["files"]+=export_dirty_days(store,scope=job_scope)
        finally:
            if reader is not None:
                reader.close()
            elif media_reader is not None:
                media_reader.close()
    return result
