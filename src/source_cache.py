"""Local file inventory and failed-decode cache; no source writes or network."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3


class SourceCache:
    VERSION="local-decode-v2"

    def __init__(self, root=None):
        self.path=Path(root)/"source-cache.sqlite3" if root is not None else None
        self.memory={}
        if self.path:
            self.path.parent.mkdir(parents=True,exist_ok=True)
            with self.connect() as conn:
                conn.executescript("""
                    CREATE TABLE IF NOT EXISTS directories(path TEXT PRIMARY KEY,stamp INTEGER,entries TEXT);
                    CREATE TABLE IF NOT EXISTS failures(path TEXT,tag TEXT,stamp TEXT,error TEXT,PRIMARY KEY(path,tag));
                """)

    @contextmanager
    def connect(self):
        conn=sqlite3.connect(self.path,timeout=30)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def files(self, root):
        root=Path(root)
        if not self.path:
            yield from (p for p in root.rglob("*") if p.is_file())
            return
        with self.connect() as conn:
            stack=[root]
            while stack:
                folder=stack.pop()
                try:
                    stamp=folder.stat().st_mtime_ns
                except OSError:
                    continue
                key=os.path.normcase(str(folder.resolve()))
                saved=conn.execute("SELECT stamp,entries FROM directories WHERE path=?",(key,)).fetchone()
                if saved and saved[0]==stamp:
                    entries=json.loads(saved[1])
                else:
                    try:
                        with os.scandir(folder) as listing:
                            entries=[(e.name,"d" if e.is_dir(follow_symlinks=False) else "f")
                                for e in listing if not e.is_symlink()]
                    except OSError:
                        continue
                    conn.execute("INSERT OR REPLACE INTO directories VALUES(?,?,?)",(key,stamp,json.dumps(entries)))
                for name,kind in entries:
                    child=folder/name
                    if kind=="d":
                        stack.append(child)
                    else:
                        yield child

    def identity(self,path,tag):
        path=Path(path)
        info=path.stat()
        key=os.path.normcase(str(path.resolve()))
        stamp=f"{info.st_size}:{info.st_mtime_ns}:{self.VERSION}"
        return key,hashlib.sha256(tag.encode()).hexdigest(),stamp

    def failure(self,path,tag,*,retry=False):
        key,tag,stamp=self.identity(path,tag)
        identity=(key,tag,stamp)
        if identity in self.memory:
            return self.memory[identity]
        error=None
        if self.path and not retry:
            with self.connect() as conn:
                row=conn.execute("SELECT stamp,error FROM failures WHERE path=? AND tag=?",(key,tag)).fetchone()
            if row and row[0]==stamp:
                error=row[1]
        self.memory[identity]=error
        return error

    def remember_failure(self,path,tag,error):
        key,tag,stamp=self.identity(path,tag)
        self.memory[(key,tag,stamp)]=error
        if self.path:
            with self.connect() as conn:
                conn.execute("INSERT OR REPLACE INTO failures VALUES(?,?,?,?)",(key,tag,stamp,error))
