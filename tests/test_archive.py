import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from storage import Store,epoch
from archive import missing_ranges,covered_ranges,mark_covered,export_dirty_days,archive_dir
from media_approval import sync_range
from deepseek_api import Client


def row(i,day="2026-10-10",content="测试正文",kind="文本"):
    return {"local_id":i,"shard":"part0","sort_seq":i,"create_time":epoch(day),
        "sender_name":"测试成员","type":kind,"content":content}


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        folder=ROOT/"work"/"test-data"
        folder.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=folder)
        self.store=Store(Path(self.temp.name)/"messages.sqlite3")
        self.groups=[{"username":"a@chatroom","name":"测试群"},{"username":"person","name":"测试联系人"}]
        self.store.set_selection("account",self.groups,{"a@chatroom"},epoch("2026-10-01"))
        self.chat=Store.chat_id("account","a@chatroom")
        self.store.set_scope(self.chat,epoch("2026-10-10"),epoch("2026-10-10",True))
        self.accesses=[]
        self.rows=[row(1)]
        test=self
        class Reader:
            account="account"
            def refresh(inner):
                pass
            def message_batches(inner,user,start,end,page_size):
                test.accesses.append((user,start,end))
                yield [r for r in test.rows if start<=r["create_time"]<end]
            def close(inner):
                test.accesses.append("close")
        self.reader=Reader

    def tearDown(self):
        self.temp.cleanup()

    def sync(self,**kwargs):
        return sync_range(self.store,self.reader,vision_enabled=False,now=epoch("2026-10-15"),**kwargs)

    def test_covered_history_reuses_text_without_opening_wechat_and_extends_only_missing_days(self):
        self.assertEqual(self.sync()["added"],1)
        path=archive_dir(self.store,self.chat)/"2026-10-10.txt"
        before=path.stat().st_mtime_ns
        self.assertIn("北京时间（UTC+8）",path.read_text(encoding="utf-8"))
        def no_source():
            self.fail("Covered history must not connect to WeChat")
        result=sync_range(self.store,no_source,vision_enabled=False,now=epoch("2026-10-15"))
        self.assertTrue(result["reused"])
        self.assertEqual(path.stat().st_mtime_ns,before)
        self.rows.extend([row(2,"2026-10-09"),row(3,"2026-10-11")])
        self.store.set_scope(self.chat,epoch("2026-10-09"),epoch("2026-10-11",True))
        self.accesses.clear()
        result=self.sync()
        self.assertEqual(result["added"],2)
        self.assertEqual([x[1:] for x in self.accesses if x!="close"],
            [(epoch("2026-10-09"),epoch("2026-10-10")),(epoch("2026-10-11"),epoch("2026-10-12"))])
        self.assertEqual(path.stat().st_mtime_ns,before)
        self.assertEqual(len(covered_ranges(self.store,self.chat)),1)

    def test_overlapping_force_imports_deduplicate_and_write_corrected_text(self):
        self.sync()
        self.rows=[row(1,content="更正后的正文"),row(2)]
        result=self.sync(force=True)
        self.assertEqual(result["added"],1)
        text=(archive_dir(self.store,self.chat)/"2026-10-10.txt").read_text(encoding="utf-8")
        self.assertEqual(text.count("消息ID："),2)
        self.assertIn("更正后的正文",text)
        self.assertEqual(self.store.stats()[0]["count"],2)

    def test_failed_range_keeps_partial_text_but_does_not_mark_complete(self):
        class Broken(self.reader):
            def message_batches(inner,*args):
                yield [row(1)]
                raise RuntimeError("模拟分片失败")
        result=sync_range(self.store,Broken,vision_enabled=False,now=epoch("2026-10-15"))
        self.assertTrue(result["errors"])
        self.assertEqual(covered_ranges(self.store,self.chat),[])
        self.assertTrue((archive_dir(self.store,self.chat)/"2026-10-10.txt").exists())
        self.assertEqual(self.sync()["added"],0)
        self.assertEqual(missing_ranges(self.store,self.chat,*self.store.scope[1:]),[])

    def test_text_write_failure_retains_dirty_queue_and_range_can_retry(self):
        with patch("archive.Path.replace",side_effect=PermissionError("模拟文本被占用")):
            with self.assertRaises(PermissionError):
                self.sync()
        self.assertEqual(covered_ranges(self.store,self.chat),[])
        self.assertEqual(export_dirty_days(self.store),1)
        self.assertEqual(self.sync()["added"],0)
        self.assertEqual(self.store.stats()[0]["count"],1)

    def test_scope_constrains_search_neighbors_statistics_images_and_api_evidence(self):
        self.store.put(self.chat,[row(1,"2026-10-09","OUTSIDE_DATE"),row(2),row(3,"2026-10-11","AFTER_DATE"),row(4,"2026-10-11",kind="图片")])
        other=Store.chat_id("account","person")
        self.store.put(other,[row(1,content="OTHER_CHAT")])
        self.assertEqual(self.store.stats()[0]["count"],1)
        hit=self.store.search([])["messages"][0]
        self.assertEqual(len(self.store.neighbors(hit["id"],10)["messages"]),1)
        self.assertEqual(self.store.pending_images("account"),[])
        self.assertEqual(self.store.image_status(),[])
        calls=[]
        def transport(payload):
            calls.append(payload)
            text=json.dumps({"keywords":[],"chat_names":[],"start_date":"2000-01-01","end_date":"2099-01-01"}) if len(calls)==1 else "测试回答 [M1]"
            return {"choices":[{"message":{"content":text},"finish_reason":"stop"}]}
        answer=Client("fake-key",transport=transport).answer_retrieved(self.store,"总结")
        sent=json.dumps(calls)
        for secret in ("OUTSIDE_DATE","AFTER_DATE","OTHER_CHAT"):
            self.assertNotIn(secret,sent)
        self.assertEqual(len(answer["sources"]),1)

    def test_current_day_tail_catches_delays_and_never_marks_future_time(self):
        today=epoch("2026-10-10")
        first=today+3600
        self.sync_live=lambda now:sync_range(self.store,self.reader,vision_enabled=False,now=now)
        self.sync_live(first)
        self.assertEqual(covered_ranges(self.store,self.chat)[0]["end"],first+1)
        late=row(2)
        late["create_time"]=first-20
        self.rows.append(late)
        self.accesses.clear()
        self.assertEqual(self.sync_live(first+60)["added"],1)
        self.assertEqual(self.accesses[0][1],first+1-300)
        self.assertTrue(missing_ranges(self.store,self.chat,today,today+86400))

    def test_image_description_export_is_durable_and_no_repeated_vlm_or_decryption(self):
        from PIL import Image
        import random
        data=io.BytesIO()
        Image.frombytes("RGB",(200,200),random.Random(3).randbytes(120000)).save(data,format="PNG")
        self.rows=[row(1,kind="图片",content="[图片]")]
        calls=[]
        assets=[]
        class Pictures(self.reader):
            def image_asset(inner,*args):
                assets.append(args)
                return {"bytes":data.getvalue()}
        def transport(payload):
            calls.append(payload)
            return {"choices":[{"message":{"content":"预算表，合计120万元。"},"finish_reason":"stop"}]}
        client=Client("fake-key",transport=transport)
        result=sync_range(self.store,Pictures,client_factory=lambda:client,now=epoch("2026-10-15"))
        self.assertEqual(result["vision"]["done"],1)
        text=(archive_dir(self.store,self.chat)/"2026-10-10.txt").read_text(encoding="utf-8")
        self.assertIn("图片解读（AI 生成）",text)
        self.assertIn("合计120万元",text)
        def no_source():
            self.fail("Completed image must not be decrypted again")
        sync_range(self.store,no_source,client_factory=lambda:client,now=epoch("2026-10-15"))
        self.assertEqual(len(calls),1)
        self.assertEqual(len(assets),1)

    def test_ui_switches_to_one_chat_and_local_query_does_not_open_wechat(self):
        import tkinter as tk
        from types import SimpleNamespace
        from app import App
        root=tk.Tk()
        root.withdraw()
        def no_source(*args,**kwargs):
            self.fail("Local browsing/query must not connect to WeChat")
        app=App(root,Path(self.temp.name)/"ui",auto=False,source_factory=no_source)
        try:
            app.groups=self.groups
            app.account="account"
            app.loaded=True
            app.hide_small.set(False)
            app.since.set("2026-10-10")
            app.until.set("2026-10-10")
            app.render_groups()
            app.tree.focus("0")
            app.toggle_group(SimpleNamespace())
            self.assertEqual(app.selected_names,{"a@chatroom"})
            app.chat_kind.set("个人")
            app.render_groups()
            app.tree.focus("1")
            app.toggle_group(SimpleNamespace())
            self.assertEqual(app.selected_names,{"person"})
            app.store.put(app.store.scope[0],[row(1)])
            app.show_local(persist=True)
            self.assertIn("测试正文",app.sources_text.get("1.0","end"))
            def transport(payload):
                content=json.dumps({"keywords":[],"chat_names":[],"start_date":None,"end_date":None}) if "JSON" in payload["messages"][0]["content"] else "本地回答 [M1]"
                return {"choices":[{"message":{"content":content},"finish_reason":"stop"}]}
            app.make_client=lambda:Client("fake-key",transport=transport)
            app.question.insert("1.0","总结")
            app.ask()
            import time
            deadline=time.monotonic()+5
            while app.busy and time.monotonic()<deadline:
                root.update()
                time.sleep(.01)
            self.assertFalse(app.busy)
            self.assertIn("本地回答",app.answer_text.get("1.0","end"))
        finally:
            for timer in root.tk.call("after","info"):
                root.after_cancel(timer)
            root.destroy()

    def test_daily_preview_pages_are_bounded_and_do_not_change_search_scope(self):
        import tkinter as tk
        from app import App
        root=tk.Tk()
        root.withdraw()
        def no_source(*args,**kwargs):
            self.fail("Daily preview must read local data only")
        app=App(root,Path(self.temp.name)/"daily-ui",auto=False,source_factory=no_source)
        try:
            app.groups,app.account,app.loaded=self.groups,"account",True
            app.selected_names={"a@chatroom"}
            app.since.set("2026-10-10")
            app.until.set("2026-10-12")
            app.prepare_scope()
            chat=app.store.scope[0]
            app.store.put(chat,[row(i,"2026-10-12",f"分页正文-{i:03d}") for i in range(1,406)])
            app.store.put(chat,[row(500,"2026-10-10","较早一天")])
            app.store.put(chat,[row(600,"2026-10-13","范围外消息")])
            scope=app.store.scope
            app.show_local()
            self.assertEqual(app.preview_day.get(),"2026-10-12")
            self.assertEqual(list(app.preview_counts),["2026-10-10","2026-10-12"])
            text=app.sources_text.get("1.0","end")
            self.assertEqual(text.count("分页正文-"),200)
            self.assertIn("分页正文-001",text)
            self.assertNotIn("分页正文-201",text)
            app.move_preview_page(1)
            text=app.sources_text.get("1.0","end")
            self.assertEqual(text.count("分页正文-"),200)
            self.assertNotIn("分页正文-200",text)
            self.assertIn("分页正文-201",text)
            app.move_preview_page(1)
            self.assertEqual(app.sources_text.get("1.0","end").count("分页正文-"),5)
            self.assertIn("分页正文-405",app.raw_sources_text.get("1.0","end"))
            app.last_answer={"answer":"保留的回答"}
            app.move_preview_day(-1)
            self.assertEqual(app.preview_page_index,0)
            self.assertIn("较早一天",app.sources_text.get("1.0","end"))
            self.assertNotIn("分页正文",app.raw_sources_text.get("1.0","end"))
            self.assertEqual(app.store.scope,scope)
            self.assertEqual(app.last_answer["answer"],"保留的回答")
            self.assertEqual(app.store.preview_page("2026-10-13"),[])
            app.store.put(chat,[row(501,"2026-10-10","超长文本"+"a"*170000)])
            app.show_local()
            self.assertIn("预览达到长度上限",app.sources_text.get("1.0","end"))
            self.assertLess(len(app.raw_sources_text.get("1.0","end")),20000)
        finally:
            for timer in root.tk.call("after","info"):
                root.after_cancel(timer)
            root.destroy()

    def test_source_date_queries_cover_shards_beijing_boundaries_and_milliseconds(self):
        import hashlib
        from wechat_source import Source
        from wechatauto.db import WeChatDB
        start,end=self.store.scope[1:]
        table="Msg_"+hashlib.md5(b"a@chatroom").hexdigest()
        paths=[]
        for shard in range(2):
            path=Path(self.temp.name)/f"message_{shard}.db"
            conn=sqlite3.connect(path)
            try:
                conn.execute("CREATE TABLE Name2Id(user_name TEXT)")
                conn.execute("INSERT INTO Name2Id VALUES(?)",(f"member_{shard}",))
                conn.execute(f"CREATE TABLE {table}(local_id INTEGER,local_type INTEGER,real_sender_id INTEGER,create_time INTEGER,message_content TEXT,source TEXT,packed_info_data BLOB,compress_content BLOB,server_id INTEGER,sort_seq INTEGER)")
                scale=1000 if shard else 1
                for i,stamp in enumerate((start-1,start,end-1,end),1):
                    conn.execute(f"INSERT INTO {table} VALUES(?,1,1,?,'正文','',NULL,NULL,0,?)",(i,stamp*scale,i))
                conn.commit()
            finally:
                conn.close()
            paths.append(path)
        handles=[]
        def connections(user):
            self.assertEqual(user,"a@chatroom")
            result=[]
            for path in paths:
                conn=sqlite3.connect(path)
                conn.row_factory=sqlite3.Row
                handles.append(conn)
                result.append((conn,table))
            return result
        db=WeChatDB.__new__(WeChatDB)
        db.unkeyed=[]
        db._msg_conns=connections
        db.get_nickname=lambda username:"成员"+username[-1]
        source=Source.__new__(Source)
        source.db,source.nicknames=db,{}
        batches=list(source.message_batches("a@chatroom",start,end,page_size=1))
        rows=[r for batch in batches for r in batch]
        self.assertEqual(len(rows),4)
        self.assertEqual({r["sender_name"] for r in rows},{"成员0","成员1"})
        self.assertEqual(self.store.put(self.chat,rows),4)
        self.assertEqual(self.store.stats()[0]["count"],4)
        for conn in handles:
            with self.assertRaises(sqlite3.ProgrammingError):
                conn.execute("SELECT 1")
        db.unkeyed=["message/message_2.db"]
        with self.assertRaises(RuntimeError):
            list(source.message_batches("a@chatroom",start,end))


if __name__=="__main__":
    unittest.main()
