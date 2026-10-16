"""Synthetic local files and mocked API only; never upload real user media."""
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from PIL import Image

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from storage import Store,epoch
from vision import process_images,vlm_concurrency,MAX_VLM_CONCURRENCY
from deepseek_api import Client,APIError
from archive import mark_covered


def png(i):
    output=io.BytesIO()
    Image.new("RGB",(32,32),(i*23%256,i*47%256,i*71%256)).save(output,format="PNG")
    return output.getvalue()


def row(i,data,day="2026-10-10"):
    return {"local_id":i,"shard":"part0","sort_seq":i,"create_time":epoch(day),
        "sender_name":"测试成员","type":"动画表情",
        "content":'<msg><emoji md5="'+hashlib.md5(data).hexdigest()+'"/></msg>'}


def response():
    return {"choices":[{"message":{"content":json.dumps({"summary":"测试图片","visible_text":[]})},"finish_reason":"stop"}]}


class ConcurrentVLMTests(unittest.TestCase):
    def setUp(self):
        folder=ROOT/"work"/"test-data"
        folder.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=folder)
        self.path=Path(self.temp.name)
        self.store=Store(self.path/"messages.sqlite3")
        self.groups=[{"username":"chosen","name":"选定联系人"},{"username":"other","name":"其他联系人"}]
        self.store.set_selection("account",self.groups,{"chosen"},epoch("2026-10-01"))
        self.chat=Store.chat_id("account","chosen")
        self.store.set_scope(self.chat,epoch("2026-10-10"),epoch("2026-10-10",end=True))

    def tearDown(self):
        self.temp.cleanup()

    def reader(self,files):
        test=self
        class Reader:
            account="account"
            accesses=[]
            def emoji_asset(inner,user,local_id,shard,metadata):
                test.assertEqual(user,"chosen")
                inner.accesses.append((str(local_id),threading.get_ident()))
                return {"bytes":files[int(local_id)]}
        return Reader()

    def test_approval_is_required_and_cancelled_batch_retries_from_local_files(self):
        data=png(1)
        self.store.put(self.chat,[row(1,data),row(2,data)])
        calls,plans=[],[]
        client=Client("fake-only",transport=lambda payload:(calls.append(payload),response())[1])
        result=process_images(self.store,self.reader({1:data,2:data}),client)
        self.assertTrue(result["cancelled"])
        self.assertEqual(result["pending"],2)
        self.assertEqual(calls,[])
        def no_source(*args):
            self.fail("Prepared assets must survive cancellation")
        reader=self.reader({})
        reader.emoji_asset=no_source
        def reject(plan):
            plans.append(plan)
            self.assertEqual(calls,[])
            return False
        result=process_images(self.store,reader,client,confirm=reject)
        self.assertTrue(result["cancelled"])
        self.assertEqual((plans[0]["unique_assets"],plans[0]["queued_messages"]),(1,2))
        self.assertEqual((plans[0]["chat"],plans[0]["start"],plans[0]["end"]),
            ("选定联系人","2026-10-10","2026-10-10"))
        self.assertEqual(calls,[])
        result=process_images(self.store,reader,client,confirm=lambda plan:True)
        self.assertEqual((result["api_calls"],result["done"],result["cached"]),(1,2,1))
        self.assertEqual(len(calls),1)

    def test_parallel_calls_respect_limit_deduplicate_and_keep_scope(self):
        files={i:png(i) for i in range(1,7)}
        files[7]=files[1]
        self.store.put(self.chat,[row(i,data) for i,data in files.items()])
        self.store.put(self.chat,[row(8,png(8),"2026-10-11")])
        self.store.put(Store.chat_id("account","other"),[row(9,png(9))])
        lock=threading.Lock()
        full,release=threading.Event(),threading.Event()
        state={"active":0,"peak":0,"calls":0}
        thread_ids=set()
        def transport(payload):
            with lock:
                state["active"]+=1
                state["calls"]+=1
                state["peak"]=max(state["peak"],state["active"])
                thread_ids.add(threading.get_ident())
                if state["active"]==3:
                    full.set()
            try:
                if not release.wait(5):
                    raise RuntimeError("synthetic gate timed out")
                return response()
            finally:
                with lock:
                    state["active"]-=1
        plans,results,errors=[],[],[]
        reader=self.reader(files)
        def approve(plan):
            self.assertEqual(state["calls"],0)
            plans.append(plan)
            return True
        def run():
            try:
                results.append(process_images(self.store,reader,Client("fake-only",transport=transport),
                    confirm=approve,concurrency=3))
            except Exception as exc:
                errors.append(exc)
        worker=threading.Thread(target=run)
        worker.start()
        try:
            self.assertTrue(full.wait(5))
            self.assertEqual(state["peak"],3)
            self.assertEqual(state["calls"],3)
        finally:
            release.set()
            worker.join(8)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors,[])
        self.assertEqual(state["calls"],6)
        self.assertEqual(results[0]["done"],7)
        self.assertEqual(results[0]["cached"],1)
        self.assertEqual((plans[0]["unique_assets"],plans[0]["concurrency"]),(6,3))
        self.assertEqual(len(reader.accesses),6)
        self.assertEqual({tid for _,tid in reader.accesses},{worker.ident})
        self.assertNotIn(worker.ident,thread_ids)
        with self.store.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM image_results").fetchone()[0],7)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM media_jobs").fetchone()[0],0)

    def test_fatal_api_error_stops_new_submissions_and_releases_claims(self):
        files={i:png(i) for i in range(1,5)}
        self.store.put(self.chat,[row(i,data) for i,data in files.items()])
        def transport(payload):
            raise APIError("模拟余额不足")
        result=process_images(self.store,self.reader(files),Client("fake-only",transport=transport),
            concurrency=1,confirm=lambda plan:True)
        self.assertEqual((result["api_calls"],result["failed"],result["pending"]),(1,1,3))
        with self.store.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM media_jobs").fetchone()[0],0)

    def test_stop_before_approval_sends_nothing_and_concurrency_has_hard_cap(self):
        self.assertEqual(MAX_VLM_CONCURRENCY,200)
        self.assertEqual(vlm_concurrency("200"),200)
        for value in (0,201,-1,True,1.5,"abc"):
            with self.assertRaises(ValueError):
                vlm_concurrency(value)
        data=png(1)
        self.store.put(self.chat,[row(1,data)])
        stop=threading.Event()
        def approve(plan):
            stop.set()
            return True
        result=process_images(self.store,self.reader({1:data}),Client("fake-only",transport=lambda payload:self.fail("No upload")),
            stop=stop,confirm=approve)
        self.assertEqual(result["api_calls"],0)
        self.assertEqual(result["pending"],1)

    def test_ui_prompts_on_main_thread_and_cancel_preserves_text(self):
        import tkinter as tk
        from app import App
        data=png(1)
        self.store.put(self.chat,[row(1,data)])
        uid=self.store.search([])["messages"][0]["id"]
        asset=self.store.media.register(data,data,"image/png")
        self.store.media.link(uid,asset,"emoji")
        mark_covered(self.store,self.chat,*self.store.scope[1:])
        root=tk.Tk()
        root.withdraw()
        calls,prompts=[],[]
        def no_source(*args,**kwargs):
            self.fail("Cached media confirmation must not open WeChat")
        app=App(root,self.path,auto=False,source_factory=no_source)
        app.account,app.groups,app.loaded="account",self.groups,True
        app.selected_names={"chosen"}
        app.since.set("2026-10-10")
        app.until.set("2026-10-10")
        app.make_client=lambda optional=False:Client("fake-only",transport=lambda payload:(calls.append(payload),response())[1])
        main_thread=threading.get_ident()
        def reject(title,text,**kwargs):
            self.assertEqual(threading.get_ident(),main_thread)
            self.assertEqual(calls,[])
            prompts.append(text)
            return False
        def drain():
            deadline=time.monotonic()+6
            while app.busy and time.monotonic()<deadline:
                root.update()
                time.sleep(.01)
            self.assertFalse(app.busy)
        try:
            with patch("app.messagebox.askyesno",side_effect=reject):
                app.save_and_sync()
                drain()
            self.assertEqual(len(prompts),1)
            self.assertIn("2026-10-10",prompts[0])
            self.assertIn("1 个不同素材",prompts[0])
            self.assertEqual(calls,[])
            self.assertEqual(self.store.stats()[0]["count"],1)
            with patch("app.messagebox.askyesno",return_value=True) as dialog:
                app.save_and_sync()
                drain()
            self.assertEqual(dialog.call_count,1)
            self.assertEqual(len(calls),1)
        finally:
            for timer in root.tk.call("after","info"):
                root.after_cancel(timer)
            root.destroy()


if __name__=="__main__":
    unittest.main()
