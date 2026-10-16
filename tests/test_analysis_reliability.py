"""Input headroom, bounded failures and durable conversations; synthetic APIs only."""
import gc
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from storage import Store,epoch
from conversation import Conversations
from deepseek_api import Client,APIError,InputTooLong,read_response
from full_context import prepare_context,answer_full,MAX_REQUEST_BYTES,input_limit


class Characters:
    def count(self,text):
        return len(text)


def row(i,text="合成测试消息"):
    return {"local_id":i,"shard":"a","sort_seq":i,"create_time":epoch("2026-10-10")+i,
        "type":"文本","content":text,"sender_name":"测试成员"}


def response(text="回答 [M1]"):
    return {"choices":[{"message":{"content":text},"finish_reason":"stop"}]}


class ReliabilityTests(unittest.TestCase):
    def setUp(self):
        gc.collect()
        folder=ROOT/"work"/"test-data"
        folder.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=folder)
        self.path=Path(self.temp.name)
        self.store=Store(self.path/"messages.sqlite3")
        self.groups=[{"username":"chosen","name":"示例群"}]
        self.store.set_selection("account",self.groups,{"chosen"},epoch("2026-10-01"))
        self.chat=Store.chat_id("account","chosen")
        self.scope=(self.chat,epoch("2026-10-10"),epoch("2026-10-10",True))
        self.store.set_scope(*self.scope)
        self.client=Client("fake-only",transport=lambda _:response())

    def tearDown(self):
        self.temp.cleanup()
        gc.collect()

    def test_input_ceiling_and_quarter_window_reserve_both_apply(self):
        self.store.put(self.chat,[row(i,"x"*3500) for i in range(1,251)])
        prepared=prepare_context(self.client,self.store,"分析",counter=Characters())
        info=prepared["info"]
        self.assertLessEqual(info["estimated_input_tokens"],800000)
        self.assertEqual(info["input_token_limit"],800000)
        self.assertLessEqual(info["estimated_input_tokens"]+info["safety_tokens"]+info["reserved_output_tokens"],info["context_limit"])
        self.assertEqual(info["safety_tokens"],250000)
        self.assertGreater(info["omitted_messages"],0)
        self.assertEqual(info["included_messages"]+info["omitted_messages"],250)
        self.assertLessEqual(info["request_bytes"],MAX_REQUEST_BYTES)
        self.assertEqual(input_limit("800000"),800000)
        for invalid in (4095,800001,True,1.5,"invalid"):
            with self.assertRaises(ValueError): input_limit(invalid)

    def test_utf8_request_bytes_can_require_further_cutting_than_tokens(self):
        self.store.put(self.chat,[row(i,"测"*15000) for i in range(1,51)])
        token_only=prepare_context(self.client,self.store,"分析",counter=Characters(),
            max_input_tokens=800000,max_request_bytes=20*1024*1024)
        guarded=prepare_context(self.client,self.store,"分析",counter=Characters(),max_input_tokens=800000,
            max_request_bytes=2*1024*1024)
        self.assertLess(guarded["info"]["included_messages"],token_only["info"]["included_messages"])
        outgoing=self.client.build_payload(guarded["messages"],stream=True)
        self.assertEqual(len(json.dumps(outgoing,ensure_ascii=False).encode("utf-8")),guarded["info"]["request_bytes"])
        self.assertLessEqual(guarded["info"]["request_bytes"],MAX_REQUEST_BYTES)
        self.assertTrue(all(len(r["text"])==15000 for r in guarded["sources"]))

    def test_automatic_thinking_text_budget_leaves_more_room_for_the_final_answer(self):
        calls=[]
        def transport(payload):
            calls.append(payload)
            if payload["max_tokens"]<32768:
                return {"choices":[{"message":{"content":"","reasoning_content":"模拟较长思考"},"finish_reason":"length"}]}
            return {"choices":[{"message":{"content":"最终回答","reasoning_content":"模拟较长思考"},"finish_reason":"stop"}]}
        client=Client("fake-only",thinking="enabled",reasoning_effort="low",transport=transport)
        self.assertEqual(client.complete([{"role":"user","content":"合成问题"}]),"最终回答")
        self.assertEqual(calls[0]["max_tokens"],131072)
        self.assertEqual(calls[0]["reasoning_effort"],"low")
        self.assertEqual(len(calls),1)

    def test_explicit_budgets_and_auxiliary_vision_defaults_remain_authoritative(self):
        messages=[{"role":"user","content":"合成问题"}]
        client=Client("fake-only",thinking="enabled",reasoning_effort="low")
        self.assertEqual(client.build_payload(messages,json_mode=True)["max_tokens"],16384)
        self.assertEqual(client.build_payload(messages,vision=True)["max_tokens"],16384)
        client.max_tokens=8192
        self.assertEqual(client.build_payload(messages)["max_tokens"],8192)
        client=Client("fake-only",thinking="disabled")
        self.assertEqual(client.build_payload(messages)["max_tokens"],3500)

    def test_larger_automatic_output_is_reserved_before_any_context_is_sent(self):
        self.store.put(self.chat,[row(i,"完整原文"*750) for i in range(1,51)])
        client=Client("fake-only",thinking="enabled",reasoning_effort="low",transport=lambda _:response())
        prepared=prepare_context(client,self.store,"分析",counter=Characters(),limit=200000)
        info=prepared["info"]
        self.assertEqual(info["reserved_output_tokens"],131072)
        self.assertLessEqual(info["estimated_input_tokens"]+info["reserved_output_tokens"]+info["safety_tokens"],200000)
        self.assertGreater(info["omitted_messages"],0)

    def test_manual_output_limits_accept_384k_and_reject_values_above_provider_limit(self):
        for model in ("deepseek-flash","deepseek-v4-pro"):
            client=Client("fake-only",model=model,max_tokens=393216)
            self.assertEqual(client.build_payload([{"role":"user","content":"合成问题"}])["max_tokens"],393216)
        for invalid in (393217,511,True,1.5,"393216"):
            with self.assertRaises(ValueError): Client("fake-only",max_tokens=invalid)

    def test_maximum_output_budget_is_reserved_before_sending_text(self):
        self.store.put(self.chat,[row(1)])
        client=Client("fake-only",max_tokens=393216,transport=lambda _:response())
        info=prepare_context(client,self.store,"分析",counter=Characters())["info"]
        self.assertEqual(info["reserved_output_tokens"],393216)
        self.assertLessEqual(info["estimated_input_tokens"]+info["reserved_output_tokens"]+info["safety_tokens"],1000000)

    def test_explicit_oversize_rejection_shrinks_and_keeps_original_scope(self):
        self.store.put(self.chat,[row(i,"x"*600) for i in range(1,101)])
        calls=[]
        def transport(payload):
            body=json.loads(payload["messages"][-1]["content"])
            calls.append(body)
            if len(calls)==1:
                self.store.set_scope("other",epoch("2026-10-01"),epoch("2026-10-31",True))
                raise InputTooLong("synthetic context_length_exceeded")
            return response("依据 ["+body["supplied_messages"][0]["ref"]+"]")
        client=Client("fake-only",transport=transport)
        result=answer_full(client,self.store,"分析",limit=30000,counter=Characters())
        self.assertEqual(len(calls),2)
        self.assertLess(len(calls[1]["supplied_messages"]),len(calls[0]["supplied_messages"]))
        self.assertEqual(result["analysis_coverage"]["oversize_retries"],1)
        self.assertTrue(all(m["time"].startswith("2026-10-10") for m in calls[1]["supplied_messages"]))

    def test_regular_timeout_is_not_automatically_retried(self):
        self.store.put(self.chat,[row(1)])
        calls=[]
        def transport(payload):
            calls.append(payload)
            raise APIError("synthetic timeout")
        with self.assertRaises(APIError):
            answer_full(Client("fake-only",transport=transport),self.store,"分析",counter=Characters())
        self.assertEqual(len(calls),1)

    def test_repeated_explicit_rejection_has_a_bounded_retry_count(self):
        self.store.put(self.chat,[row(i,"x"*600) for i in range(1,101)])
        calls=[]
        def transport(payload):
            calls.append(payload)
            raise InputTooLong("synthetic oversized input")
        with self.assertRaises((InputTooLong,RuntimeError)):
            answer_full(Client("fake-only",transport=transport),self.store,"分析",counter=Characters(),limit=30000)
        self.assertLessEqual(len(calls),3)

    def test_http_oversize_errors_are_classified_without_leaking_response_text(self):
        for code,body in ((413,b"private-detail"),(400,b'{"error":{"message":"maximum context length private-detail"}}')):
            error=urllib.error.HTTPError("https://api.deepseek.com/chat/completions",code,"synthetic",{},io.BytesIO(body))
            with patch("deepseek_api.urllib.request.urlopen",side_effect=error):
                with self.assertRaises(InputTooLong) as caught:
                    self.client._http({"messages":[]})
            self.assertNotIn("private-detail",str(caught.exception))

    def test_keepalive_whitespace_cannot_extend_response_wait_forever(self):
        class Keepalive:
            def read(self,n): return b" "
            read1=read
        with patch("deepseek_api.time.monotonic",side_effect=[0,1,2,181]):
            with self.assertRaisesRegex(APIError,"180 秒"):
                read_response(Keepalive(),180)
        class Complete:
            chunks=iter([b" \n",json.dumps(response()).encode(),b""])
            def read(self,n): return next(self.chunks)
            read1=read
        with patch("deepseek_api.time.monotonic",return_value=0):
            self.assertEqual(read_response(Complete(),180)["choices"][0]["message"]["content"],"回答 [M1]")

    def test_pending_draft_failed_request_and_completed_reply_survive_restart(self):
        path=self.path/"conversations.sqlite3"
        conv=Conversations(path)
        sid=conv.current(self.scope)
        old={"question":"旧问题","answer":"旧回答"}
        conv.append(self.scope,sid,old)
        failed=conv.begin_request(self.scope,sid,"失败时的问题")
        conv.update_request(failed,error="synthetic timeout")
        restored=Conversations(path)
        self.assertEqual(restored.draft(self.scope,sid),"失败时的问题")
        self.assertEqual(restored.unfinished(self.scope,sid)["status"],"failed")
        self.assertEqual(len(restored.turns(self.scope,sid)),1)
        request_id=restored.begin_request(self.scope,sid,"新问题")
        restored.update_request(request_id,coverage={"included_messages":10})
        payload={"question":"新问题","answer":"新回答","source_text":"已保存原文","analysis_coverage":{"included_messages":10}}
        restored.complete_request(self.scope,sid,request_id,payload)
        restored.complete_request(self.scope,sid,request_id,payload)
        again=Conversations(path)
        self.assertEqual(len(again.turns(self.scope,sid)),2)
        self.assertEqual(again.turns(self.scope,sid)[1]["source_text"],"已保存原文")
        self.assertIsNone(again.unfinished(self.scope,sid))
        self.assertEqual(again.draft(self.scope,sid),"")
        with self.assertRaises(ValueError): again.draft(("other",1,2),sid)
        with self.assertRaises(ValueError): again.complete_request(("other",1,2),sid,request_id,payload)

    def test_ui_autosaves_reply_before_rendering_and_restores_unsent_draft(self):
        import tkinter as tk
        from app import App
        root=tk.Tk()
        root.withdraw()
        entered,release=threading.Event(),threading.Event()
        calls=[]
        def transport(payload):
            calls.append(payload)
            entered.set()
            if not release.wait(5): raise RuntimeError("synthetic gate timeout")
            return response()
        def no_wechat(*args,**kwargs):
            self.fail("Saved-message analysis must not open WeChat")
        app=App(root,self.path,auto=False,source_factory=no_wechat)
        app.account,app.groups,app.loaded="account",self.groups,True
        app.selected_names={"chosen"}
        app.since.set("2026-10-10")
        app.until.set("2026-10-10")
        app.store.put(self.chat,[row(1)])
        app.make_client=lambda:Client("fake-only",transport=transport)
        app.show_local()
        sid=app.conversation_id
        app.conversations.append(self.scope,sid,{"question":"之前的问题","answer":"之前的回答"})
        app.load_conversation()
        app.question.insert("1.0","发送中的问题")
        try:
            app.ask()
            self.assertTrue(entered.wait(4))
            restored=Conversations(self.path/"conversations.sqlite3")
            self.assertEqual(restored.draft(self.scope,sid),"发送中的问题")
            self.assertEqual(restored.unfinished(self.scope,sid)["status"],"pending")
            release.set()
            # Deliberately do not poll Tk: the worker must commit independently.
            deadline=time.monotonic()+5
            while len(restored.turns(self.scope,sid))<2 and time.monotonic()<deadline:
                time.sleep(.01)
            self.assertTrue(app.busy)
            saved=restored.turns(self.scope,sid)
            self.assertEqual(len(saved),2)
            self.assertIn("source_text",saved[-1])
            self.assertIn("analysis_coverage",saved[-1])
            while app.busy and time.monotonic()<deadline:
                root.update()
                time.sleep(.01)
            self.assertFalse(app.busy)
            self.assertEqual(len(restored.turns(self.scope,sid)),2)
            app.question.insert("1.0","尚未发送的草稿")
            deadline=time.monotonic()+.8
            while time.monotonic()<deadline:
                root.update()
                time.sleep(.01)
            self.assertEqual(restored.draft(self.scope,sid),"尚未发送的草稿")
            app.new_conversation()
            self.assertEqual(app.question.get("1.0","end").strip(),"")
            app.session_box.current(app.session_ids.index(sid))
            app.select_conversation()
            self.assertEqual(app.question.get("1.0","end").strip(),"尚未发送的草稿")
            self.assertEqual(len(calls),1)
        finally:
            release.set()
            for timer in root.tk.call("after","info"):
                root.after_cancel(timer)
            root.destroy()


if __name__=="__main__":
    unittest.main()
