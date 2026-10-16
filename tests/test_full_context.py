"""Whole saved scope, chronological overflow and local tokenizer; all APIs mocked."""
import io
import gc
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

from PIL import Image

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from storage import Store,epoch
from deepseek_api import Client
from full_context import prepare_context,answer_full,TokenCounter,context_limit


class CharacterCounter:
    def count(self,text):
        return len(text)


def row(i,text,day="2026-10-10",kind="文本"):
    return {"local_id":i,"shard":"a","sort_seq":i,"create_time":epoch(day)+i,
        "sender_name":"测试成员","type":kind,"content":text}


class FullContextTests(unittest.TestCase):
    def setUp(self):
        # Prior UI fixtures leave Python cycles after Tk.destroy(). Collect on
        # the owning main thread before any API worker can trigger collection.
        gc.collect()
        folder=ROOT/"work"/"test-data"
        folder.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=folder)
        self.store=Store(Path(self.temp.name)/"messages.sqlite3")
        groups=[{"username":"chosen","name":"所选聊天"},{"username":"other","name":"其他聊天"}]
        self.store.set_selection("account",groups,{"chosen"},epoch("2026-10-01"))
        self.chat=Store.chat_id("account","chosen")
        self.scope=(self.chat,epoch("2026-10-10"),epoch("2026-10-10",True))
        self.store.set_scope(*self.scope)
        self.calls=[]
        self.client=Client("fake-only",transport=self.transport)

    def tearDown(self):
        self.temp.cleanup()
        gc.collect()

    def transport(self,payload):
        self.calls.append(payload)
        body=json.loads(payload["messages"][-1]["content"])
        ref=body["supplied_messages"][0]["ref"]
        return {"choices":[{"message":{"content":"完整分析中的依据 ["+ref+"]"},"finish_reason":"stop"}]}

    def test_default_answer_reads_more_than_old_limits_and_never_runs_keyword_planner(self):
        long_text="正文开始"+"完整保留的内容"*1000+"LONG_TEXT_END"
        self.store.put(self.chat,[row(i,f"消息 {i}") for i in range(1,101)]+[row(101,long_text)])
        result=self.client.answer(self.store,"分析全部已保存的讨论")
        self.assertEqual(len(self.calls),1)
        body=json.loads(self.calls[0]["messages"][-1]["content"])
        self.assertEqual(len(body["supplied_messages"]),101)
        self.assertEqual(body["supplied_messages"][-1]["text"],long_text)
        self.assertEqual(body["supplied_messages"][0]["ref"],"M1")
        self.assertFalse(result["has_more"])
        self.assertEqual(result["analysis_coverage"]["omitted_messages"],0)
        self.assertEqual(result["sources"][0]["ref"],"M1")
        self.assertTrue(body["analysis_coverage"]["complete"])
        self.assertNotIn("retrieval_plan",body)

    def test_overflow_keeps_a_contiguous_newest_suffix_and_warns_before_only_api_call(self):
        self.store.put(self.chat,[row(i,str(i)+"x"*600) for i in range(1,101)])
        notices=[]
        def notice(info):
            self.assertEqual(self.calls,[])
            notices.append(info)
        result=answer_full(self.client,self.store,"分析",limit=20000,counter=CharacterCounter(),notice=notice)
        self.assertEqual(len(self.calls),1)
        body=json.loads(self.calls[0]["messages"][-1]["content"])
        info=result["analysis_coverage"]
        self.assertGreater(info["omitted_messages"],0)
        self.assertEqual(info["included_messages"]+info["omitted_messages"],100)
        refs=[int(r["ref"][1:]) for r in body["supplied_messages"]]
        self.assertEqual(refs,list(range(info["omitted_messages"]+1,101)))
        self.assertEqual(body["supplied_messages"][-1]["text"],"100"+"x"*600)
        self.assertTrue(result["answer"].startswith("> 容量提示："))
        self.assertIn("实际发送范围",notices[0]["notice"])
        self.assertEqual(self.store.stats()[0]["count"],100)
        self.assertEqual(body["coverage"][0]["count"],100)
        self.assertLessEqual(info["estimated_input_tokens"]+info["reserved_output_tokens"]+info["safety_tokens"],20000)

    def test_only_frozen_chat_dates_are_read_and_empty_scope_fails_closed(self):
        self.store.put(self.chat,[row(1,"ONLY_CHOSEN"),row(2,"BEFORE_RANGE","2026-10-09"),row(3,"AFTER_RANGE","2026-10-11")])
        other=Store.chat_id("account","other")
        self.store.put(other,[row(1,"OTHER_CHAT")])
        def progress(_):
            self.store.set_scope(other,epoch("2026-10-01"),epoch("2026-10-31",True))
        prepared=prepare_context(self.client,self.store,"分析",counter=CharacterCounter(),progress=progress)
        sent=prepared["messages"][-1]["content"]
        self.assertIn("ONLY_CHOSEN",sent)
        for secret in ("BEFORE_RANGE","AFTER_RANGE","OTHER_CHAT"):
            self.assertNotIn(secret,sent)
        self.assertEqual(prepared["scope"],self.scope)
        self.store.scope=None
        with self.assertRaises(ValueError): prepare_context(self.client,self.store,"分析")

    def test_saved_asset_dictionary_is_complete_once_and_raw_media_xml_stays_local(self):
        data=io.BytesIO()
        Image.new("RGB",(8,8),"red").save(data,format="PNG")
        raw=data.getvalue()
        xml='<msg><emoji md5="'+'a'*32+'" cdnurl="https://qpic.cn/raw-url-secret" aeskey="raw-key-secret"/></msg>'
        self.store.put(self.chat,[row(1,xml,kind="动画表情"),row(2,xml,kind="动画表情")])
        ids=self.store.search([])["messages"]
        asset=self.store.media.register(raw,raw,"image/png")
        for message in ids:
            self.store.media.link(message["id"],asset,"emoji")
        full_description="完整素材解读"*2200+"ASSET_ANALYSIS_END"
        self.store.media.save_analysis(asset["hash"],{"summary":full_description},"synthetic-model","test")
        prepared=prepare_context(self.client,self.store,"分析",counter=CharacterCounter())
        body=json.loads(prepared["messages"][-1]["content"])
        self.assertEqual(len(body["supplied_messages"]),2)
        self.assertEqual(len(body["supplied_assets"]),1)
        self.assertIn("ASSET_ANALYSIS_END",body["supplied_assets"][asset["hash"]]["description"])
        self.assertGreater(len(body["supplied_assets"][asset["hash"]]["description"]),8000)
        self.assertNotIn("raw-url-secret",prepared["messages"][-1]["content"])
        self.assertNotIn("raw-key-secret",prepared["messages"][-1]["content"])

    def test_history_question_system_and_output_reservation_reduce_available_capacity(self):
        self.store.put(self.chat,[row(i,"x"*600) for i in range(1,101)])
        normal=prepare_context(self.client,self.store,"分析",limit=30000,counter=CharacterCounter())
        self.client.system_prompt="系统提示"*1000
        self.client.max_tokens=8000
        reduced=prepare_context(self.client,self.store,"分析",limit=30000,counter=CharacterCounter(),
            history=[{"role":"user","content":"历史问题"*1000},{"role":"assistant","content":"旧回答"*1000}])
        self.assertLess(reduced["info"]["included_messages"],normal["info"]["included_messages"])
        self.assertEqual(reduced["info"]["reserved_output_tokens"],8000)
        self.assertEqual(len(reduced["messages"]),4)

    def test_an_oversized_newest_record_does_not_silently_skip_it_or_send_partial_body(self):
        self.store.put(self.chat,[row(1,"旧的小消息"),row(2,"x"*20000)])
        with self.assertRaisesRegex(RuntimeError,"最新单条消息"):
            answer_full(self.client,self.store,"分析",limit=8192,counter=CharacterCounter())
        self.assertEqual(self.calls,[])
        self.assertEqual(self.store.stats()[0]["count"],2)

    def test_stop_after_notice_prevents_api_request_and_followup_reads_new_messages(self):
        self.store.put(self.chat,[row(1,"第一次的内容")])
        stop=threading.Event()
        with self.assertRaisesRegex(RuntimeError,"已停止分析"):
            answer_full(self.client,self.store,"分析",stop=stop,notice=lambda _:stop.set(),counter=CharacterCounter())
        self.assertEqual(self.calls,[])
        self.client.answer(self.store,"第一次")
        self.store.put(self.chat,[row(2,"后来新增的内容")])
        self.client.answer(self.store,"追问",history=[{"role":"user","content":"第一次"},{"role":"assistant","content":"旧回答"}])
        second=json.loads(self.calls[-1]["messages"][-1]["content"])
        self.assertEqual(second["analysis_coverage"]["included_messages"],2)
        self.assertIn("后来新增的内容",second["supplied_messages"][-1]["text"])

    def test_local_tokenizers_and_context_limit_validation(self):
        for model in ("deepseek-flash","deepseek-v4-pro"):
            counter=TokenCounter(model)
            self.assertGreater(counter.count("中文和 English，123"),0)
            self.assertGreater(counter.count("<｜User｜>"),1)
        self.assertEqual(context_limit("1000000"),1_000_000)
        for invalid in (0,8191,1_000_001,True,1.5,"abc"):
            with self.assertRaises(ValueError): context_limit(invalid)

    def test_ui_uses_full_context_by_default_and_persists_overflow_notice(self):
        import tkinter as tk
        from app import App
        root=tk.Tk()
        root.withdraw()
        def no_wechat(*args,**kwargs):
            self.fail("Analysis must read the saved index only")
        app=App(root,Path(self.temp.name),auto=False,source_factory=no_wechat)
        app.account,app.groups,app.loaded="account",[{"username":"chosen","name":"所选聊天"}],True
        app.selected_names={"chosen"}
        app.since.set("2026-10-10")
        app.until.set("2026-10-10")
        app.config["context_limit"]=8192
        app.make_client=lambda:self.client
        app.store.put(self.chat,[row(i,"独立的完整消息内容。"*160) for i in range(1,101)])
        app.question.insert("1.0","分析全部")
        try:
            app.ask()
            deadline=time.monotonic()+8
            while app.busy and time.monotonic()<deadline:
                root.update()
                time.sleep(.01)
            self.assertFalse(app.busy)
            self.assertEqual(len(self.calls),1)
            turn=app.conversation_turns[-1]
            self.assertGreater(turn["analysis_coverage"]["omitted_messages"],0)
            self.assertIn("容量提示",app.answer_text.get("1.0","end"))
            self.assertIn("实际发送范围",turn["source_text"])
            self.assertEqual(app.store.stats()[0]["count"],100)
            self.assertIn("排除较早",app.status.get())
        finally:
            for timer in root.tk.call("after","info"):
                root.after_cancel(timer)
            root.destroy()


if __name__=="__main__":
    unittest.main()
