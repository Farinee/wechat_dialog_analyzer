import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
sys.path.insert(0,str(ROOT))
from message_text import compact_reply
from storage import Store,epoch
from archive import export_dirty_days,archive_dir
from convert_replies import convert_text

XML=('wxid_test:\n<?xml version="1.0"?><msg><appmsg><title>+1</title><type>57</type>'
    '<gameshare><duration>-1</duration></gameshare><refermsg><type>1</type><svrid>123456</svrid>'
    '<chatusr>member</chatusr><displayname>示例老师</displayname><content>\n主要是六点才下班</content>'
    '<msgsource>&lt;msgsource&gt;private-signature&lt;/msgsource&gt;</msgsource>'
    '<createtime>1792054286</createtime></refermsg></appmsg><fromusername>wxid_test</fromusername></msg>')
EXPECTED="[引用回复]\n+1\n引用 示例老师：\n主要是六点才下班"


class ReplyTests(unittest.TestCase):
    def test_reply_keeps_author_reply_and_quote_without_transport_metadata(self):
        self.assertEqual(compact_reply("文件/链接/卡片",XML),EXPECTED)
        self.assertEqual(compact_reply("引用回复",XML),EXPECTED)
        cdata=XML.replace("<title>+1</title>","<title><![CDATA[同意 & 谢谢]]></title>")
        self.assertIn("同意 & 谢谢",compact_reply("文件/链接/卡片",cdata))

    def test_text_quoted_xml_and_other_cards_are_not_changed(self):
        self.assertIsNone(compact_reply("文本",XML))
        self.assertIsNone(compact_reply("文件/链接/卡片",XML.replace("<type>57</type>","<type>5</type>",1)))
        for bad in (XML[:-10],XML.replace("<msg>",'<!DOCTYPE msg [<!ENTITY x "boom">]><msg>'),
                    XML.replace("+1","x"*530000)):
            self.assertIsNone(compact_reply("文件/链接/卡片",bad))

    def test_quoted_media_is_a_placeholder_and_nested_replies_are_bounded(self):
        media=XML.replace("<refermsg><type>1</type>","<refermsg><type>3</type>")
        media=media.replace("主要是六点才下班","&lt;msg&gt;&lt;img aeskey='private'/&gt;&lt;/msg&gt;")
        self.assertEqual(compact_reply("文件/链接/卡片",media),"[引用回复]\n+1\n引用 示例老师：\n[图片]")
        import html
        nested=XML.replace("<refermsg><type>1</type>","<refermsg><type>49</type>")
        nested=nested.replace("\n主要是六点才下班",html.escape(XML.split("\n",1)[1]))
        text=compact_reply("文件/链接/卡片",nested)
        self.assertEqual(text.count("[引用回复]"),2)
        self.assertNotIn("msgsource",text)

    def test_import_migration_raw_roundtrip_search_and_exports(self):
        folder=ROOT/"work"/"test-data"
        folder.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=folder) as temp:
            path=Path(temp)/"messages.sqlite3"
            store=Store(path)
            store.set_selection("test",[{"username":"group","name":"测试群"}],{"group"},epoch("2026-10-01"))
            chat=store.selected()[0]["id"]
            row={"local_id":1,"shard":"a","sort_seq":1,"create_time":epoch("2026-10-10"),
                "sender_name":"成员","type":"文件/链接/卡片","content":XML}
            store.put(chat,[row])
            hit=store.search(["六点"])["messages"][0]
            self.assertEqual(hit["kind"],"引用回复")
            self.assertEqual(hit["content"],EXPECTED)
            self.assertEqual(store.raw_payload(hit["id"]),XML)
            self.assertNotIn("private-signature",json.dumps(store.search([])))
            with store.connect() as conn:
                conn.execute("UPDATE messages SET content=?,kind='文件/链接/卡片'",(XML,))
                conn.execute("DELETE FROM message_payloads")
                conn.execute("DELETE FROM archive_meta WHERE key='reply_compact_v1'")
            store=Store(path)
            self.assertEqual(store.raw_payload(hit["id"]),XML)
            self.assertEqual(export_dirty_days(store),1)
            text=(archive_dir(store,chat)/"2026-10-10.txt").read_text(encoding="utf-8")
            self.assertIn(EXPECTED,text)
            self.assertNotIn("msgsource",text)
            self.assertEqual(store.put(chat,[row]),0)
            Store(path)
            self.assertEqual(export_dirty_days(store),0)
            self.assertEqual(store.search(["private-signature"])["messages"],[])
            self.assertEqual(store.search(["六点"])["messages"][0]["id"],hit["id"])

    def test_standalone_script_preserves_transcript_headers_and_other_messages(self):
        self.assertEqual(convert_text(XML),(EXPECTED,1))
        transcript="[2026-10-15 16:51:44] Q · 文件/链接/卡片\n"+XML+"\n[下一条] 普通文本"
        result,count=convert_text(transcript)
        self.assertEqual(count,1)
        self.assertIn(EXPECTED,result)
        self.assertIn("[下一条] 普通文本",result)
        self.assertIn("[2026-10-15 16:51:44] Q",result)
        self.assertNotIn("wxid_test",result)


if __name__=="__main__":
    unittest.main()
