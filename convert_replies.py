"""Compact WeChat type-57 replies from a UTF-8 file or migrate the local archive."""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime
import json
from pathlib import Path
import re
import sqlite3
import sys
import uuid

ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT/"src"))
from message_text import compact_reply
from storage import Store,TZ
from archive import export_dirty_days


def convert_text(text):
    """Accept a single XML body, or message XML blocks in a text transcript."""
    compact=compact_reply("文件/链接/卡片",text)
    if compact is not None:
        return compact,1
    count=0
    pattern=re.compile(r"(?:^[A-Za-z0-9_.@-]{1,128}:\r?\n)?(?:<\?xml[^>]*\?>\s*)?<msg\b[^>]*>[\s\S]*?</msg>",re.M)
    def replace(match):
        nonlocal count
        value=compact_reply("文件/链接/卡片",match[0])
        if value is None:
            return match[0]
        count+=1
        return value
    return pattern.sub(replace,text),count


def migrate(data):
    """Back up before opening Store, whose idempotent migration preserves raw XML."""
    path=data/"messages.sqlite3"
    if not path.is_file():
        raise ValueError("本地消息数据库不存在")
    folder=data/"backups"
    folder.mkdir(parents=True,exist_ok=True)
    backup=folder/("messages-before-replies-"+datetime.now(TZ).strftime("%Y%m%d-%H%M%S")+"-"+uuid.uuid4().hex[:8]+".sqlite3")
    with closing(sqlite3.connect(path.as_uri()+"?mode=ro",uri=True)) as source:
        with closing(sqlite3.connect(backup)) as target:
            source.backup(target)
            before=target.execute("SELECT COUNT(*),COALESCE(SUM(length(content)),0) FROM messages").fetchone()
            old_replies=target.execute("SELECT COUNT(*) FROM messages WHERE kind='引用回复'").fetchone()[0]
    store=Store(path)
    files=export_dirty_days(store)
    with store.connect() as conn:
        after=conn.execute("SELECT COUNT(*),COALESCE(SUM(length(content)),0) FROM messages").fetchone()
        replies=conn.execute("SELECT COUNT(*) FROM messages WHERE kind='引用回复'").fetchone()[0]
        integrity=conn.execute("PRAGMA quick_check").fetchone()[0]
        foreign_keys=len(conn.execute("PRAGMA foreign_key_check").fetchall())
    return {"backup":str(backup),"records_before":before[0],"records_after":after[0],
        "replies_converted":replies-old_replies,"text_characters_saved":before[1]-after[1],
        "date_files_updated":files,"integrity":integrity,"foreign_key_errors":foreign_keys}


def main():
    parser=argparse.ArgumentParser(description="将微信引用回复 XML 转为可检索文字，原始数据库资料保留。")
    mode=parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--input",type=Path,help="UTF-8 XML 或文本输入文件")
    mode.add_argument("--migrate",action="store_true",help="备份并转换本地存档，刷新按天文本")
    parser.add_argument("--output",type=Path,help="转换结果文件；须配合 --input，且不得覆盖输入文件")
    parser.add_argument("--data-dir",type=Path,default=ROOT/"data")
    args=parser.parse_args()
    if args.input:
        if not args.output or args.input.resolve()==args.output.resolve():
            parser.error("请用 --output 指定一个不同的输出文件，保留原始输入")
        result,count=convert_text(args.input.read_text(encoding="utf-8-sig"))
        args.output.write_text(result,encoding="utf-8")
        print(json.dumps({"replies_converted":count,"output":str(args.output)},ensure_ascii=False))
    else:
        print(json.dumps(migrate(args.data_dir.resolve()),ensure_ascii=False))


if __name__=="__main__":
    main()
