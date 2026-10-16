import hashlib
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from chat_list import visible_chats


class ChatListTests(unittest.TestCase):
    def test_thresholds_require_both_and_order_uses_numeric_content_bytes(self):
        groups=[
            {"username":"few@chatroom","name":"条数不够","message_count":999,"content_bytes":100000},
            {"username":"small@chatroom","name":"内容不够","message_count":1000,"content_bytes":51199},
            {"username":"edge@chatroom","name":"边界保留","message_count":1000,"content_bytes":51200},
            {"username":"many@chatroom","name":"条数更多","message_count":5000,"content_bytes":90000},
            {"username":"big@chatroom","name":"内容最大","message_count":1500,"content_bytes":2000000},
        ]
        rows=visible_chats(groups)
        self.assertEqual([index for index,_ in rows],[4,3,2])
        self.assertEqual(len(visible_chats(groups,hide_small=False)),5)
        self.assertEqual([index for index,_ in visible_chats(groups,sort_by="message_count")],[3,4,2])
        self.assertEqual([index for index,_ in visible_chats(groups,term="边界")],[2])

    def test_source_statistics_sum_shards_and_count_utf8_body_bytes(self):
        from wechat_source import Source
        folder=ROOT/"work"/"test-data"
        folder.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=folder) as temp:
            files=[]
            groups=[{"username":"a@chatroom","name":"测试群A"},{"username":"b@chatroom","name":"测试群B"}]
            for index,values in enumerate((("中",None),("文",b"12345"))):
                path=Path(temp)/f"part{index}.db"
                conn=sqlite3.connect(path)
                try:
                    for group in groups:
                        table="Msg_"+hashlib.md5(group["username"].encode()).hexdigest()
                        conn.execute(f'CREATE TABLE "{table}" (message_content BLOB)')
                    first="Msg_"+hashlib.md5(b"a@chatroom").hexdigest()
                    second="Msg_"+hashlib.md5(b"b@chatroom").hexdigest()
                    conn.executemany(f'INSERT INTO "{first}" VALUES(?)',[(value,) for value in values])
                    conn.execute(f'INSERT INTO "{second}" VALUES(?)',(b"x"*20,))
                    conn.commit()
                finally:
                    conn.close()
                files.append(path)
            class FakeDB:
                def _message_dbs(inner):
                    return files
                def _open(inner,path):
                    return sqlite3.connect(path)
            source=Source.__new__(Source)
            source.db=FakeDB()
            result=source.chat_statistics(groups)
            self.assertEqual(result[0]["message_count"],4)
            self.assertEqual(result[0]["content_bytes"],11)
            self.assertEqual(result[1]["message_count"],2)
            self.assertEqual(result[1]["content_bytes"],40)

    def test_sorted_ui_keeps_stable_chat_ids_and_hidden_selections(self):
        import tkinter as tk
        from app import App
        folder=ROOT/"work"/"test-data"
        folder.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=folder) as temp:
            root=tk.Tk()
            root.withdraw()
            app=App(root,Path(temp),auto=False)
            try:
                app.groups=[
                    {"username":"small@chatroom","name":"小群","message_count":10,"content_bytes":1000},
                    {"username":"medium@chatroom","name":"中群","message_count":3000,"content_bytes":100000},
                    {"username":"large@chatroom","name":"大群","message_count":1500,"content_bytes":200000},
                ]
                app.selected_names={"small@chatroom"}
                app.render_groups()
                self.assertEqual(app.tree.get_children(),("2","1"))
                self.assertIn("仍保留选择",app.selection_label.get())
                self.assertEqual(app.selected_names,{"small@chatroom"})
                app.hide_small.set(False)
                app.change_list_view()
                self.assertEqual(app.tree.get_children(),("2","1","0"))
                import json
                config=json.loads((Path(temp)/"settings.json").read_text(encoding="utf-8"))
                self.assertFalse(config["hide_small_chats"])
                self.assertEqual(config["chat_sort_by"],"content_bytes")
            finally:
                for timer in root.tk.call("after","info"):
                    root.after_cancel(timer)
                root.destroy()


if __name__=="__main__":
    unittest.main(verbosity=2)
