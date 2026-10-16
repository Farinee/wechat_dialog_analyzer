"""Synthetic downloads and mocked VLM only: no network or real user media."""
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
import urllib.request
import urllib.error
from unittest.mock import patch

from PIL import Image

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from storage import Store,epoch
from emoji_cdn import (EmojiCDN,CDNError,CDNTooLarge,LIMIT,checked_url,CheckedRedirect,
    fetch_bytes,candidates,parallel_sources,download_concurrency)
from vision import process_images,media_is_animated
from deepseek_api import Client
from message_text import emoji_metadata


def image_bytes(i=1,fmt="PNG",animated=False):
    output=io.BytesIO()
    image=Image.new("RGB",(32,32),(i*23%256,i*47%256,i*71%256))
    options={"save_all":True,"append_images":[Image.new("RGB",(32,32),"white")],"duration":100,"loop":0} if animated else {}
    image.save(output,format=fmt,**options)
    return output.getvalue()


def metadata(data,token="synthetic-secret"):
    return {"md5":hashlib.md5(data).hexdigest(),"cdnurl":"https://vweixinf.tc.qq.com/original?token="+token,
        "thumburl":"https://qpic.cn/thumbnail-never-used"}


def row(i,data,day="2026-10-10",token="synthetic-secret"):
    meta=metadata(data,token)
    return {"local_id":i,"shard":"part0","sort_seq":i,"create_time":epoch(day),
        "sender_name":"测试成员","type":"动画表情",
        "content":'<msg><emoji '+" ".join(k+'="'+v+'"' for k,v in meta.items())+'/></msg>'}


class Reader:
    account="account"
    def __init__(self,data=None):
        self.data=data
        self.reads=[]
    def emoji_asset(self,user,local_id,shard,meta):
        self.reads.append((user,local_id))
        if self.data is None:
            raise ValueError("本地表情格式尚不支持")
        return {"bytes":self.data}


class CDNTests(unittest.TestCase):
    def setUp(self):
        folder=ROOT/"work"/"test-data"
        folder.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=folder)
        self.store=Store(Path(self.temp.name)/"messages.sqlite3")
        self.groups=[{"username":"chosen","name":"选定联系人"},{"username":"other","name":"其他联系人"}]
        self.store.set_selection("account",self.groups,{"chosen"},epoch("2026-10-01"))
        self.chat=Store.chat_id("account","chosen")
        self.store.set_scope(self.chat,epoch("2026-10-10"),epoch("2026-10-10",True))

    def tearDown(self):
        self.temp.cleanup()

    def client(self,calls):
        def transport(payload):
            calls.append(payload)
            return {"choices":[{"message":{"content":json.dumps({"summary":"合成测试表情","visible_text":[]})},"finish_reason":"stop"}]}
        return Client("fake-only",transport=transport)

    def test_verified_download_and_persistent_cache_are_account_scoped(self):
        data=image_bytes()
        meta=metadata(data)
        fetched=[]
        cdn=EmojiCDN(self.store.media,"account",fetch=lambda url:(fetched.append(url),data)[1])
        source=cdn.get(meta)
        self.assertEqual(source["bytes"],data)
        asset=self.store.media.register(data,data,"image/png")
        self.store.media.remember_cdn("account",source["cdn_request_key"],asset)
        # A new downloader, rather than an in-memory result, must reuse the file.
        cached=EmojiCDN(self.store.media,"account",fetch=lambda _:self.fail("No repeated download")).get(meta)
        self.assertEqual(cached["asset_hash"],asset["hash"])
        other=EmojiCDN(self.store.media,"other",fetch=lambda url:(fetched.append(url),data)[1]).get(meta)
        self.assertEqual(other["bytes"],data)
        self.assertEqual(len(fetched),2)

    def test_encrypted_original_fallback_is_decrypted_and_md5_verified(self):
        from cryptography.hazmat.primitives.ciphers import Cipher,algorithms,modes
        data=image_bytes()
        key=bytes(range(16))
        pad=16-len(data)%16
        cipher=Cipher(algorithms.AES(key),modes.CBC(key)).encryptor()
        encrypted=cipher.update(data+bytes([pad])*pad)+cipher.finalize()
        meta=metadata(data)
        meta.update(encrypturl="https://qpic.cn/encrypted-original",aeskey=key.hex())
        urls=[]
        def fetch(url):
            urls.append(url)
            return encrypted if "encrypted-original" in url else b"<html>expired</html>"
        source=EmojiCDN(self.store.media,"account",fetch=fetch).get(meta)
        self.assertEqual(source["bytes"],data)
        self.assertEqual(source["source_bytes"],encrypted)
        self.assertEqual(len(urls),2)

    def test_wrong_digest_html_and_oversized_files_are_rejected_without_thumbnail(self):
        meta=metadata(image_bytes())
        for raw in (image_bytes(2),b"<html>expired</html>"):
            cdn=EmojiCDN(self.store.media,"account",fetch=lambda _:raw)
            with self.assertRaises(CDNError) as error:
                cdn.get(meta)
            self.assertNotIn("synthetic-secret",str(error.exception))
        with self.assertRaises(CDNTooLarge):
            EmojiCDN(self.store.media,"account",fetch=lambda _:b"x"*(LIMIT+17)).get(meta)
        # An actual image padded to exactly the strict limit is still rejected.
        raw=image_bytes()
        with self.assertRaises(CDNTooLarge):
            EmojiCDN(self.store.media,"account",fetch=lambda _:raw+b"\0"*(LIMIT-len(raw))).get({"cdnurl":meta["cdnurl"]})
        with self.assertRaises(CDNError):
            EmojiCDN(self.store.media,"account",fetch=lambda _:self.fail("No thumbnail fetch")).get({"thumburl":meta["thumburl"]})

    def test_domains_credentials_ports_and_redirects_are_validated(self):
        for url in ("file:///test","https://qq.com.evil.invalid/a","http://127.0.0.1/a",
                "https://qpic.cn@evil.invalid/a","https://user:password@qq.com/a",
                "https://qpic.cn:9999/a","https://qpic.cn/a\r\nAuthorization:secret"):
            with self.assertRaises(CDNError):
                checked_url(url)
        self.assertEqual(checked_url("https://vweixinf.tc.qq.com/a"),"https://vweixinf.tc.qq.com/a")
        req=urllib.request.Request("https://qpic.cn/a")
        for url in ("https://evil.invalid/redirect","http://qpic.cn/downgrade"):
            with self.assertRaises(CDNError):
                CheckedRedirect().redirect_request(req,None,302,"Found",{},url)
        meta=metadata(image_bytes())
        meta.update(cdnurl="https://evil.invalid/no",encrypturl="https://qpic.cn/valid")
        self.assertEqual(len(candidates(meta)),1)

    def test_http_size_limits_and_tls_upgrade(self):
        class Response:
            headers={"Content-Length":str(LIMIT+17)}
            def __enter__(inner): return inner
            def __exit__(inner,*args): pass
            def geturl(inner): return "https://qpic.cn/a"
            def read(inner,n):
                self.assertEqual(n,LIMIT+17)
                return b"x"*n
        response=Response()
        with patch("emoji_cdn.urllib.request.build_opener") as factory:
            factory.return_value.open.return_value=response
            with self.assertRaises(CDNTooLarge): fetch_bytes("http://qpic.cn/a")
            self.assertEqual(factory.return_value.open.call_args.args[0].full_url,"https://qpic.cn/a")
            response.headers={}
            with self.assertRaises(CDNTooLarge): fetch_bytes("http://qpic.cn/a")

    def test_legacy_http_can_retry_its_original_url_but_https_never_downgrades(self):
        class Response:
            headers={}
            def __enter__(inner): return inner
            def __exit__(inner,*args): pass
            def geturl(inner): return "http://qpic.cn/original"
            def read(inner,n): return b"synthetic"
        with patch("emoji_cdn.urllib.request.build_opener") as factory:
            factory.return_value.open.side_effect=[urllib.error.URLError("synthetic TLS error"),Response()]
            self.assertEqual(fetch_bytes("http://qpic.cn/original"),b"synthetic")
            self.assertEqual([c.args[0].full_url for c in factory.return_value.open.call_args_list],
                ["https://qpic.cn/original","http://qpic.cn/original"])
            factory.return_value.open.reset_mock()
            factory.return_value.open.side_effect=urllib.error.URLError("synthetic TLS error")
            with self.assertRaises(CDNError): fetch_bytes("https://qpic.cn/original")
            self.assertEqual(factory.return_value.open.call_count,1)

    def test_malformed_xml_keeps_original_urls_out_of_retrieval_and_vlm(self):
        data=image_bytes()
        item=row(1,data)
        item["content"]=item["content"].replace("synthetic-secret","synthetic-secret&part=2")
        self.assertIn("&part=2",emoji_metadata(item["content"])["cdnurl"])
        self.store.put(self.chat,[item])
        calls=[]
        with patch("emoji_cdn.fetch_bytes",return_value=data):
            result=process_images(self.store,Reader(),self.client(calls),emoji_cdn_enabled=True,confirm=lambda _:True)
        self.assertEqual(result["done"],1)
        self.assertNotIn("synthetic-secret",json.dumps(calls))
        self.assertNotIn("synthetic-secret",self.store.search([])["messages"][0]["content"])

    def test_local_first_and_disabled_fallback_never_download(self):
        data=image_bytes()
        self.store.put(self.chat,[row(1,data)])
        with patch("emoji_cdn.fetch_bytes",side_effect=AssertionError("No download")):
            local=process_images(self.store,Reader(data),self.client([]),emoji_cdn_enabled=True,confirm=lambda _:False)
            self.assertEqual(local["pending"],1)
            self.store.put(self.chat,[row(2,image_bytes(2))])
            disabled=process_images(self.store,Reader(),self.client([]),emoji_cdn_enabled=False,confirm=lambda _:False)
            self.assertEqual(disabled["failed"],1)

    def test_corrupt_local_full_file_also_falls_back_to_original(self):
        data=image_bytes()
        self.store.put(self.chat,[row(1,data)])
        with patch("emoji_cdn.fetch_bytes",return_value=data) as fetch:
            result=process_images(self.store,Reader(b"GIF89a-corrupt"),self.client([]),
                emoji_cdn_enabled=True,confirm=lambda _:False)
        self.assertEqual(fetch.call_count,1)
        self.assertEqual((result["pending"],result["failed"]),(1,0))

    def test_api_settings_keep_download_limit_separate_from_vlm_limit(self):
        from settings_ui import SettingsDialog
        def value(v):
            return SimpleNamespace(get=lambda *args:v)
        dialog=SimpleNamespace(model=value("deepseek-flash"),thinking=value("disabled"),effort=value("默认"),
            max_tokens=value("0"),context_limit=value("1000000"),max_input_tokens=value("256000"),vlm_concurrency=value("200"),download_concurrency=value("8"),
            system_prompt=value(""),vision_enabled=value(True),emoji_enabled=value(True),
            emoji_cdn_enabled=value(True),vision_prompt=value("测试"),db_dir=value(""))
        options=SettingsDialog.options(dialog)
        self.assertEqual((options["download_concurrency"],options["vlm_concurrency"]),(8,200))
        self.assertTrue(options["emoji_cdn_enabled"])
        dialog.download_concurrency=value("201")
        with self.assertRaisesRegex(ValueError,"表情下载并发数"):
            SettingsDialog.options(dialog)

    def test_scoped_downloads_and_cancelled_vlm_retry_from_local_cache(self):
        data=image_bytes()
        self.store.put(self.chat,[row(1,data),row(2,data),row(3,image_bytes(2),day="2026-10-11",token="outside-date")])
        self.store.put(Store.chat_id("account","other"),[row(4,image_bytes(3),token="outside-chat")])
        calls,plans=[],[]
        client=self.client(calls)
        scope=tuple(self.store.scope)
        def progress(text):
            # Switching the UI scope during the job must not widen the download query.
            self.store.set_scope(Store.chat_id("account","other"),epoch("2026-10-01"),epoch("2026-10-15",True))
        def reject(plan):
            self.assertEqual(calls,[])
            self.assertEqual((plan["unique_assets"],plan["queued_messages"]),(1,2))
            plans.append(plan)
            return False
        reader=Reader()
        with patch("emoji_cdn.fetch_bytes",return_value=data) as fetch:
            result=process_images(self.store,reader,client,progress=progress,scope=scope,
                emoji_cdn_enabled=True,confirm=reject,download_concurrency=2)
        self.assertTrue(result["cancelled"])
        self.assertEqual(result["pending"],2)
        self.assertEqual(len(reader.reads),2)
        self.assertEqual(fetch.call_count,1)
        self.assertNotIn("outside-",fetch.call_args.args[0])
        self.assertEqual(calls,[])
        with patch("emoji_cdn.fetch_bytes",side_effect=AssertionError("Use retained bytes")):
            retry=process_images(self.store,Reader(),client,scope=scope,emoji_cdn_enabled=True,confirm=lambda _:True)
        self.assertEqual((retry["done"],retry["api_calls"]),(2,1))
        self.assertEqual(len(calls),1)
        with self.store.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM image_results").fetchone()[0],2)

    def test_full_animated_downloads_never_reach_vlm_or_confirmation(self):
        calls=[]
        for i,fmt in enumerate(("GIF","PNG","WEBP"),1):
            data=image_bytes(i,fmt,animated=True)
            self.assertTrue(media_is_animated(data))
            self.store.put(self.chat,[row(i,data)])
            with patch("emoji_cdn.fetch_bytes",return_value=data):
                result=process_images(self.store,Reader(),self.client(calls),emoji_cdn_enabled=True,
                    confirm=lambda _:self.fail("All files are animated"))
            self.assertEqual(result["animated"],1)
        self.assertEqual(calls,[])

    def test_parallel_downloads_are_bounded_and_duplicates_share_one_fetch(self):
        entries=[]
        resources={}
        for i in range(1,7):
            data=image_bytes(i)
            meta=metadata(data,str(i))
            entries.append((i,{"id":str(i)},meta))
            resources[meta["cdnurl"]]=data
        entries.append((7,{"id":"7"},entries[0][2]))
        lock=threading.Lock()
        full,release=threading.Event(),threading.Event()
        state={"active":0,"peak":0,"calls":0}
        def fetch(url):
            with lock:
                state["active"]+=1
                state["peak"]=max(state["peak"],state["active"])
                state["calls"]+=1
                if state["active"]==3: full.set()
            try:
                if not release.wait(5): raise RuntimeError("Synthetic gate timed out")
                return resources[url]
            finally:
                with lock: state["active"]-=1
        results=[]
        cdn=EmojiCDN(self.store.media,"account",fetch=fetch)
        worker=threading.Thread(target=lambda:results.extend(parallel_sources(entries,cdn,3)))
        worker.start()
        try:
            self.assertTrue(full.wait(5))
            self.assertEqual(state["calls"],3)
        finally:
            release.set()
            worker.join(8)
        self.assertFalse(worker.is_alive())
        self.assertEqual((state["peak"],state["calls"],len(results)),(3,6,7))
        self.assertTrue(all(source and error is None for _,_,source,error in results))
        self.assertEqual(download_concurrency("200"),200)
        for value in (0,201,-1,True,1.5,"abc"):
            with self.assertRaises(ValueError): download_concurrency(value)

    def test_stop_during_download_keeps_pending_count_and_never_calls_vlm(self):
        self.store.put(self.chat,[row(i,image_bytes(i),token=str(i)) for i in range(1,5)])
        stop=threading.Event()
        calls=[]
        def fetch(url):
            stop.set()
            return image_bytes(int(url.rsplit("=",1)[-1]))
        with patch("emoji_cdn.fetch_bytes",side_effect=fetch) as mocked:
            result=process_images(self.store,Reader(),self.client(calls),stop=stop,
                emoji_cdn_enabled=True,download_concurrency=1,confirm=lambda _:self.fail("No VLM"))
        self.assertEqual(mocked.call_count,1)
        self.assertEqual((result["pending"],result["api_calls"]),(4,0))
        self.assertEqual(calls,[])


if __name__=="__main__":
    unittest.main()
