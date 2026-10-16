import json
from pathlib import Path
import sys
import tempfile
import unittest
import zlib

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from message_text import compact_emoji,emoji_metadata
from storage import Store,epoch
from archive import export_dirty_days,archive_dir

XML = ('<msg><emoji fromusername="fake_user" tousername="test@chatroom" '
    'md5="5f732bf599659318b74d935969f2330d" len="53542" '
    'cdnurl="https://example.invalid/emoji?filekey=only-test&amp;id=123" '
    'aeskey="test-key-never-send" desc="" attachedtext=""></emoji></msg>')


class MessageTextTests(unittest.TestCase):
    def test_empty_description_retains_identity_without_guessing_or_download_metadata(self):
        self.assertEqual(compact_emoji("动画表情",XML),"[表情 #5f732bf59965]")
        described=XML.replace('desc=""','desc="开心 &amp; 好耶"').replace('attachedtext=""','attachedtext="庆祝"')
        self.assertEqual(compact_emoji("动画表情",described),"[表情：开心 & 好耶；庆祝 #5f732bf59965]")
        self.assertIsNone(compact_emoji("文本",XML))
        self.assertIsNone(compact_emoji("动画表情","[动画表情]"))

    def test_malformed_or_entity_xml_is_not_expanded_and_does_not_leak_metadata(self):
        for xml in (XML[:-10], '<!DOCTYPE msg [<!ENTITY x "data">]><msg><emoji desc="&x;"/></msg>',
                    '<msg><emoji desc="'+('x'*300000)+'"/></msg>'):
            # A DOCTYPE can precede a normal emoji message in older exports.
            if xml.startswith("<!DOCTYPE"):
                xml='<?xml version="1.0"?>'+xml
            result=compact_emoji("动画表情",xml)
            self.assertTrue(result.startswith("[表情 #"))
            self.assertLess(len(result),40)

    def test_import_export_and_raw_metadata_roundtrip_remain_deduplicated(self):
        folder=ROOT/"work"/"test-data"
        folder.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=folder) as temp:
            store=Store(Path(temp)/"messages.sqlite3")
            store.set_selection("test",[{"username":"test@chatroom","name":"测试群"}],{"test@chatroom"},epoch("2026-10-01"))
            chat=store.selected()[0]["id"]
            row={"local_id":1,"shard":"a","sort_seq":1,"create_time":epoch("2026-10-10"),"sender_name":"成员","type":"动画表情","content":XML}
            self.assertEqual(store.put(chat,[row]),1)
            self.assertEqual(store.put(chat,[row]),0)
            hit=store.search([])["messages"][0]
            self.assertEqual(hit["content"],"[表情 #5f732bf59965]")
            with store.connect() as conn:
                raw=conn.execute("SELECT payload FROM message_payloads WHERE message_id=?",(hit["id"],)).fetchone()[0]
            self.assertEqual(zlib.decompress(raw).decode(),XML)
            self.assertLess(len(raw),len(XML.encode()))
            export_dirty_days(store)
            text=(archive_dir(store,chat)/"2026-10-10.txt").read_text(encoding="utf-8")
            self.assertIn("[表情 #5f732bf59965]",text)
            self.assertNotIn("cdnurl",text)
            self.assertNotIn("test-key-never-send",json.dumps(store.search([])))

    def test_existing_records_migrate_once_and_update_text_and_search_index(self):
        folder=ROOT/"work"/"test-data"
        folder.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=folder) as temp:
            path=Path(temp)/"messages.sqlite3"
            store=Store(path)
            store.set_selection("test",[{"username":"a","name":"测试"}],{"a"},epoch("2026-10-01"))
            chat=store.selected()[0]["id"]
            store.put(chat,[{"local_id":1,"shard":"a","sort_seq":1,"create_time":epoch("2026-10-10"),"type":"文本","content":XML}])
            with store.connect() as conn:
                conn.execute("UPDATE messages SET kind='动画表情'")
                conn.execute("DELETE FROM archive_meta WHERE key='emoji_compact_v4'")
            Store(path)
            self.assertEqual(store.search(["test-key-never-send"])["messages"],[])
            self.assertEqual(len(store.search(["5f732bf59965"])["messages"]),1)
            export_dirty_days(store)
            Store(path)
            self.assertEqual(export_dirty_days(store),0)
            with store.connect() as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM message_payloads").fetchone()[0],1)

    def test_unescaped_cdn_ampersands_do_not_hide_sticker_metadata(self):
        malformed=XML.replace("&amp;id=123","&id=123").replace('desc=""','desc="辛苦&amp;感谢"')
        self.assertEqual(emoji_metadata(malformed)["md5"],"5f732bf599659318b74d935969f2330d")
        result=compact_emoji("动画表情",malformed)
        self.assertEqual(result,"[表情：辛苦&感谢 #5f732bf59965]")
        self.assertNotIn("http",result)

    def test_group_sender_prefix_is_removed_only_for_actual_media_messages(self):
        text="wxid_fake_member:\n"+XML
        self.assertEqual(emoji_metadata(text)["md5"],"5f732bf599659318b74d935969f2330d")
        self.assertEqual(compact_emoji("动画表情",text),"[表情 #5f732bf59965]")
        self.assertIsNone(compact_emoji("文本",text))


if __name__=="__main__":
    unittest.main()
