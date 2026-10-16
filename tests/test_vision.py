import hashlib
import io
import json
from pathlib import Path
import random
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from deepseek_api import Client,APIError
from storage import Store,epoch
from vision import image_in_range,prepare_image,MIN_IMAGE_BYTES,MAX_IMAGE_BYTES
from media_approval import process_images


def png_bytes():
    from PIL import Image
    image=Image.frombytes("RGB",(200,200),random.Random(3).randbytes(200*200*3))
    buffer=io.BytesIO()
    image.save(buffer,format="PNG")
    return buffer.getvalue()


def picture(local_id,shard="part0"):
    return {"local_id":local_id,"shard":shard,"sort_seq":local_id,"type":"图片",
        "content":"[图片]","sender_name":"测试发言人","create_time":epoch("2026-10-15")}


class Reader:
    account="test-account"
    def __init__(self,data):
        self.data=data
        self.accesses=[]
    def image_asset(self,username,local_id,shard):
        self.accesses.append((username,local_id,shard))
        return {"bytes":self.data,"quality":"cached"}


class VisionTests(unittest.TestCase):
    def setUp(self):
        temporary=ROOT/"work"/"test-data"
        temporary.mkdir(parents=True,exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(dir=temporary)
        self.root=Path(self.tmp.name)
        self.store=Store(self.root/"messages.sqlite3")
        self.groups=[{"username":"chosen@chatroom","name":"已选择群"},
            {"username":"other@chatroom","name":"未选择群"}]
        self.store.set_selection("test-account",self.groups,{"chosen@chatroom"},epoch("2026-10-01"))
        self.chat=Store.chat_id("test-account","chosen@chatroom")
        self.store.set_scope(self.chat,epoch("2026-10-15"),epoch("2026-10-15",end=True))

    def tearDown(self):
        self.tmp.cleanup()

    def client(self,calls):
        def transport(payload):
            calls.append(payload)
            return {"choices":[{"message":{"content":"图片中为月度预算表，预算合计120万元。"},"finish_reason":"stop"}]}
        return Client("fake-test-key",model="deepseek-v4-pro",transport=transport,
            thinking="enabled",reasoning_effort="high")

    def test_size_boundaries_are_strict(self):
        self.assertFalse(image_in_range(MIN_IMAGE_BYTES))
        self.assertTrue(image_in_range(MIN_IMAGE_BYTES+1))
        self.assertTrue(image_in_range(MAX_IMAGE_BYTES-1))
        self.assertFalse(image_in_range(MAX_IMAGE_BYTES))

    def test_payload_parameters_and_image_model(self):
        calls=[]
        client=self.client(calls)
        self.assertEqual(client.build_payload([{"role":"user","content":"test"}])["model"],"deepseek-v4-pro")
        raw=png_bytes()
        client.describe_image(raw,"image/png")
        request=calls[0]
        self.assertEqual(request["model"],"deepseek-flash")
        self.assertEqual(request["thinking"],{"type":"enabled"})
        self.assertEqual(request["reasoning_effort"],"high")
        self.assertTrue(request["messages"][1]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,"))
        self.assertGreater(request["max_tokens"],3500)
        with self.assertRaises(ValueError):
            Client("fake-key",thinking="disabled",reasoning_effort="high")
        with self.assertRaises(ValueError):
            Client("fake-key",thinking="enabled",reasoning_effort="none")
        with self.assertRaises(ValueError):
            client.describe_image(b"x"*MIN_IMAGE_BYTES,"image/png")

    def test_description_is_indexed_cached_and_preserved_after_resync(self):
        rows=[picture(1),picture(2)]
        self.store.put(self.chat,rows)
        self.store.put(Store.chat_id("test-account","other@chatroom"),[picture(3)])
        calls=[]
        reader=Reader(png_bytes())
        result=process_images(self.store,reader,self.client(calls))
        self.assertEqual(result["done"],2)
        self.assertEqual(result["cached"],1)
        self.assertEqual(len(calls),1)
        self.assertEqual({access[0] for access in reader.accesses},{"chosen@chatroom"})
        self.assertEqual(len(self.store.search(["月度预算表"])["messages"]),2)
        self.store.put(self.chat,rows)
        self.assertEqual(len(self.store.search(["月度预算表"])["messages"]),2)
        self.assertIn("AI 生成",self.store.search(["月度预算表"])["messages"][0]["content"])
        self.assertEqual(process_images(self.store,reader,self.client(calls))["done"],0)
        self.assertEqual(len(calls),1)

    def test_ineligible_sizes_do_not_call_vlm_and_missing_key_preserves_pending(self):
        self.store.put(self.chat,[picture(1)])
        calls=[]
        result=process_images(self.store,Reader(b"x"*MIN_IMAGE_BYTES),self.client(calls))
        self.assertEqual(result["skipped"],1)
        self.assertEqual(calls,[])
        self.store.put(self.chat,[picture(2)])
        result=process_images(self.store,Reader(png_bytes()),None)
        self.assertEqual(result["pending"],1)
        self.assertEqual(calls,[])

    def test_api_failure_does_not_advance_or_erase_text_progress_and_can_retry(self):
        self.store.put(self.chat,[picture(1),picture(2)])
        self.store.checkpoint(self.chat,20)
        bad=Client("fake-test-key",transport=lambda _:(_ for _ in ()).throw(APIError("模拟余额不足")))
        reader=Reader(png_bytes())
        result=process_images(self.store,reader,bad)
        # Both messages share one asset and therefore share its failed result.
        self.assertEqual(result["failed"],2)
        self.assertEqual(result["pending"],0)
        self.assertEqual(result["api_calls"],1)
        self.assertEqual(self.store.selected()[0]["cursor"],20)
        self.assertEqual(self.store.stats()[0]["count"],2)
        result=process_images(self.store,reader,self.client([]))
        self.assertEqual(result["done"],2)
        self.assertEqual(self.store.pending_images("test-account"),[])

    def test_source_image_lookup_is_scoped_to_chat_and_shard(self):
        from wechat_source import Source
        data=png_bytes()
        digest="abcdef0123456789abcdef0123456789"
        account_dir=self.root/"wechat-account"
        attach=account_dir/"msg"/"attach"/hashlib.md5(b"chosen@chatroom").hexdigest()/"2026-10"/"Img"
        attach.mkdir(parents=True)
        (attach/(digest+".dat")).write_bytes(data)
        accesses=[]
        class FakeDB:
            def get_message_row(inner,user,local_id,local_type,shard):
                accesses.append((user,local_id,local_type,shard))
                return {"packed_info":digest.encode(),"content":b""}
        source=Source.__new__(Source)
        source.db=FakeDB()
        source.db.account_dir=str(account_dir)
        source._image_files={}
        result=source.image_asset("chosen@chatroom",17,"part3")
        self.assertEqual(result["bytes"],data)
        self.assertEqual(accesses[0],("chosen@chatroom",17,3,"part3"))
        self.assertIsNone(source.image_asset("other@chatroom",17,"part3"))

    def test_wechat_v2_encrypted_image_roundtrip_without_process_access(self):
        from cryptography.hazmat.primitives.ciphers import Cipher,algorithms,modes
        from wechatauto.image_crypto import MediaDownloader,V2_MAGIC
        data=png_bytes()
        key="0123456789abcdef"
        xor=0x45
        encryptor=Cipher(algorithms.AES(key.encode()),modes.ECB()).encryptor()
        encrypted=encryptor.update(data[:32]+bytes([16])*16)+encryptor.finalize()
        wrapped=V2_MAGIC+struct.pack("<LL",32,16)+b"\x00"+encrypted+data[32:-16]+bytes(v^xor for v in data[-16:])
        path=self.root/"encrypted.dat"
        path.write_bytes(wrapped)
        reader=MediaDownloader(object(),save_dir=str(self.root))
        self.assertEqual(reader.decrypt_image(str(path),aes_key=key,xor_key=xor),data)

    def test_wxgf_conversion_uses_local_ffmpeg(self):
        import imageio_ffmpeg
        ffmpeg=imageio_ffmpeg.get_ffmpeg_exe()
        sample=subprocess.run([ffmpeg,"-hide_banner","-loglevel","error","-f","lavfi",
            "-i","color=c=blue:s=128x128:d=0.1","-frames:v","1","-c:v","libx265",
            "-x265-params","log-level=error","-f","hevc","pipe:1"],capture_output=True,timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(sample.returncode,0)
        converted,mime=prepare_image(b"wxgf"+b"metadata"+sample.stdout)
        self.assertEqual(mime,"image/jpeg")
        from PIL import Image
        with Image.open(io.BytesIO(converted)) as image:
            self.assertEqual(image.size,(128,128))

    def test_settings_save_controls_and_sanitized_messages_preview(self):
        import tkinter as tk
        from settings_ui import SettingsDialog
        root=tk.Tk()
        root.withdraw()
        saved=[]
        dialog=SettingsDialog(root,self.root,{"model":"deepseek-v4-pro","thinking":"enabled",
            "reasoning_effort":"high","max_tokens":8192},saved.append)
        dialog.preview_kind.set("图片请求")
        payload=json.loads(dialog.preview.get("1.0","end"))
        self.assertEqual(payload["model"],"deepseek-flash")
        self.assertIn("messages",payload)
        self.assertNotIn("Authorization",payload)
        self.assertEqual(dialog.options()["vlm_concurrency"],200)
        dialog.vlm_concurrency.set("201")
        with self.assertRaises(ValueError):
            dialog.options()
        dialog.vlm_concurrency.set("12")
        dialog.save()
        self.assertEqual(saved[0]["model"],"deepseek-v4-pro")
        self.assertEqual(saved[0]["reasoning_effort"],"high")
        self.assertTrue(saved[0]["vision_enabled"])
        self.assertEqual(saved[0]["vlm_concurrency"],12)
        root.destroy()

    def test_application_update_uses_saved_options_and_enriches_new_images(self):
        import os
        import threading
        from unittest.mock import patch
        from app import App
        data=png_bytes()
        class SyncReader(Reader):
            def refresh(inner):
                pass
            def messages(inner,username,limit,offset):
                return [picture(40)] if offset==0 else []
            def close(inner):
                pass
        reader=SyncReader(data)
        app=App.__new__(App)
        app.store=self.store
        app.data=self.root
        app.config={"model":"deepseek-v4-pro","thinking":"enabled","reasoning_effort":"high",
            "max_tokens":8192,"vision_enabled":True}
        app.stop=threading.Event()
        app.progress=lambda _:None
        app.confirm_vlm=lambda plan:True
        app.make_source=lambda:reader
        self.store.set_scope(self.chat,epoch("2026-10-15"),epoch("2026-10-15",end=True))
        calls=[]
        def transport(payload):
            calls.append(payload)
            return {"choices":[{"message":{"content":"这是测试预算图片"},"finish_reason":"stop"}]}
        def client_factory(key,model,**options):
            return Client(key,model,transport=transport,**options)
        with patch.dict(os.environ,{"DEEPSEEK_API_KEY":"fake-integration-test-key"}),patch('app.Client',side_effect=client_factory):
            result=app.update_messages()
            self.assertEqual(result["groups"],1)
            self.assertEqual(result["vision"]["done"],1)
            self.assertEqual(calls[0]["model"],"deepseek-flash")
            self.assertEqual(calls[0]["reasoning_effort"],"high")
            self.assertEqual(calls[0]["max_tokens"],8192)
            again=app.update_messages()
            self.assertEqual(again["added"],0)
            self.assertEqual(again.get("vision",{}).get("done",0),0)
        self.assertEqual(len(calls),1)
        self.assertEqual(len(self.store.search(["预算图片"])["messages"]),1)


if __name__=="__main__":
    unittest.main(verbosity=2)
