"""Shared incremental lexical index with strict chat/date retrieval boundaries."""
from __future__ import annotations
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
from media_store import citation_text
from storage import display_time,epoch
from rag_context import conversation_windows,CONTEXT_POLICY

VERSION="lexical-1"


def scope_key(scope):
    if scope is None or len(scope)!=3 or not scope[0] or scope[1]>=scope[2]:
        raise ValueError("RAG 需要明确的聊天及日期范围")
    return hashlib.sha256(json.dumps(list(scope)).encode()).hexdigest()


def chat_key(chat):
    # Store chat IDs already include the owning WeChat account. History still
    # uses scope_key: sharing source chunks must not merge Q&A sessions.
    if not chat:
        raise ValueError("RAG 需要明确的聊天")
    return digest(["shared-chat",chat])


def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode()).hexdigest()


def prefix(text,limit,counter):
    if counter.count(text)<=limit:
        return text
    low,high=0,len(text)
    while low<high:
        mid=(low+high+1)//2
        if counter.count(text[:mid])<=limit:
            low=mid
        else:
            high=mid-1
    return text[:low]


class RagIndex:
    def __init__(self,path):
        self.path=Path(path)
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS scopes(id TEXT PRIMARY KEY,coverage TEXT NOT NULL,generation INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS buckets(scope TEXT,bucket INTEGER,hash TEXT,PRIMARY KEY(scope,bucket));
                CREATE TABLE IF NOT EXISTS records(scope TEXT,id TEXT,bucket INTEGER,timestamp INTEGER,seq INTEGER,
                    text TEXT,asset_text TEXT,payload TEXT,PRIMARY KEY(scope,id));
                CREATE INDEX IF NOT EXISTS rag_record_time ON records(scope,timestamp,seq,id);
                CREATE TABLE IF NOT EXISTS chunks(id TEXT PRIMARY KEY,scope TEXT,bucket INTEGER,
                    timestamp INTEGER,last_timestamp INTEGER,text TEXT,parts TEXT);
                CREATE INDEX IF NOT EXISTS rag_chunk_scope ON chunks(scope,timestamp);
                CREATE TABLE IF NOT EXISTS memories(scope TEXT,session TEXT,through INTEGER,hash TEXT,summary TEXT,
                    PRIMARY KEY(scope,session));
                CREATE TABLE IF NOT EXISTS rag_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS indexed_ranges(scope TEXT,start INTEGER,end INTEGER,
                    PRIMARY KEY(scope,start));
            """)
            try:
                conn.executescript("""
                    CREATE VIRTUAL TABLE IF NOT EXISTS rag_fts USING fts5(text,content='chunks',content_rowid='rowid',tokenize='trigram');
                    CREATE TRIGGER IF NOT EXISTS rag_ai AFTER INSERT ON chunks BEGIN
                        INSERT INTO rag_fts(rowid,text) VALUES(new.rowid,new.text);
                    END;
                    CREATE TRIGGER IF NOT EXISTS rag_ad AFTER DELETE ON chunks BEGIN
                        INSERT INTO rag_fts(rag_fts,rowid,text) VALUES('delete',old.rowid,old.text);
                    END;
                """)
                self.fts=True
            except sqlite3.OperationalError:
                self.fts=False

    @contextmanager
    def connect(self):
        conn=sqlite3.connect(self.path,timeout=60)
        conn.row_factory=sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _save_bucket(self,conn,key,bucket,records,counter,model,check):
        check()
        # A canonical order makes a subset/overlap visit reuse the same hash.
        records=sorted(records,key=lambda r:(r["timestamp"],r["seq"],r["id"]),reverse=True)
        fingerprint=digest([VERSION,model,records])
        previous=conn.execute("SELECT hash FROM buckets WHERE scope=? AND bucket=?",(key,bucket)).fetchone()
        if previous and previous[0]==fingerprint:
            return "reused_buckets"
        conn.execute("DELETE FROM chunks WHERE scope=? AND bucket=?",(key,bucket))
        conn.execute("DELETE FROM records WHERE scope=? AND bucket=?",(key,bucket))
        if not records:
            conn.execute("DELETE FROM buckets WHERE scope=? AND bucket=?",(key,bucket))
            return "removed_buckets" if previous else None
        records.reverse()
        by_id={record["id"]:record for record in records}
        parts=[]
        texts=[]
        count=0
        def flush():
            nonlocal parts,texts,count
            if not parts:
                return
            chunk_uid=digest([key,bucket,parts,VERSION])
            search="\n".join(texts)
            seen_assets=set()
            for uid in dict.fromkeys(p["id"] for p in parts):
                record=by_id[uid]
                if record.get("asset_id") not in seen_assets:
                    search+="\n"+record.get("asset_description","")
                    seen_assets.add(record.get("asset_id"))
            times=[p["timestamp"] for p in parts]
            conn.execute("INSERT INTO chunks VALUES(?,?,?,?,?,?,?)",
                (chunk_uid,key,bucket,min(times),max(times),search,json.dumps(parts,ensure_ascii=False)))
            parts,texts,count=[],[],0
        for record in records:
            check()
            text=record["text"]
            conn.execute("INSERT OR REPLACE INTO records VALUES(?,?,?,?,?,?,?,?)",(key,record["id"],bucket,
                record["timestamp"],record["seq"],text,record.get("asset_description","") or "",json.dumps(record,ensure_ascii=False)))
            start=0
            while start<len(text) or (not text and start==0):
                body=prefix(text[start:],600,counter)
                if text and not body:
                    raise RuntimeError("分块容量不足以容纳单个字符")
                cost=counter.count(body)+32
                if parts and count+cost>800:
                    flush()
                parts.append({"id":record["id"],"start":start,"end":start+len(body),"timestamp":record["timestamp"]})
                texts.append(body)
                count+=cost
                if start+len(body)>=len(text):
                    break
                flush()
                start+=max(1,len(body)-min(100,len(body)//5))
        flush()
        conn.execute("INSERT OR REPLACE INTO buckets VALUES(?,?,?)",(key,bucket,fingerprint))
        return "changed_buckets"

    def _migrate(self,conn,counter,model,check,progress):
        if conn.execute("SELECT 1 FROM rag_meta WHERE key='shared_chat_index'").fetchone():
            return
        legacy=conn.execute("SELECT * FROM scopes ORDER BY rowid").fetchall()
        if legacy:
            progress("正在将已有 RAG 缓存合并为共享索引，保留问答摘要…")
        for entry in legacy:
            check()
            metadata=json.loads(entry["coverage"])
            key=chat_key(metadata["id"])
            old=entry["id"]
            buckets=conn.execute("SELECT bucket FROM buckets WHERE scope=?",(old,)).fetchall()
            for row in buckets:
                check()
                bucket=row["bucket"]
                existing=conn.execute("SELECT 1 FROM buckets WHERE scope=? AND bucket=?",(key,bucket)).fetchone()
                if existing:
                    # Later published snapshots win duplicated message IDs.
                    merged={}
                    for namespace in (key,old):
                        for record in conn.execute("SELECT id,payload FROM records WHERE scope=? AND bucket=?",(namespace,bucket)):
                            merged[record["id"]]=json.loads(record["payload"])
                    self._save_bucket(conn,key,bucket,list(merged.values()),counter,model,check)
                else:
                    # The hash is independent of the old date-range namespace;
                    # move unchanged chunks without tokenizing their text again.
                    for table in ("records","chunks","buckets"):
                        conn.execute(f"UPDATE {table} SET scope=? WHERE scope=? AND bucket=?",(key,old,bucket))
            prior=conn.execute("SELECT generation FROM scopes WHERE id=?",(key,)).fetchone()
            generation=max(entry["generation"],prior[0] if prior else 0)+1
            conn.execute("INSERT OR REPLACE INTO scopes VALUES(?,?,?)",(key,entry["coverage"],generation))
            for table in ("chunks","records","buckets"):
                conn.execute(f"DELETE FROM {table} WHERE scope=?",(old,))
            conn.execute("DELETE FROM scopes WHERE id=?",(old,))
        # Legacy coverage contains calendar dates, not exact epoch bounds. Only
        # a fresh source snapshot certifies ranges; never broaden legacy access.
        conn.execute("INSERT INTO rag_meta VALUES('shared_chat_index','1')")

    @staticmethod
    def _mark_range(conn,key,start,end):
        overlaps=conn.execute("SELECT start,end FROM indexed_ranges WHERE scope=? AND end>=? AND start<=?",
            (key,start,end)).fetchall()
        for row in overlaps:
            start=min(start,row["start"])
            end=max(end,row["end"])
            conn.execute("DELETE FROM indexed_ranges WHERE scope=? AND start=?",(key,row["start"]))
        conn.execute("INSERT INTO indexed_ranges VALUES(?,?,?)",(key,start,end))

    def sync(self,store,scope,counter,*,model="",progress=lambda _:None,stop=None):
        scope_key(scope) # Validate without using dates as a source-index namespace.
        scope=tuple(scope)
        key=chat_key(scope[0])
        counts={"changed_buckets":0,"reused_buckets":0,"removed_buckets":0}
        def check():
            if stop is not None and stop.is_set():
                raise RuntimeError("已停止建立 RAG 索引，原有完整索引继续保留")
        with self.connect() as conn,store.analysis_snapshot(scope) as (coverage,rows):
            conn.execute("BEGIN IMMEDIATE")
            self._migrate(conn,counter,model,check,progress)
            previous={r["bucket"] for r in conn.execute("SELECT bucket FROM buckets WHERE scope=? AND bucket>=? AND bucket<=?",
                (key,scope[1]//1800,(scope[2]-1)//1800))}
            live=set()
            def save(bucket,records):
                live.add(bucket)
                # Arbitrary sub-hour bounds must preserve cached events outside
                # this request; the source snapshot itself never expands scope.
                if bucket*1800<scope[1] or (bucket+1)*1800>scope[2]:
                    records=records+[json.loads(r[0]) for r in conn.execute("""SELECT payload FROM records
                        WHERE scope=? AND bucket=? AND (timestamp<? OR timestamp>=?)""",(key,bucket,scope[1],scope[2]))]
                action=self._save_bucket(conn,key,bucket,records,counter,model,check)
                if action:
                    counts[action]+=1
                if action=="changed_buckets":
                    progress(f"正在更新共享 RAG 索引 · 已更新 {counts['changed_buckets']} 个时间段，复用 {counts['reused_buckets']} 个")
            current=None
            records=[]
            for raw in rows:
                check()
                record={k:raw.get(k) for k in ("id","timestamp","seq","sender","kind","group_name","asset_id","asset_version")}
                record.update(time=raw["time"],text=citation_text(raw),asset_description=raw.get("asset_description") or "")
                bucket=raw["timestamp"]//1800
                if current is not None and bucket!=current:
                    save(current,records)
                    records=[]
                current=bucket
                records.append(record)
            if current is not None:
                save(current,records)
            for bucket in previous-live:
                save(bucket,[])
            prior=conn.execute("SELECT generation FROM scopes WHERE id=?",(key,)).fetchone()
            generation=(prior[0] if prior else 0)+int(bool(counts["changed_buckets"] or counts["removed_buckets"] or prior is None))
            conn.execute("INSERT OR REPLACE INTO scopes VALUES(?,?,?)",(key,json.dumps(coverage,ensure_ascii=False),generation))
            self._mark_range(conn,key,scope[1],scope[2])
            check()
        return dict(counts,generation=generation)

    def retrieve(self,scope,plan,limit=None,*,stop=None):
        scope_key(scope)
        key=chat_key(scope[0])
        start=max(scope[1],epoch(plan.get("start_date")) or scope[1])
        end=min(scope[2],epoch(plan.get("end_date"),True) or scope[2])
        terms=list(dict.fromkeys(t.casefold().strip() for t in plan.get("keywords",[]) if t.strip()))[:8]
        oldest=plan.get("order")=="oldest"
        direction="ASC" if oldest else "DESC"
        with self.connect() as conn:
            conn.execute("BEGIN")
            entry=conn.execute("SELECT * FROM scopes WHERE id=?",(key,)).fetchone()
            complete=conn.execute("SELECT 1 FROM indexed_ranges WHERE scope=? AND start<=? AND end>=?",
                (key,scope[1],scope[2])).fetchone()
            if entry is None or not complete:
                raise RuntimeError("此范围尚未建立完整 RAG 索引")
            coverage=json.loads(entry["coverage"])
            scoped=conn.execute("""SELECT COUNT(*) AS count,MIN(timestamp) AS earliest,MAX(timestamp) AS latest
                FROM records WHERE scope=? AND timestamp>=? AND timestamp<?""",(key,scope[1],scope[2])).fetchone()
            coverage.update(dict(scoped),selected_start_date=display_time(scope[1])[:10],
                selected_end_date=display_time(scope[2]-1)[:10])
            base="scope=? AND timestamp>=? AND timestamp<?"
            args=[key,start,end]
            clauses=[]
            for term in terms:
                clauses.append("(instr(lower(text),?)>0 OR instr(lower(asset_text),?)>0)")
                args.extend((term,term))
            where=base+(" AND ("+" OR ".join(clauses)+")" if clauses else "")
            totals=conn.execute("SELECT COUNT(*) AS count,MIN(timestamp) AS earliest,MAX(timestamp) AS latest FROM records WHERE "+where,args).fetchone()
            records={}
            excerpts={}
            def take(uid,span=None):
                row=conn.execute("SELECT * FROM records WHERE scope=? AND id=? AND timestamp>=? AND timestamp<?",(key,uid,start,end)).fetchone()
                if row:
                    records[uid]=json.loads(row["payload"])
                    if span is not None:
                        excerpts.setdefault(uid,[]).append(span)
            boundaries=[]
            for order in (direction,"DESC" if oldest else "ASC"):
                row=conn.execute("SELECT id FROM records WHERE "+where+f" ORDER BY timestamp {order},seq {order},id {order} LIMIT 1",args).fetchone()
                if row and row["id"] not in boundaries:
                    boundaries.append(row["id"])
                    take(row["id"])
            if limit is None:
                # Count and all matching payloads share this frozen snapshot.
                # No ranking, excerpt or hit-count cap may omit exact matches.
                matches=[]
                for row in conn.execute("SELECT payload FROM records WHERE "+where+
                        " ORDER BY timestamp ASC,seq ASC,id ASC",args):
                    if stop is not None and stop.is_set():
                        raise RuntimeError("已停止检索，本次未发送原文分析请求")
                    matches.append(json.loads(row["payload"]))
                matches,passages,context_count=self._with_context(conn,key,start,end,matches,stop)
                stats={"count":totals["count"],"is_exact":True,"limited_by_message_cap":False,
                    "context_count":context_count,
                    "earliest_time":display_time(totals["earliest"]) if totals["earliest"] is not None else None,
                    "latest_time":display_time(totals["latest"]) if totals["latest"] is not None else None}
                return {"coverage":coverage,"statistics":stats,"records":matches,"excerpts":{},
                    "boundaries":boundaries,"generation":entry["generation"],"terms":terms,
                    "passages":passages,"context_policy":dict(CONTEXT_POLICY)}
            if terms:
                chunk_args=[key,start,end]
                if self.fts and all(len(t)>=3 for t in terms):
                    query=" OR ".join('"'+t.replace('"','""')+'"' for t in terms)
                    sql="""SELECT c.* FROM chunks c JOIN rag_fts ON rag_fts.rowid=c.rowid
                        WHERE c.scope=? AND c.last_timestamp>=? AND c.timestamp<? AND rag_fts MATCH ?
                        ORDER BY bm25(rag_fts),c.timestamp DESC LIMIT ?"""
                    chunk_args.extend((query,limit))
                else:
                    sql="SELECT * FROM chunks WHERE scope=? AND last_timestamp>=? AND timestamp<? AND ("+" OR ".join("instr(lower(text),?)>0" for _ in terms)+f") ORDER BY timestamp {direction} LIMIT ?"
                    chunk_args.extend((*terms,limit))
                chunks=conn.execute(sql,chunk_args).fetchall()
            else:
                chunks=conn.execute("SELECT * FROM chunks WHERE scope=? AND last_timestamp>=? AND timestamp<? ORDER BY timestamp "+direction+" LIMIT ?",(key,start,end,limit)).fetchall()
            for chunk in chunks:
                for part in json.loads(chunk["parts"]):
                    take(part["id"],(part["start"],part["end"]))
            # Add immediate neighboring events inside the same frozen index snapshot.
            for record in list(records.values())[:40]:
                for op,order in (("<","DESC"),(">","ASC")):
                    for row in conn.execute(f"SELECT id FROM records WHERE {base} AND (timestamp,seq,id){op}(?,?,?) ORDER BY timestamp {order},seq {order},id {order} LIMIT 1",
                            (key,start,end,record["timestamp"],record["seq"],record["id"])):
                        take(row["id"])
            stats={"count":totals["count"],"is_exact":True,"limited_by_message_cap":False,
                "earliest_time":display_time(totals["earliest"]) if totals["earliest"] is not None else None,
                "latest_time":display_time(totals["latest"]) if totals["latest"] is not None else None}
        return {"coverage":coverage,"statistics":stats,"records":list(records.values()),"excerpts":excerpts,
            "boundaries":boundaries,"generation":entry["generation"],"terms":terms}

    @staticmethod
    def _with_context(conn,key,start,end,matches,stop):
        """Read full neighbors in the same transaction as all exact matches."""
        if not matches:
            return [],[],0
        def check():
            if stop is not None and stop.is_set():
                raise RuntimeError("已停止检索，本次未发送原文分析请求")
        matched_ids={row["id"] for row in matches}
        by_id={row["id"]:row for row in matches}
        # Only metadata is scanned globally. Fetch complete payloads solely for
        # added neighbors, in bounded SQL batches, never one query per match.
        metadata=[]
        for row in conn.execute("SELECT id,timestamp,seq FROM records WHERE scope=? AND timestamp>=? AND timestamp<? ORDER BY timestamp,seq,id",(key,start,end)):
            check()
            metadata.append(dict(row))
        windows=conversation_windows(metadata,matched_ids,check)
        extra_ids=[metadata[i]["id"] for left,right in windows for i in range(left,right)
                   if metadata[i]["id"] not in matched_ids]
        for offset in range(0,len(extra_ids),400):
            check()
            batch=extra_ids[offset:offset+400]
            for row in conn.execute("SELECT id,payload FROM records WHERE scope=? AND timestamp>=? AND timestamp<? AND id IN ("+
                    ",".join("?" for _ in batch)+")",(key,start,end,*batch)):
                by_id[row["id"]]=json.loads(row["payload"])
        records=[]
        passages=[]
        for left,right in windows:
            check()
            passage={"ref":f"C{len(passages)+1}","message_ids":[],"matched_ids":[]}
            for position in range(left,right):
                check()
                uid=metadata[position]["id"]
                row=dict(by_id[uid],retrieval_role="match" if uid in matched_ids else "context",passage_ref=passage["ref"])
                records.append(row)
                passage["message_ids"].append(uid)
                if uid in matched_ids:
                    passage["matched_ids"].append(uid)
            passages.append(passage)
        if sum(row["retrieval_role"]=="match" for row in records)!=len(matched_ids):
            raise RuntimeError("上下文扩展未能保留全部匹配原文，本次未发送原文分析请求")
        return records,passages,len(records)-len(matched_ids)
