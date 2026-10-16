import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from conversation import Conversations,model_history,export_markdown
from storage import Store,epoch
from deepseek_api import Client


class ConversationTests(unittest.TestCase):
    def setUp(self):
        folder=ROOT/"work"/"test-data"
        folder.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=folder)
        self.path=Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_persistence_scope_isolation_history_budget_and_markdown_export(self):
        path=self.path/"conversations.sqlite3"
        conversations=Conversations(path)
        scope=("chosen",1,2)
        sid=conversations.current(scope)
        turn={"question":"预算是多少？","answer":"## 预算\n**100万元** [M1]","source_text":"本轮原文","coverage_text":"当前范围"}
        conversations.append(scope,sid,turn)
        restored=Conversations(path)
        self.assertEqual(restored.current(scope),sid)
        turns=restored.turns(scope,sid)
        self.assertEqual(turns[0]["answer"],turn["answer"])
        self.assertEqual(restored.turns(("chosen",1,3),sid),[])
        with self.assertRaises(ValueError):
            restored.select(("other",1,2),sid)
        with self.assertRaises(ValueError):
            restored.append(("chosen",1,3),sid,turn)
        self.assertIn("历史引用",model_history(turns)[1]["content"])
        self.assertNotIn("[M1]",model_history(turns)[1]["content"])
        self.assertIn("## 预算",export_markdown(turns))
        self.assertLessEqual(sum(len(m["content"]) for m in model_history([turn]*100)),20000)
        second=restored.create(scope)
        self.assertNotEqual(second,sid)
        self.assertEqual(len(restored.sessions(scope)),2)
        self.assertEqual(restored.turns(scope,second),[])

    def test_ui_followups_restore_history_render_markdown_and_keep_ranges_separate(self):
        import tkinter as tk
        from app import App
        def no_source(*args,**kwargs):
            self.fail("Conversations must not open WeChat")
        root=tk.Tk()
        root.withdraw()
        app=App(root,self.path,auto=False,source_factory=no_source)
        groups=[{"username":"chosen","name":"预算群"}]
        app.account,app.groups,app.loaded="test",groups,True
        app.selected_names={"chosen"}
        app.since.set("2026-10-10")
        app.until.set("2026-10-10")
        app.prepare_scope()
        chat=app.store.scope[0]
        app.store.put(chat,[{"local_id":1,"shard":"a","sort_seq":1,"create_time":epoch("2026-10-10"),
            "type":"文本","content":"预算100万元","sender_name":"成员"}])
        calls=[]
        def transport(payload):
            calls.append(payload)
            if "JSON" in payload["messages"][0]["content"]:
                content=json.dumps({"keywords":["预算"],"chat_names":[],"start_date":None,"end_date":None})
            else:
                content="## 预算\n**100万元** [M1]\n\n- 已确认\n`原文`"
            return {"choices":[{"message":{"content":content,"reasoning_content":"独立保存的模型思考"},"finish_reason":"stop"}]}
        app.make_client=lambda:Client("fake-only",transport=transport)
        def ask(question):
            app.question.delete("1.0","end")
            app.question.insert("1.0",question)
            app.ask()
            deadline=time.monotonic()+6
            while app.busy and time.monotonic()<deadline:
                root.update()
                time.sleep(.01)
            self.assertFalse(app.busy)
        try:
            app.show_local()
            sid=app.conversation_id
            ask("预算是多少？")
            ask("把刚才的内容列成清单")
            self.assertEqual(len(app.conversation_turns),2)
            self.assertEqual(app.conversation_turns[-1]["reasoning_content"],"独立保存的模型思考")
            self.assertIn("独立保存的模型思考",export_markdown(app.conversation_turns))
            self.assertNotIn("独立保存的模型思考",json.dumps(calls,ensure_ascii=False))
            text=app.answer_text.get("1.0","end")
            self.assertIn("预算是多少？",text)
            self.assertIn("把刚才的内容列成清单",text)
            self.assertNotIn("**100万元**",text)
            self.assertTrue(app.answer_text.tag_ranges("h2"))
            self.assertTrue(app.answer_text.tag_ranges("bold"))
            self.assertTrue(app.answer_text.tag_ranges("code"))
            self.assertEqual(len(calls),2)
            second_input=json.loads(calls[1]["messages"][-1]["content"])
            self.assertEqual(second_input["analysis_coverage"]["included_messages"],1)
            self.assertEqual(calls[1]["messages"][1]["role"],"user")
            self.assertIn("预算是多少",calls[1]["messages"][1]["content"])
            self.assertIn("历史引用",calls[1]["messages"][2]["content"])
            app.show_local()
            self.assertEqual(len(app.conversation_turns),2)
            app.since.set("2026-10-11")
            app.until.set("2026-10-11")
            app.show_local()
            self.assertEqual(app.conversation_turns,[])
            self.assertNotIn("100万元",app.answer_text.get("1.0","end"))
            app.since.set("2026-10-10")
            app.until.set("2026-10-10")
            app.show_local()
            self.assertEqual(app.conversation_id,sid)
            self.assertEqual(len(app.conversation_turns),2)
            app.new_conversation()
            self.assertEqual(app.conversation_turns,[])
            app.session_box.current(app.session_ids.index(sid))
            app.select_conversation()
            self.assertEqual(len(app.conversation_turns),2)
            app.make_client=lambda:(_ for _ in ()).throw(RuntimeError("模拟失败"))
            ask("失败后重试")
            self.assertEqual(len(app.conversation_turns),2)
            self.assertIn("失败后重试",app.question.get("1.0","end"))
        finally:
            for timer in root.tk.call("after","info"):
                root.after_cancel(timer)
            root.destroy()

    def test_reasoning_is_saved_before_completion_and_exported_verbatim(self):
        conv=Conversations(self.path/"conversations.sqlite3")
        scope=("chosen",1,2)
        sid=conv.current(scope)
        rid=conv.begin_request(scope,sid,"问题")
        reasoning="模型原文\n```python\nx = 1\n```\n## 模型内部小标题"
        conv.update_request(rid,partial_reasoning=reasoning)
        conv.complete_request(scope,sid,rid,{"question":"问题","answer":"正式回答 [M1]"})
        restored=Conversations(self.path/"conversations.sqlite3")
        turns=restored.turns(scope,sid)
        self.assertEqual(turns[0]["reasoning_content"],reasoning)
        self.assertIsNone(restored.unfinished(scope,sid))
        exported=export_markdown(turns)
        self.assertIn("### 模型思考（API 返回）",exported)
        self.assertIn("````text\n"+reasoning+"\n````",exported)
        self.assertIn("正式回答 [M1]",exported)
        self.assertNotIn(reasoning,json.dumps(model_history(turns),ensure_ascii=False))
        with conv.connect() as conn:
            self.assertEqual(conn.execute("SELECT partial_reasoning FROM requests WHERE id=?",(rid,)).fetchone()[0],"")

    def test_export_recovers_all_fragments_without_crossing_scope_or_adding_history(self):
        conv=Conversations(self.path/"conversations.sqlite3")
        scope=("chosen",1,2)
        sid=conv.current(scope)
        first=conv.begin_request(scope,sid,"只收到思考的问题")
        conv.update_request(first,partial_reasoning="完整保留的早期思考",coverage={"notice":"合成输入范围"})
        conv.update_request(first,error="合成中断")
        empty=conv.begin_request(scope,sid,"未收到内容的问题")
        conv.update_request(empty,error="合成中断")
        done=conv.begin_request(scope,sid,"成功的问题")
        payload={"question":"成功的问题","answer":"成功回答"}
        conv.complete_request(scope,sid,done,payload)
        conv.complete_request(scope,sid,done,payload)
        last=conv.begin_request(scope,sid,"仍在接收的问题")
        conv.update_request(last,partial="最后的接收片段")
        other_scope=("chosen",1,3)
        other_sid=conv.current(other_scope)
        other=conv.begin_request(other_scope,other_sid,"其他范围")
        conv.update_request(other,partial="不应混入的内容")
        restored=Conversations(conv.path)
        exported=restored.export_turns(scope,sid)
        self.assertEqual([t["turn_id"] for t in exported],[first,done,last])
        self.assertEqual(exported[0]["coverage_text"],"合成输入范围")
        self.assertEqual(exported[0]["reasoning_content"],"完整保留的早期思考")
        self.assertEqual([t.get("incomplete",False) for t in exported],[True,False,True])
        text=export_markdown(exported)
        self.assertNotIn("不应混入",text)
        self.assertNotIn("未收到内容的问题",text)
        self.assertEqual(len(restored.turns(scope,sid)),1)
        self.assertEqual(model_history(exported),model_history(restored.turns(scope,sid)))
        with self.assertRaises(ValueError):
            restored.export_turns(other_scope,sid)


if __name__=="__main__":
    unittest.main()
