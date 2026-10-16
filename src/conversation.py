"""Persistent conversations isolated by exact selected chat and date range."""
from contextlib import contextmanager
from datetime import datetime
import json
from pathlib import Path
import re
import sqlite3
import uuid
from storage import TZ


class Conversations:
    def __init__(self,path):
        self.path=Path(path)
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY,chat TEXT,start INTEGER,end INTEGER,created TEXT);
                CREATE TABLE IF NOT EXISTS turns(id TEXT PRIMARY KEY,session TEXT REFERENCES sessions(id),created TEXT,payload TEXT);
                CREATE INDEX IF NOT EXISTS turns_session ON turns(session,created,id);
                CREATE TABLE IF NOT EXISTS active(chat TEXT,start INTEGER,end INTEGER,session TEXT,PRIMARY KEY(chat,start,end));
                CREATE TABLE IF NOT EXISTS drafts(session TEXT PRIMARY KEY REFERENCES sessions(id),text TEXT NOT NULL,updated TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS requests(id TEXT PRIMARY KEY,session TEXT NOT NULL REFERENCES sessions(id),
                    question TEXT NOT NULL,status TEXT NOT NULL,created TEXT NOT NULL,updated TEXT NOT NULL,
                    error TEXT NOT NULL DEFAULT '',coverage TEXT NOT NULL DEFAULT '{}');
                CREATE INDEX IF NOT EXISTS requests_session ON requests(session,created);
            """)
            if "partial" not in {row["name"] for row in conn.execute("PRAGMA table_info(requests)")}:
                conn.execute("ALTER TABLE requests ADD COLUMN partial TEXT NOT NULL DEFAULT ''")
            if "partial_reasoning" not in {row["name"] for row in conn.execute("PRAGMA table_info(requests)")}:
                conn.execute("ALTER TABLE requests ADD COLUMN partial_reasoning TEXT NOT NULL DEFAULT ''")

    @contextmanager
    def connect(self):
        conn=sqlite3.connect(self.path,timeout=30)
        conn.row_factory=sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def create(self,scope):
        uid=uuid.uuid4().hex
        with self.connect() as conn:
            conn.execute("INSERT INTO sessions VALUES(?,?,?,?,?)",(uid,*scope,datetime.now(TZ).isoformat(timespec="microseconds")))
            conn.execute("INSERT OR REPLACE INTO active VALUES(?,?,?,?)",(*scope,uid))
        return uid

    def current(self,scope):
        with self.connect() as conn:
            row=conn.execute("SELECT session FROM active WHERE chat=? AND start=? AND end=?",scope).fetchone()
        return row[0] if row else self.create(scope)

    def sessions(self,scope):
        with self.connect() as conn:
            return [dict(r) for r in conn.execute("""SELECT s.id,s.created,
                COALESCE((SELECT json_extract(t.payload,'$.question') FROM turns t WHERE t.session=s.id ORDER BY t.created,t.id LIMIT 1),
                    (SELECT r.question FROM requests r WHERE r.session=s.id ORDER BY r.created,r.id LIMIT 1)) AS title
                FROM sessions s WHERE s.chat=? AND s.start=? AND s.end=? ORDER BY s.created DESC,s.id""",scope)]

    def select(self,scope,uid):
        with self.connect() as conn:
            if not conn.execute("SELECT 1 FROM sessions WHERE id=? AND chat=? AND start=? AND end=?",(uid,*scope)).fetchone():
                raise ValueError("此对话不属于当前聊天和日期范围")
            conn.execute("INSERT OR REPLACE INTO active VALUES(?,?,?,?)",(*scope,uid))

    def turns(self,scope,uid):
        with self.connect() as conn:
            return [dict(json.loads(r["payload"]),turn_id=r["id"],created=r["created"]) for r in conn.execute("""SELECT t.* FROM turns t
                JOIN sessions s ON s.id=t.session WHERE t.session=? AND s.chat=? AND s.start=? AND s.end=? ORDER BY t.created,t.id""",(uid,*scope))]

    def append(self,scope,uid,payload):
        with self.connect() as conn:
            if not conn.execute("SELECT 1 FROM sessions WHERE id=? AND chat=? AND start=? AND end=?",(uid,*scope)).fetchone():
                raise ValueError("保存对话的范围已变化")
            conn.execute("INSERT INTO turns VALUES(?,?,?,?)",(uuid.uuid4().hex,uid,datetime.now(TZ).isoformat(timespec="microseconds"),
                json.dumps(payload,ensure_ascii=False)))

    @staticmethod
    def _check_session(conn,scope,uid):
        if not conn.execute("SELECT 1 FROM sessions WHERE id=? AND chat=? AND start=? AND end=?",(uid,*scope)).fetchone():
            raise ValueError("此对话不属于当前聊天和日期范围")

    def save_draft(self,scope,uid,text):
        with self.connect() as conn:
            self._check_session(conn,scope,uid)
            conn.execute("""INSERT INTO drafts VALUES(?,?,?) ON CONFLICT(session) DO UPDATE SET text=excluded.text,
                updated=excluded.updated WHERE drafts.text!=excluded.text""",
                (uid,text,datetime.now(TZ).isoformat(timespec="microseconds")))

    def draft(self,scope,uid):
        with self.connect() as conn:
            self._check_session(conn,scope,uid)
            row=conn.execute("SELECT text FROM drafts WHERE session=?",(uid,)).fetchone()
        return row[0] if row else ""

    def begin_request(self,scope,uid,question):
        request_id=uuid.uuid4().hex
        now=datetime.now(TZ).isoformat(timespec="microseconds")
        with self.connect() as conn:
            self._check_session(conn,scope,uid)
            conn.execute("INSERT INTO requests(id,session,question,status,created,updated) VALUES(?,?,?,'pending',?,?)",
                (request_id,uid,question,now,now))
            conn.execute("INSERT OR REPLACE INTO drafts VALUES(?,?,?)",(uid,question,now))
        return request_id

    def update_request(self,request_id,*,coverage=None,error=None,partial=None,partial_reasoning=None):
        with self.connect() as conn:
            if partial is not None:
                conn.execute("UPDATE requests SET partial=?,updated=? WHERE id=? AND status='pending'",
                    (partial,datetime.now(TZ).isoformat(timespec="microseconds"),request_id))
            if partial_reasoning is not None:
                conn.execute("UPDATE requests SET partial_reasoning=?,updated=? WHERE id=? AND status='pending'",
                    (partial_reasoning,datetime.now(TZ).isoformat(timespec="microseconds"),request_id))
            if coverage is not None:
                conn.execute("UPDATE requests SET coverage=?,updated=? WHERE id=? AND status='pending'",
                    (json.dumps(coverage,ensure_ascii=False),datetime.now(TZ).isoformat(timespec="microseconds"),request_id))
            if error is not None:
                conn.execute("UPDATE requests SET status='failed',error=?,updated=? WHERE id=? AND status='pending'",
                    (str(error)[:1000],datetime.now(TZ).isoformat(timespec="microseconds"),request_id))

    def complete_request(self,scope,uid,request_id,payload):
        """Commit the reply before rendering; request id makes replay idempotent."""
        now=datetime.now(TZ).isoformat(timespec="microseconds")
        with self.connect() as conn:
            self._check_session(conn,scope,uid)
            row=conn.execute("SELECT status,partial_reasoning FROM requests WHERE id=? AND session=?",(request_id,uid)).fetchone()
            if row is None:
                raise ValueError("待保存的分析请求不存在")
            if row["partial_reasoning"] and not payload.get("reasoning_content"):
                payload=dict(payload,reasoning_content=row["partial_reasoning"])
            conn.execute("INSERT OR IGNORE INTO turns VALUES(?,?,?,?)",
                (request_id,uid,now,json.dumps(payload,ensure_ascii=False)))
            conn.execute("UPDATE requests SET status='done',error='',partial='',partial_reasoning='',updated=? WHERE id=?",(now,request_id))
            conn.execute("DELETE FROM drafts WHERE session=? AND text=?",(uid,payload["question"]))
        return request_id

    def unfinished(self,scope,uid):
        with self.connect() as conn:
            self._check_session(conn,scope,uid)
            row=conn.execute("SELECT * FROM requests WHERE session=? ORDER BY created DESC,id DESC LIMIT 1",(uid,)).fetchone()
        return dict(row) if row and row["status"]!="done" else None

    def export_turns(self,scope,uid):
        """Export every received fragment; incomplete requests stay out of history."""
        with self.connect() as conn:
            conn.execute("BEGIN")
            self._check_session(conn,scope,uid)
            rows=conn.execute("""SELECT t.id,COALESCE(r.created,t.created) AS created,t.payload,
                    '' AS question,'' AS partial,'' AS partial_reasoning,'' AS coverage
                FROM turns t LEFT JOIN requests r ON r.id=t.id AND r.session=t.session WHERE t.session=?
                UNION ALL
                SELECT r.id,r.created,NULL,r.question,r.partial,r.partial_reasoning,r.coverage
                FROM requests r WHERE r.session=? AND r.status!='done'
                    AND (r.partial!='' OR r.partial_reasoning!='')
                    AND NOT EXISTS(SELECT 1 FROM turns t WHERE t.id=r.id)
                ORDER BY created,id""",(uid,uid))
            turns=[]
            for row in rows:
                if row["payload"] is not None:
                    turn=json.loads(row["payload"])
                else:
                    turn=unfinished_turn(dict(row))
                    turn["coverage_text"]=json.loads(row["coverage"]).get("notice","")
                turns.append(dict(turn,turn_id=row["id"],created=row["created"]))
            return turns


def unfinished_turn(request):
    """Recover received API text for display/export without treating it as a turn."""
    if not request or not (request.get("partial") or request.get("partial_reasoning")):
        return None
    return {"question":request["question"],"answer":request.get("partial") or "尚未收到回答正文。",
        "reasoning_content":request.get("partial_reasoning") or "","incomplete":True}


def model_history(turns,limit=10,budget=20000):
    """Bound prior conversation; only user/assistant text, never raw XML or instructions."""
    pairs=[]
    used=0
    for turn in reversed(turns[-limit:]):
        if turn.get("incomplete"):
            continue
        question=str(turn.get("question",""))[:3000]
        answer=str(turn.get("answer",""))[:10000]
        if used+len(question)+len(answer)>budget:
            break
        # Prior citation labels must not masquerade as this round's M identifiers.
        answer=re.sub(r"\[M(\d+)\]",r"[历史引用 \1]",answer)
        pairs.append([{"role":"user","content":question},{"role":"assistant","content":answer}])
        used+=len(question)+len(answer)
    return [message for pair in reversed(pairs) for message in pair]


def export_markdown(turns):
    sections=["# 微信聊天分析助手 · 对话记录"]
    for i,turn in enumerate(turns,1):
        sections.extend([f"\n## 第 {i} 轮 · 你\n",turn["question"]])
        if turn.get("incomplete"):
            sections.append("\n> 未完成：以下仅为已收到并保存的内容，不能视为完整分析。\n")
        reasoning=turn.get("reasoning_content")
        if isinstance(reasoning,str) and reasoning:
            title="模型思考（API 返回，未完成片段）" if turn.get("incomplete") else "模型思考（API 返回）"
            # A longer fence preserves literal model text, even when it contains
            # its own Markdown code fences or headings.
            fence="`"*max(3,1+max((len(m[0]) for m in re.finditer(r"`+",reasoning)),default=0))
            sections.extend([f"\n### {title}\n",fence+"text\n"+reasoning+"\n"+fence])
        sections.extend(["\n## 助手\n",turn["answer"],
            "\n### 本轮引用\n",turn.get("source_text",""),"\n### 本轮范围\n",turn.get("coverage_text","")])
    return "\n".join(sections)
