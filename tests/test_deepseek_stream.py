"""Synthetic SSE only: no uploads, paid requests, or real network access."""
import gc
from contextlib import closing
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from conversation import Conversations,model_history,export_markdown,unfinished_turn
from deepseek_api import Client,APIError
from deepseek_stream import read_stream,STREAM_IDLE_SECONDS,STREAM_WAIT_SECONDS
from storage import Store,epoch


def event(content=None,*,reasoning=None,finish=None):
    delta={}
    if content is not None: delta["content"]=content
    if reasoning is not None: delta["reasoning_content"]=reasoning
    payload={"choices":[{"index":0,"delta":delta,"finish_reason":finish}]}
    return ("data: "+json.dumps(payload,ensure_ascii=False)+"\r\n\r\n").encode()


class Response:
    def __init__(self,chunks,on_read=lambda:None):
        self.chunks=iter(chunks)
        self.on_read=on_read
    def read(self,n=-1):
        self.on_read()
        chunk=next(self.chunks,b"")
        if isinstance(chunk,Exception): raise chunk
        return chunk
    read1=read
    def __enter__(self): return self
    def __exit__(self,*args): pass


class StreamTests(unittest.TestCase):
    def setUp(self):
        gc.collect()
        (ROOT/"work"/"test-data").mkdir(parents=True,exist_ok=True)
    def tearDown(self): gc.collect()

    def test_sse_comments_split_utf8_thinking_usage_and_done(self):
        raw=(b": keep-alive\r\n\r\n"+event(reasoning="不保存思考原文")+event("中文回答 ")+event("[M1]")+
             event(finish="stop")+b'data: {"choices":[],"usage":{"total_tokens":123}}\n\n'+b"data: [DONE]\n\n")
        partial=[]
        reasoning=[]
        result=read_stream(Response([raw[i:i+1] for i in range(len(raw))]),progress=lambda text:None,
            on_partial=partial.append,on_reasoning=reasoning.append,stop=None,api_error=APIError)
        self.assertEqual(result["choices"][0]["message"]["content"],"中文回答 [M1]")
        self.assertEqual(result["usage"]["total_tokens"],123)
        self.assertEqual(partial[-1],"中文回答 [M1]")
        self.assertNotIn("不保存思考原文","".join(partial))
        self.assertEqual(result["choices"][0]["message"]["reasoning_content"],"不保存思考原文")
        self.assertEqual(reasoning[-1],"不保存思考原文")

    def test_stream_survives_keepalives_beyond_old_180_second_deadline(self):
        now=[0]
        def advance(): now[0]+=120
        response=Response([b": keep-alive\n\n"]*3+[event("回答"),event(finish="stop"),b"data: [DONE]\n\n"],advance)
        statuses=[]
        with patch("deepseek_stream.time.monotonic",side_effect=lambda:now[0]),patch("deepseek_api.urllib.request.urlopen",return_value=response) as network:
            result=Client("fake-only").complete([{"role":"user","content":"合成输入"}],stream=True,progress=statuses.append)
        self.assertEqual(result,"回答")
        request=network.call_args.args[0]
        self.assertTrue(json.loads(request.data)["stream"])
        self.assertEqual(network.call_args.kwargs["timeout"],STREAM_IDLE_SECONDS)
        self.assertTrue(any("连接正常" in text for text in statuses))
        self.assertTrue(any("正在接收回答" in text for text in statuses))

    def test_disconnect_saves_partial_without_retry_or_false_success(self):
        partial=[]
        response=Response([event("已经收到的片段"),TimeoutError("synthetic")])
        with patch("deepseek_api.urllib.request.urlopen",return_value=response) as network:
            with self.assertRaisesRegex(APIError,"未自动重发"):
                Client("fake-only").complete([{"role":"user","content":"合成输入"}],stream=True,on_partial=partial.append)
        self.assertEqual(network.call_count,1)
        self.assertEqual(partial[-1],"已经收到的片段")

    def test_premature_eof_and_invalid_frames_never_become_complete_answers(self):
        for last in (b"",b"data: bad-json\n\n",b"data: [DONE]\n\n"):
            partial=[]
            with self.assertRaises(APIError):
                read_stream(Response([event("已收到"),last]),progress=lambda text:None,on_partial=partial.append,stop=None,api_error=APIError)
            self.assertEqual(partial[-1],"已收到")

    def test_overall_deadline_and_stop_preserve_partial(self):
        for cancel in (False,True):
            now=[0]
            stop=threading.Event()
            reads=[0]
            def advance():
                reads[0]+=1
                if reads[0]==2:
                    if cancel: stop.set()
                    else: now[0]=STREAM_WAIT_SECONDS+1
            partial=[]
            with patch("deepseek_stream.time.monotonic",side_effect=lambda:now[0]):
                with self.assertRaisesRegex(APIError,"未自动重发"):
                    read_stream(Response([event("片段"),b": keep-alive\n\n"],advance),progress=lambda text:None,
                        on_partial=partial.append,stop=stop,api_error=APIError)
            self.assertEqual(partial[-1],"片段")

    def test_output_length_limit_preserves_text_instead_of_retrying(self):
        partial=[]
        with patch("deepseek_api.urllib.request.urlopen",return_value=Response([event("长度上限前的回答"),event(finish="length"),b"data: [DONE]\n\n"])) as network:
            with self.assertRaisesRegex(RuntimeError,"输出长度上限"):
                Client("fake-only").complete([{"role":"user","content":"合成输入"}],stream=True,on_partial=partial.append)
        self.assertEqual(network.call_count,1)
        self.assertEqual(partial[-1],"长度上限前的回答")

    def test_old_database_migration_preserves_turns_and_partial_survives_restart(self):
        import sqlite3
        with tempfile.TemporaryDirectory(dir=ROOT/"work"/"test-data") as folder:
            path=Path(folder)/"conversations.sqlite3"
            with closing(sqlite3.connect(path)) as conn:
                conn.execute("CREATE TABLE requests(id TEXT PRIMARY KEY,session TEXT,question TEXT,status TEXT,created TEXT,updated TEXT,error TEXT NOT NULL DEFAULT '',coverage TEXT NOT NULL DEFAULT '{}')")
            conv=Conversations(path)
            scope=("chosen",1,2)
            sid=conv.current(scope)
            conv.append(scope,sid,{"question":"旧问题","answer":"旧回答"})
            rid=conv.begin_request(scope,sid,"新问题")
            conv.update_request(rid,partial="已收到的回答片段")
            conv.update_request(rid,partial_reasoning="已收到的模型思考")
            conv.update_request(rid,error="synthetic disconnect")
            restored=Conversations(path)
            self.assertEqual(restored.unfinished(scope,sid)["partial"],"已收到的回答片段")
            self.assertEqual(restored.unfinished(scope,sid)["partial_reasoning"],"已收到的模型思考")
            self.assertEqual(len(model_history(restored.turns(scope,sid))),2)
            self.assertEqual(len(restored.turns(scope,sid)),1)

    def test_ui_displays_and_restores_interrupted_partial_without_resending(self):
        import tkinter as tk
        from app import App
        with tempfile.TemporaryDirectory(dir=ROOT/"work"/"test-data") as folder:
            root=tk.Tk()
            root.withdraw()
            app=App(root,Path(folder),auto=False,source_factory=lambda *args:None)
            app.account,app.groups,app.loaded="account",[{"username":"chosen","name":"示例群"}],True
            app.selected_names={"chosen"}
            app.since.set("2026-10-10")
            app.until.set("2026-10-10")
            app.store.set_selection("account",app.groups,{"chosen"},epoch("2026-10-10"))
            chat=Store.chat_id("account","chosen")
            app.store.put(chat,[{"local_id":1,"shard":"a","sort_seq":1,"create_time":epoch("2026-10-10")+1,"type":"文本","content":"合成证据"}])
            app.show_local()
            app.make_client=lambda:Client("fake-only")
            app.question.insert("1.0","合成问题")
            try:
                response=Response([event(reasoning="流式模型思考原文"),event("**已经收到**的回答 [M1]"),TimeoutError("synthetic")])
                with patch("deepseek_api.urllib.request.urlopen",return_value=response) as network:
                    app.ask()
                    deadline=time.monotonic()+5
                    while app.busy and time.monotonic()<deadline:
                        root.update()
                        time.sleep(.01)
                    self.assertFalse(app.busy)
                    self.assertEqual(network.call_count,1)
                    self.assertIn("已经收到",app.answer_text.get("1.0","end"))
                    self.assertIn("未完成回答",app.answer_text.get("1.0","end"))
                    self.assertEqual(app.conversation_turns,[])
                    app.load_conversation()
                    self.assertEqual(app.partial_turn["reasoning_content"],"流式模型思考原文")
                    self.assertIn("已经收到",app.answer_text.get("1.0","end"))
                    self.assertIn("合成问题",app.question.get("1.0","end"))
                    self.assertEqual(network.call_count,1)
                    output=Path(folder)/"interrupted.md"
                    with patch("app.filedialog.asksaveasfilename",return_value=str(output)):
                        app.export_answer()
                    exported=output.read_text(encoding="utf-8")
                    self.assertIn("模型思考（API 返回，未完成片段）",exported)
                    self.assertIn("流式模型思考原文",exported)
                    self.assertIn("已经收到",exported)
            finally:
                for timer in root.tk.call("after","info"): root.after_cancel(timer)
                root.destroy()

    def test_reasoning_only_disconnect_is_saved_and_exportable_after_restart(self):
        with tempfile.TemporaryDirectory(dir=ROOT/"work"/"test-data") as folder:
            path=Path(folder)/"conversations.sqlite3"
            conv=Conversations(path)
            scope=("chosen",1,2)
            sid=conv.current(scope)
            rid=conv.begin_request(scope,sid,"合成问题")
            response=Response([event(reasoning="只返回了思考"),event(reasoning="，尚无正文"),TimeoutError("synthetic")])
            with patch("deepseek_api.urllib.request.urlopen",return_value=response) as network:
                with self.assertRaises(APIError):
                    Client("fake-only").complete([{"role":"user","content":"合成输入"}],stream=True,
                        on_reasoning=lambda text:conv.update_request(rid,partial_reasoning=text))
            conv.update_request(rid,error="synthetic disconnect")
            restored=Conversations(path)
            pending=unfinished_turn(restored.unfinished(scope,sid))
            self.assertEqual(pending["reasoning_content"],"只返回了思考，尚无正文")
            self.assertTrue(pending["incomplete"])
            self.assertEqual(model_history([pending]),[])
            self.assertIn("只返回了思考，尚无正文",export_markdown([pending]))
            self.assertEqual(restored.turns(scope,sid),[])
            self.assertEqual(network.call_count,1)

    def test_reasoning_survives_output_limit_and_invalid_reasoning_is_rejected(self):
        reasoning=[]
        with patch("deepseek_api.urllib.request.urlopen",return_value=Response([
                event(reasoning="长度上限前的思考"),event(finish="length"),b"data: [DONE]\n\n"])) as network:
            with self.assertRaisesRegex(RuntimeError,"输出长度上限"):
                Client("fake-only").complete([{"role":"user","content":"合成输入"}],stream=True,on_reasoning=reasoning.append)
        self.assertEqual(reasoning[-1],"长度上限前的思考")
        self.assertEqual(network.call_count,1)
        with self.assertRaisesRegex(APIError,"流式回复异常"):
            read_stream(Response([event(reasoning=123)]),progress=lambda _:None,on_partial=lambda _:None,
                on_reasoning=lambda _:None,stop=None,api_error=APIError)

    def test_non_streaming_provider_reasoning_is_separate_and_not_required(self):
        reasoning=[]
        client=Client("fake-only",transport=lambda payload:{"choices":[{"message":{
            "content":"正式回答","reasoning_content":"API 返回的思考"},"finish_reason":"stop"}]})
        self.assertEqual(client.complete([{"role":"user","content":"合成输入"}],on_reasoning=reasoning.append),"正式回答")
        self.assertEqual(reasoning,["API 返回的思考"])
        client.transport=lambda payload:{"choices":[{"message":{"content":"未开启思考的回答"},"finish_reason":"stop"}]}
        self.assertEqual(client.complete([{"role":"user","content":"合成输入"}],on_reasoning=reasoning.append),"未开启思考的回答")
        self.assertEqual(len(reasoning),1)


if __name__=="__main__": unittest.main()
