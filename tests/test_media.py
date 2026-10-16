"""Synthetic media only: no WeChat process access or real API uploads."""
import hashlib
import io
import json
from pathlib import Path
import random
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest

from PIL import Image

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from storage import Store,epoch
from deepseek_api import Client,APIError
from vision import MAX_IMAGE_BYTES,MIN_IMAGE_BYTES
from archive import mark_covered,export_dirty_days,archive_dir
from media_approval import process_images,sync_range
from wechat_source import Source


def png(noise=False):
    output=io.BytesIO()
    image=Image.frombytes("RGB",(200,200),random.Random(11).randbytes(120000)) if noise else Image.new("RGB",(64,64),"red")
    image.save(output,format="PNG")
    return output.getvalue()


def animated(fmt="GIF"):
    output=io.BytesIO()
    Image.new("RGB",(64,64),"red").save(output,format=fmt,save_all=True,
        append_images=[Image.new("RGB",(64,64),"blue")],duration=100,loop=0)
    return output.getvalue()


def xml(data):
    return '<msg><emoji type="2" md5="'+hashlib.md5(data).hexdigest()+'" aeskey="raw-secret-test-only" cdnurl="https://example.invalid/never-download" desc=""/></msg>'


def row(i,data,kind="动画表情",day="2026-10-10"):
    return {"local_id":i,"shard":"part0","sort_seq":i,"create_time":epoch(day),
        "type":kind,"sender_name":"示例成员","content":xml(data) if "表情" in kind else "[图片]"}


class Reader:
    account="account"
    def __init__(self,data):
        self.data,self.reads=data,[]
    def image_asset(self,*args):
        self.reads.append(args)
        return {"bytes":self.data}
    def emoji_asset(self,*args):
        self.reads.append(args)
        return {"bytes":self.data}


class MediaTests(unittest.TestCase):
    def setUp(self):
        folder=ROOT/"work"/"test-data"
        folder.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=folder)
        self.path=Path(self.temp.name)
        self.store=Store(self.path/"messages.sqlite3")
        self.groups=[{"username":"a@chatroom","name":"示例群"},{"username":"person","name":"示例联系人"}]
        self.store.set_selection("account",self.groups,{"a@chatroom"},epoch("2026-10-01"))
        self.chat=Store.chat_id("account","a@chatroom")
        self.store.set_scope(self.chat,epoch("2026-10-10"),epoch("2026-10-10",True))

    def tearDown(self):
        self.temp.cleanup()

    def client(self,calls,summary="红色图形",prompt="测试提示词"):
        def transport(payload):
            calls.append(payload)
            data={"summary":summary,"visible_text":["测试文字"],"numbers_dates":["预算120万元"],
                "tone":"可能表示庆祝，属于推测","keywords":["预算表","庆祝"],"uncertainty":["无法判断发言者真实意图"]}
            return {"choices":[{"message":{"content":json.dumps(data,ensure_ascii=False)},"finish_reason":"stop"}]}
        return Client("fake-only",transport=transport,vision_prompt=prompt)

    def test_tiny_static_sticker_is_recognized_once_and_raw_xml_never_enters_model(self):
        data=png()
        self.assertLess(len(data),MIN_IMAGE_BYTES)
        self.store.put(self.chat,[row(1,data),row(2,data)])
        calls=[]
        reader=Reader(data)
        result=process_images(self.store,reader,self.client(calls))
        self.assertEqual((result["done"],result["cached"]),(2,1))
        self.assertEqual(len(calls),1)
        self.assertEqual(len(reader.reads),1)
        self.assertEqual({r["kind"] for r in self.store.search([])["messages"]},{"静态表情"})
        self.assertNotIn("raw-secret-test-only",json.dumps(calls))
        self.assertNotIn("example.invalid",json.dumps(calls))
        self.assertEqual(calls[0]["response_format"],{"type":"json_object"})
        with self.store.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM media_assets").fetchone()[0],1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM media_analyses").fetchone()[0],1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM media_links").fetchone()[0],2)
        self.assertEqual(self.store.raw_payload(self.store.search([])["messages"][0]["id"]),xml(data))

    def test_animated_gif_and_apng_are_skipped_without_any_vlm_request(self):
        calls=[]
        for i,fmt in enumerate(("GIF","PNG","WEBP"),1):
            data=animated(fmt)
            self.store.put(self.chat,[row(i,data)])
            result=process_images(self.store,Reader(data),self.client(calls))
            self.assertEqual(result["animated"],1)
        self.assertEqual(calls,[])
        self.assertEqual(self.store.pending_images("account"),[])
        self.assertEqual({r["kind"] for r in self.store.search([])["messages"]},{"动态表情"})
        self.assertEqual(self.store.media.request_reanalysis(),0)

    def test_static_gif_is_allowed_and_animated_outbound_requests_are_rejected(self):
        buffer=io.BytesIO()
        Image.new("RGB",(32,32),"red").save(buffer,format="GIF")
        self.store.put(self.chat,[row(1,buffer.getvalue())])
        calls=[]
        client=self.client(calls)
        self.assertEqual(process_images(self.store,Reader(buffer.getvalue()),client)["done"],1)
        with self.assertRaises(ValueError):
            client.describe_media(animated(),"image/gif",{"category":"emoji"})
        self.assertEqual(len(calls),1)

    def test_picture_lower_limit_is_not_bypassed_by_existing_sticker_cache(self):
        data=png()
        self.store.put(self.chat,[row(1,data)])
        calls=[]
        process_images(self.store,Reader(data),self.client(calls))
        self.store.put(self.chat,[row(2,data,"图片")])
        result=process_images(self.store,Reader(data),self.client(calls))
        self.assertEqual(result["skipped"],1)
        self.assertEqual(len(calls),1)
        self.store.put(self.chat,[row(3,data)])
        class TooBig(Reader):
            def emoji_asset(inner,*args):
                return {"bytes":b"","size":MAX_IMAGE_BYTES}
        # Different metadata ensures this is not a previously verified asset.
        with self.store.connect() as conn:
            conn.execute("DELETE FROM media_sources")
        self.assertEqual(process_images(self.store,TooBig(data),self.client(calls))["skipped"],1)
        self.assertEqual(len(calls),1)

    def test_cache_reuses_across_contacts_dates_prompt_changes_and_missing_api_key(self):
        data=png()
        self.store.put(self.chat,[row(1,data)])
        calls=[]
        process_images(self.store,Reader(data),self.client(calls))
        self.store.set_selection("account",self.groups,{"person"},epoch("2026-10-01"))
        other=Store.chat_id("account","person")
        self.store.set_scope(other,epoch("2026-10-11"),epoch("2026-10-11",True))
        self.store.put(other,[row(1,data,day="2026-10-11")])
        class NoReads(Reader):
            def emoji_asset(inner,*args):
                self.fail("Verified sticker must not be decoded again")
        result=process_images(self.store,NoReads(data),self.client(calls,prompt="不同提示词"))
        self.assertEqual(result["cached"],1)
        self.store.put(other,[row(2,data,day="2026-10-11")])
        self.assertEqual(process_images(self.store,NoReads(data),None)["cached"],1)
        self.assertEqual(len(calls),1)

    def test_scope_blocks_other_chats_and_other_dates_from_asset_processing(self):
        data=png()
        self.store.put(self.chat,[row(1,data),row(2,data,day="2026-10-11")])
        self.store.put(Store.chat_id("account","person"),[row(3,data)])
        reader=Reader(data)
        self.assertEqual(process_images(self.store,reader,self.client([]))["done"],1)
        self.assertEqual(len(reader.reads),1)
        self.assertEqual(reader.reads[0][0],"a@chatroom")

    def test_missing_scope_cannot_process_or_reanalyze_all_history(self):
        data=png()
        self.store.put(self.chat,[row(1,data)])
        reader,calls=Reader(data),[]
        self.store.scope=None
        self.assertEqual(self.store.pending_images("account"),[])
        self.assertEqual(self.store.media.request_reanalysis(),0)
        with self.assertRaisesRegex(ValueError,"日期范围"):
            process_images(self.store,reader,self.client(calls))
        self.assertEqual(reader.reads,[])
        self.assertEqual(calls,[])

    def test_sync_freezes_scope_for_import_vlm_and_text_exports(self):
        data=png()
        other=Store.chat_id("account","person")
        self.store.put(self.chat,[row(2,data,day="2026-10-09"),row(3,data,day="2026-10-11")])
        self.store.put(other,[row(4,data)])
        scopes,logs,calls=[],[],[]
        test=self
        class SyncReader(Reader):
            def refresh(inner):
                pass
            def message_batches(inner,user,start,end,page_size):
                scopes.append((user,start,end))
                # Even if an adapter yields rows outside the dates, reject them.
                yield [row(1,data),row(5,data,day="2026-10-09"),row(6,data,day="2026-10-11")]
            def emoji_asset(inner,user,local_id,shard,metadata):
                test.assertEqual((user,str(local_id)),("a@chatroom","1"))
                return super().emoji_asset(user,local_id,shard,metadata)
            def close(inner):
                pass
        reader=SyncReader(data)
        original_scope=self.store.scope
        def client_factory():
            # A later change to the shared Store cannot broaden this job.
            self.store.scope=None
            return self.client(calls)
        result=sync_range(self.store,lambda:reader,client_factory=client_factory,progress=logs.append,
            now=epoch("2026-10-15"))
        self.assertEqual(result["errors"],[])
        self.assertEqual(result["added"],1)
        self.assertEqual(result["vision"]["done"],1)
        self.assertEqual(len(calls),1)
        self.assertEqual(scopes,[("a@chatroom",*original_scope[1:])])
        self.assertEqual(len(reader.reads),1)
        self.assertTrue(any("本范围素材 1/1" in s and "2026-10-10 至 2026-10-10" in s for s in logs))
        self.assertTrue((archive_dir(self.store,self.chat)/"2026-10-10.txt").is_file())
        self.assertFalse((archive_dir(self.store,self.chat)/"2026-10-09.txt").exists())
        self.assertFalse((archive_dir(self.store,self.chat)/"2026-10-11.txt").exists())
        self.assertFalse(archive_dir(self.store,other).exists())
        with self.store.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0],4)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM image_results").fetchone()[0],1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM dirty_days").fetchone()[0],3)

    def test_failed_api_retries_from_local_asset_without_opening_wechat(self):
        data=png()
        self.store.put(self.chat,[row(1,data)])
        bad=Client("fake-only",transport=lambda _:(_ for _ in ()).throw(APIError("模拟失败")))
        self.assertEqual(process_images(self.store,Reader(data),bad)["failed"],1)
        mark_covered(self.store,self.chat,*self.store.scope[1:])
        def no_source():
            self.fail("Retry must use the retained local asset, not WeChat")
        calls=[]
        result=sync_range(self.store,no_source,client_factory=lambda:self.client(calls),now=epoch("2026-10-15"))
        self.assertEqual(result["errors"],[])
        self.assertEqual(result["vision"]["done"],1)
        self.assertEqual(len(calls),1)

    def test_export_and_api_context_include_one_dictionary_entry_for_repeated_events(self):
        data=png()
        self.store.put(self.chat,[row(1,data),row(2,data)])
        process_images(self.store,Reader(data),self.client([]))
        export_dirty_days(self.store)
        text=(archive_dir(self.store,self.chat)/"2026-10-10.txt").read_text(encoding="utf-8")
        self.assertEqual(text.count("消息ID："),2)
        self.assertEqual(text.count("红色图形"),1)
        calls=[]
        def transport(payload):
            calls.append(payload)
            answer=json.dumps({"keywords":[],"chat_names":[],"start_date":None,"end_date":None}) if len(calls)==1 else "引用 [M1]"
            return {"choices":[{"message":{"content":answer},"finish_reason":"stop"}]}
        response=Client("fake-only",transport=transport).answer_retrieved(self.store,"总结")
        content=json.loads(calls[1]["messages"][1]["content"])
        self.assertEqual(len(content["supplied_messages"]),2)
        self.assertEqual(len(content["supplied_assets"]),1)
        self.assertEqual(json.dumps(content,ensure_ascii=False).count("红色图形"),1)
        self.assertEqual(len(response["sources"]),2)

    def test_manual_reanalysis_versions_results_and_updates_all_linked_days(self):
        data=png()
        self.store.put(self.chat,[row(1,data),row(2,data,day="2026-10-11")])
        self.store.set_scope(self.chat,epoch("2026-10-10"),epoch("2026-10-11",True))
        calls=[]
        process_images(self.store,Reader(data),self.client(calls,summary="旧描述"))
        export_dirty_days(self.store)
        self.store.set_scope(self.chat,epoch("2026-10-10"),epoch("2026-10-10",True))
        self.assertEqual(self.store.media.request_reanalysis(),1)
        process_images(self.store,Reader(data),self.client(calls,summary="新描述"))
        self.assertEqual(len(calls),2)
        self.assertEqual(export_dirty_days(self.store),2)
        for day in ("2026-10-10","2026-10-11"):
            text=(archive_dir(self.store,self.chat)/(day+".txt")).read_text(encoding="utf-8")
            self.assertIn("新描述",text)
            self.assertNotIn("旧描述",text)
        self.assertEqual(len(list((self.path/"analysis").rglob("*.json"))),2)

    def test_concurrent_workers_claim_one_asset_and_do_not_duplicate_vlm_calls(self):
        data=png()
        self.store.put(self.chat,[row(1,data)])
        entered,finish=threading.Event(),threading.Event()
        calls=[]
        client=self.client(calls)
        transport=client.transport
        def blocking(payload):
            entered.set()
            if not finish.wait(5):
                raise RuntimeError("测试超时")
            return transport(payload)
        client.transport=blocking
        thread=threading.Thread(target=lambda:process_images(self.store,Reader(data),client))
        thread.start()
        try:
            self.assertTrue(entered.wait(5))
            result=process_images(self.store,Reader(data),self.client(calls))
            self.assertEqual(result["pending"],1)
            self.assertEqual(calls,[])
        finally:
            finish.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(calls),1)

    def test_source_uses_full_local_files_and_ignores_thumbnails(self):
        data=animated()
        digest=hashlib.md5(data).hexdigest()
        base=self.path/"wechat"
        thumb=base/"business"/"emoticon"/"Thumb"
        thumb.mkdir(parents=True)
        (thumb/digest).write_bytes(png())
        source=Source.__new__(Source)
        source.db=SimpleNamespace(account_dir=str(base))
        meta={"md5":digest}
        self.assertIsNone(source.emoji_asset("a@chatroom",1,"part0",meta))
        del source._emoji_files
        original=base/"cache"/"2026-10"/"Emoticon"/digest[:2]
        original.mkdir(parents=True)
        (original/digest).write_bytes(data)
        self.assertEqual(source.emoji_asset("a@chatroom",1,"part0",meta)["bytes"],data)

    def test_source_decrypts_local_sticker_and_retains_original_copy(self):
        from cryptography.hazmat.primitives.ciphers import Cipher,algorithms,modes
        data=png()
        key=b"0123456789abcdef"
        pad=16-len(data)%16
        encryptor=Cipher(algorithms.AES(key),modes.CBC(key)).encryptor()
        encrypted=encryptor.update(data+bytes([pad])*pad)+encryptor.finalize()
        digest=hashlib.md5(data).hexdigest()
        base=self.path/"wechat"
        folder=base/"business"/"emoticon"/"Persist"
        folder.mkdir(parents=True)
        (folder/digest).write_bytes(encrypted)
        source=Source.__new__(Source)
        source.db=SimpleNamespace(account_dir=str(base))
        asset=source.emoji_asset("a@chatroom",1,"part0",{"md5":digest,"aeskey":key.hex()})
        self.assertEqual(asset["bytes"],data)
        self.assertEqual(asset["source_bytes"],encrypted)
        saved=self.store.media.register(data,data,"image/png",source_bytes=encrypted)
        self.assertEqual((self.path/saved["original_path"]).read_bytes(),data)
        self.assertEqual((self.path/"assets"/saved["hash"]/"source.bin").read_bytes(),encrypted)

    def test_unverified_source_md5_does_not_merge_different_files(self):
        first=png()
        output=io.BytesIO()
        Image.new("RGB",(64,64),"blue").save(output,format="PNG")
        second=output.getvalue()
        rows=[row(1,first),row(2,second)]
        for item in rows:
            item["content"]='<msg><emoji md5="'+"f"*32+'"/></msg>'
        self.store.put(self.chat,rows)
        class Files(Reader):
            def emoji_asset(inner,user,lid,*args):
                return {"bytes":first if lid=="1" else second}
        calls=[]
        process_images(self.store,Files(first),self.client(calls))
        self.assertEqual(len(calls),2)
        with self.store.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM media_sources").fetchone()[0],0)

    def test_ui_manual_reanalysis_uses_local_cache_and_updates_preview_dictionary(self):
        import tkinter as tk
        import time
        from app import App
        data=png()
        self.store.put(self.chat,[row(1,data),row(2,data)])
        calls=[]
        process_images(self.store,Reader(data),self.client(calls,summary="旧界面描述"))
        mark_covered(self.store,self.chat,*self.store.scope[1:])
        def no_source(*args,**kwargs):
            self.fail("UI reanalysis must use retained files")
        root=tk.Tk()
        root.withdraw()
        app=App(root,self.path,auto=False,source_factory=no_source)
        try:
            app.groups=self.groups
            app.account,app.loaded="account",True
            app.selected_names={"a@chatroom"}
            app.since.set("2026-10-10")
            app.until.set("2026-10-10")
            app.make_client=lambda optional=False:self.client(calls,summary="新界面描述")
            app.confirm_vlm=lambda plan:True
            app.reanalyze_media()
            deadline=time.monotonic()+5
            while app.busy and time.monotonic()<deadline:
                root.update()
                time.sleep(.01)
            self.assertFalse(app.busy)
            self.assertEqual(len(calls),2)
            preview=app.sources_text.get("1.0","end")
            self.assertEqual(preview.count("新界面描述"),1)
            self.assertNotIn("旧界面描述",preview)
            original=app.raw_sources_text.get("1.0","end")
            self.assertEqual(original.count(xml(data)),2)
            self.assertIn("original.bin",original)
            self.assertNotIn("新界面描述",original)
            self.assertNotIn("raw-secret-test-only",preview)
            sources=app.store.search([])["messages"][:1]
            app.events.put(("answer",{"answer":"示例回答","plan":{},"sources":[{
                "id":sources[0]["id"],"ref":"M1","group":"示例群","time":"2026-10-10",
                "sender":"示例成员","text":"可检索引用"}]}))
            app.poll()
            original=app.raw_sources_text.get("1.0","end")
            self.assertEqual(original.count(xml(data)),1)
            self.assertIn("[M1]",original)
            self.assertIn("可检索引用",app.sources_text.get("1.0","end"))
            app.since.set("2026-10-11")
            app.until.set("2026-10-11")
            app.show_local()
            self.assertNotIn(xml(data),app.raw_sources_text.get("1.0","end"))
            app.since.set("bad-date")
            app.show_local()
            self.assertIn("YYYY-MM-DD",app.raw_sources_text.get("1.0","end"))
        finally:
            for timer in root.tk.call("after","info"):
                root.after_cancel(timer)
            root.destroy()

    def test_missing_input_is_rebuilt_from_local_original_without_wechat(self):
        data=png()
        self.store.put(self.chat,[row(1,data)])
        calls=[]
        process_images(self.store,Reader(data),self.client(calls))
        hit=self.store.search([])["messages"][0]
        asset=self.store.media.for_message(hit["id"])
        (self.path/asset["input_path"]).unlink()
        self.store.media.request_reanalysis()
        class NoReads(Reader):
            def emoji_asset(inner,*args):
                self.fail("Derivative must be rebuilt from the local original")
        result=process_images(self.store,NoReads(data),self.client(calls))
        self.assertEqual(result["done"],1)
        self.assertEqual(len(calls),2)

    def test_failed_reanalysis_and_text_resync_keep_previous_successful_interpretation(self):
        data=png()
        message=row(1,data)
        self.store.put(self.chat,[message])
        process_images(self.store,Reader(data),self.client([],summary="原有成功描述"))
        self.store.media.request_reanalysis()
        bad=Client("fake-only",transport=lambda _:(_ for _ in ()).throw(APIError("模拟失败")))
        self.assertEqual(process_images(self.store,Reader(data),bad)["failed"],1)
        self.store.put(self.chat,[message])
        self.assertEqual(len(self.store.search(["原有成功描述"])["messages"]),1)
        self.assertEqual(self.store.search([])["messages"][0]["kind"],"静态表情")

    def test_cipher_file_cache_reuses_previous_decode_before_key_resolution(self):
        data=png()
        ciphertext=b"not-a-real-encrypted-file"*10
        asset=self.store.media.register(data,data,"image/png",source_bytes=ciphertext)
        base=self.path/"wechat"
        folder=base/"business"/"emoticon"/"Persist"
        folder.mkdir(parents=True)
        digest=hashlib.md5(data).hexdigest()
        (folder/digest).write_bytes(ciphertext)
        source=Source.__new__(Source)
        source.db=SimpleNamespace(account_dir=str(base))
        source.account="account"
        source.set_media_cache(self.store.media.by_source_hash)
        self.store.put(self.chat,[row(1,data)])
        calls=[]
        result=process_images(self.store,source,self.client(calls))
        self.assertEqual(result["done"],1)
        self.assertEqual(len(calls),1)
        self.assertFalse(hasattr(source,"_emoji_key_attempted"))
        self.assertEqual(self.store.search([])["messages"][0]["asset_id"],asset["hash"])

    def test_wxgf_multi_frame_container_is_not_sent_to_vlm(self):
        import imageio_ffmpeg,subprocess,os
        result=subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(),"-hide_banner","-loglevel","error",
            "-f","lavfi","-i","color=c=blue:s=64x64:d=0.2","-frames:v","2","-c:v","libx265",
            "-x265-params","log-level=error","-f","hevc","pipe:1"],capture_output=True,timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=="nt" else 0)
        self.assertEqual(result.returncode,0)
        data=b"wxgf"+b"metadata"+result.stdout
        self.store.put(self.chat,[row(1,data)])
        calls=[]
        processed=process_images(self.store,Reader(data),self.client(calls))
        self.assertEqual(processed["animated"],1)
        self.assertEqual(calls,[])


if __name__=="__main__":
    unittest.main()
