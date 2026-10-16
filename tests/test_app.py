import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PROJECT_ROOT/"src"))
from storage import Store,sync_groups,epoch,display_time,display_sync_time
from deepseek_api import Client
from secrets_local import save_secret,load_secret


def row(seq,content="确定交付日期",lid=None,shard="a",stamp=None):
    return {"sort_seq":seq,"local_id":lid if lid is not None else seq,
        "shard":shard,"create_time":stamp or epoch("2026-10-10"),
        "type":"文本","content":content,"sender_name":"测试发言人"}


class Reader:
    account="account-test"
    def __init__(self,rows):
        self.rows=rows
    def refresh(self):
        pass
    def messages(self,user,limit,offset):
        return self.rows[offset:offset+limit]


class Tests(unittest.TestCase):
    def setUp(self):
        test_root=PROJECT_ROOT/"work"/"test-data"
        test_root.mkdir(parents=True,exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(dir=test_root)
        self.store=Store(Path(self.tmp.name)/"test.db")
        self.groups=[{"username":"a@chatroom","name":"项目群"},
            {"username":"b@chatroom","name":"非选中群"}]
        self.store.set_selection("account-test",self.groups,{"a@chatroom"},epoch("2026-10-01"))
        self.chat=self.store.selected()[0]["id"]

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def close_ui(root):
        for timer in root.tk.call("after","info"):
            root.after_cancel(timer)
        root.destroy()

    def test_scope_chinese_search_and_date(self):
        self.store.put(self.chat,[row(1,"预算金额确定为1000"),row(2,"下周交付报告")])
        other=Store.chat_id("account-test","b@chatroom")
        self.store.put(other,[row(3,"非选中的预算内容")])
        result=self.store.search(["预算"])
        self.assertEqual(len(result["messages"]),1)
        self.assertNotIn("非选中",result["messages"][0]["content"])
        self.assertEqual(len(self.store.search(["交付报告"])["messages"]),1)
        self.assertEqual(len(self.store.search(["预算"],start_date="2026-10-11")["messages"]),0)
        self.assertEqual(len(self.store.search(["预算"],end_date="2026-10-10")["messages"]),1)
        self.assertEqual(len(self.store.search(["%"])["messages"]),0)
        with self.store.connect() as conn:
            ids=[r[0] for r in conn.execute("SELECT id FROM messages")]
        self.store.set_scope(self.chat,epoch("2026-10-10"),epoch("2026-10-10",end=True))
        originals=self.store.original_records(ids)
        self.assertEqual(len(originals),2)
        self.assertTrue(all(r["chat"]==self.chat for r in originals))
        self.store.set_scope(self.chat,epoch("2026-10-11"),epoch("2026-10-11",end=True))
        self.assertEqual(self.store.original_records(ids),[])

    def test_same_local_id_different_shards_and_dedup(self):
        rows=[row(3,lid=1,shard="a"),row(2,lid=1,shard="b")]
        self.assertEqual(self.store.put(self.chat,rows),2)
        self.assertEqual(self.store.put(self.chat,rows),0)
        self.assertEqual(self.store.stats()[0]["count"],2)

    def test_manual_contact_selection_is_searchable_and_scoped(self):
        groups=self.groups+[{"username":"wxid_test_person","name":"测试联系人","kind":"个人"}]
        self.store.set_selection("account-test",groups,{"wxid_test_person"},epoch("2026-10-01"))
        chat=self.store.selected()[0]["id"]
        self.store.put(chat,[row(1,"确认预算")])
        self.store.put(self.chat,[row(2,"预算但不是已选择的群")])
        result=self.store.search(["预算"])
        self.assertEqual(len(result["messages"]),1)
        self.assertEqual(result["messages"][0]["group_name"],"测试联系人")

    def test_sync_tied_sequences_across_page_boundary(self):
        rows=[row(9,lid=1),row(9,lid=2),row(9,lid=3),row(8,lid=4)]
        self.store.checkpoint(self.chat,9)
        result=sync_groups(self.store,Reader(rows),page_size=2)
        self.assertEqual(result["added"],3)
        self.assertEqual(self.store.stats()[0]["count"],3)
        self.assertEqual(sync_groups(self.store,Reader(rows),page_size=2)["added"],0)

    def test_retry_after_failure_does_not_advance_cursor(self):
        class Failing(Reader):
            def messages(self,user,limit,offset):
                if offset:
                    raise RuntimeError("模拟分片失败")
                return super().messages(user,limit,offset)
        rows=[row(3),row(2),row(1)]
        result=sync_groups(self.store,Failing(rows),page_size=2)
        self.assertTrue(result["errors"])
        self.assertEqual(self.store.selected()[0]["cursor"],0)
        result=sync_groups(self.store,Reader(rows),page_size=2)
        self.assertEqual(result["groups"],1)
        self.assertEqual(self.store.stats()[0]["count"],3)
        self.assertEqual(self.store.selected()[0]["cursor"],3)

    def test_messages_arriving_during_paging_not_skipped_next_launch(self):
        class Growing(Reader):
            def messages(self,user,limit,offset):
                if offset==2 and len(self.rows)==4:
                    self.rows=[row(8),row(7),row(6),row(5)]+self.rows
                return super().messages(user,limit,offset)
        reader=Growing([row(4),row(3),row(2),row(1)])
        sync_groups(self.store,reader,page_size=2)
        self.assertEqual(self.store.selected()[0]["cursor"],4)
        sync_groups(self.store,reader,page_size=2)
        self.assertEqual(self.store.stats()[0]["count"],8)
        self.assertEqual(self.store.selected()[0]["cursor"],8)

    def test_neighbors_are_ordered_and_deselected_groups_excluded(self):
        self.store.put(self.chat,[row(3),row(2),row(1)])
        hit=self.store.search([])["messages"][1]
        nearby=self.store.neighbors(hit["id"],1)["messages"]
        self.assertEqual([r["seq"] for r in nearby],[1,2,3])
        self.store.set_selection("account-test",self.groups,set(),epoch("2026-10-01"))
        self.assertEqual(self.store.search([])["messages"],[])
        self.assertEqual(self.store.neighbors(hit["id"])["messages"],[])

    def test_older_history_selection_resets_checkpoint(self):
        self.store.checkpoint(self.chat,20)
        self.store.set_selection("account-test",self.groups,{"a@chatroom"},epoch("2026-09-01"))
        self.assertEqual(self.store.selected()[0]["cursor"],0)

    def test_deepseek_plan_and_answer_only_selected_evidence(self):
        self.store.put(self.chat,[row(1,"项目交付时间是10月20日")])
        other=Store.chat_id("account-test","b@chatroom")
        self.store.put(other,[row(2,"SECRET_NOT_SELECTED")])
        calls=[]
        def transport(payload):
            calls.append(payload)
            text=json.dumps({"keywords":["交付"],"chat_names":[],"start_date":None,"end_date":None}) if len(calls)==1 else "交付时间是10月20日 [M1]。"
            return {"choices":[{"message":{"content":text},"finish_reason":"stop"}]}
        result=Client("test-token",transport=transport).answer_retrieved(self.store,"交付时间？")
        self.assertEqual(len(calls),2)
        self.assertNotIn("SECRET_NOT_SELECTED",json.dumps(calls,ensure_ascii=False))
        self.assertIn("M1",result["answer"])
        self.assertEqual(result["sources"][0]["group"],"项目群")

    def test_invalid_plan_rejected(self):
        client=Client("test-token",transport=lambda _: {"choices":[{"message":{"content":"{\"keywords\":\"DROP TABLE\"}"}}]})
        with self.assertRaises(RuntimeError):
            client.plan("查询",["项目群"])

    def test_ui_saves_range_then_startup_restores_without_message_sync(self):
        import tkinter as tk
        from app import App,write_config
        data=Path(self.tmp.name)/"ui-data"
        data.mkdir()
        calls=[]
        class FakeSource(Reader):
            def __init__(self,*args,**kwargs):
                super().__init__([row(3),row(2),row(1)])
                calls.append("connect")
            def groups(inner):
                return self.groups
            def close(inner):
                calls.append("close")
        root=tk.Tk()
        root.withdraw()
        app=App(root,data,auto=False,source_factory=FakeSource)
        app.account="account-test"
        app.groups=self.groups
        app.loaded=True
        app.selected_names={"a@chatroom"}
        app.since.set("2026-10-01")
        app.until.set("2026-10-10")
        app.render_groups()
        app.save_and_sync()
        import time
        deadline=time.monotonic()+4
        while app.busy and time.monotonic()<deadline:
            root.update()
            time.sleep(0.01)
        self.assertFalse(app.busy)
        self.assertEqual(app.store.stats()[0]["count"],3)
        self.assertIn("新增 3",app.status.get())
        self.assertEqual(json.loads((data/"settings.json").read_text(encoding="utf-8"))["selected"],["a@chatroom"])
        app.load_groups(True)
        deadline=time.monotonic()+4
        while app.busy and time.monotonic()<deadline:
            root.update()
            time.sleep(0.01)
        self.assertFalse(app.busy)
        self.assertIn("已恢复本地",app.status.get())
        self.assertEqual(calls,["connect","close"])
        (data/"catalog.json").write_text(json.dumps({"account":"account-test","groups":self.groups}),encoding="utf-8")
        app.load_groups(True)
        deadline=time.monotonic()+4
        while app.busy and time.monotonic()<deadline:
            root.update()
            time.sleep(0.01)
        self.assertIn("已恢复本地",app.status.get())
        self.assertEqual(calls,["connect","close"])
        self.assertEqual(app.store.stats()[0]["count"],3)
        self.assertEqual(app.until.get(),"2026-10-10")
        self.close_ui(root)

    def test_windows_encrypted_secret_roundtrip(self):
        path=Path(self.tmp.name)/"secret.bin"
        value=b"not-a-real-api-key-test"
        save_secret(path,value)
        self.assertNotIn(value,path.read_bytes())
        self.assertEqual(load_secret(path),value)

    def test_default_data_root_follows_project_not_working_directory(self):
        from app import application_dir
        import os
        previous=Path.cwd()
        try:
            os.chdir(self.tmp.name)
            self.assertEqual(application_dir(),PROJECT_ROOT)
        finally:
            os.chdir(previous)

    def test_times_are_beijing_even_for_utc_sync_timestamps(self):
        self.assertEqual(display_time(0),"1970-01-01 08:00:00")
        self.assertEqual(epoch("1970-01-01"),-28800)
        self.assertEqual(epoch("1970-01-01",end=True),57600)
        self.assertEqual(display_sync_time("2026-10-15T01:02:03Z"),"2026-10-15 09:02:03")
        self.assertEqual(display_sync_time("2026-10-15T09:02:03+08:00"),"2026-10-15 09:02:03")
        self.assertEqual(display_sync_time(None),"尚未更新")
        from app import App
        group=self.store.stats()[0]
        group["synced_at"]="2026-10-15T01:02:03Z"
        text=App.coverage_text([group])
        self.assertIn("北京时间（UTC+8）",text)
        self.assertIn("2026-10-15 09:02:03",text)
        self.assertNotIn("T01:02:03Z",text)

    def test_name_refresh_preserves_messages_selection_and_progress(self):
        self.store.put(self.chat,[row(20)])
        self.store.checkpoint(self.chat,20)
        before=self.store.stats()[0]
        self.store.refresh_names("account-test",[{"username":"a@chatroom","name":"实际研发群"}])
        after=self.store.stats()[0]
        self.assertEqual(after["name"],"实际研发群")
        for key in ("id","username","selected","cursor","synced_at","since","count"):
            self.assertEqual(after[key],before[key])

    def test_group_names_use_real_names_or_explicit_member_summary(self):
        import sqlite3
        from wechat_source import Source
        path=Path(self.tmp.name)/"contacts.db"
        with sqlite3.connect(path) as conn:
            conn.executescript("""
                CREATE TABLE contact(id INTEGER,username TEXT,nick_name TEXT,remark TEXT);
                CREATE TABLE chat_room(id INTEGER,username TEXT);
                CREATE TABLE chatroom_member(room_id INTEGER,member_id INTEGER);
                INSERT INTO contact VALUES(1,'first@chatroom','实际群名','first@chatroom');
                INSERT INTO contact VALUES(2,'empty@chatroom','','');
                INSERT INTO contact VALUES(3,'wxid_test_member','测试成员','');
                INSERT INTO chat_room VALUES(2,'empty@chatroom');
                INSERT INTO chatroom_member VALUES(2,3);
            """)
        conn.close()
        class ContactReader:
            def _contact_conn(self):
                conn=sqlite3.connect(path)
                conn.row_factory=sqlite3.Row
                return conn
        source=Source.__new__(Source)
        source.db=ContactReader()
        groups={g["username"]:g for g in source.groups()}
        self.assertEqual(groups["first@chatroom"]["name"],"实际群名")
        self.assertTrue(groups["first@chatroom"]["name_resolved"])
        self.assertEqual(groups["empty@chatroom"]["name"],"未命名群（测试成员等）")
        self.assertFalse(groups["empty@chatroom"]["name_resolved"])
        self.assertNotIn("@chatroom",groups["empty@chatroom"]["name"])

    def test_custom_chat_name_is_removed_and_old_override_is_ignored(self):
        import tkinter as tk
        from unittest.mock import patch
        from app import App,write_config
        data=Path(self.tmp.name)/"rename-ui"
        data.mkdir()
        write_config(data,{"name_overrides":{"account-test":{"a@chatroom":"旧别名"}}})
        class FakeSource(Reader):
            def __init__(inner,*args,**kwargs):
                super().__init__([])
            def groups(inner):
                return [{"username":"a@chatroom","name":"未命名群"}]
            def close(inner):
                pass
        root=tk.Tk()
        root.withdraw()
        app=App(root,data,auto=False,source_factory=FakeSource)
        app.load_groups(False)
        import time
        deadline=time.monotonic()+4
        while app.busy and time.monotonic()<deadline:
            root.update()
            time.sleep(0.01)
        self.assertFalse(app.busy)
        self.assertEqual(app.groups[0]["name"],"未命名群")
        self.assertFalse(hasattr(app,"rename_chat"))
        self.assertEqual([app.tabs.tab(tab,"text") for tab in app.tabs.tabs()],["对话","原文"])
        self.assertEqual([app.source_tabs.tab(tab,"text") for tab in app.source_tabs.tabs()],
            ["原始资料层","可检索文字层"])
        self.assertIsNot(app.sources_text,app.coverage_widget)
        self.close_ui(root)


if __name__=="__main__":
    unittest.main(verbosity=2)
