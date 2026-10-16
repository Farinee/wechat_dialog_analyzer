"""UI regression checks using temporary archives and no external services."""
import gc
import json
from pathlib import Path
import sys
import tempfile
import tkinter as tk
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from app import App
from conversation import model_history
from settings_ui import SettingsDialog


class UIRegressionTests(unittest.TestCase):
    def setUp(self):
        gc.collect()
        folder=ROOT/"work"/"test-data"
        folder.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=folder)
        self.data=Path(self.temp.name)
        self.root=tk.Tk()
        self.root.withdraw()
        self.callback_errors=[]
        self.root.report_callback_exception=lambda *error:self.callback_errors.append(error)
        self.app=App(self.root,self.data,auto=False,
            source_factory=lambda *a,**k:self.fail("UI checks must not open WeChat"))

    def tearDown(self):
        for timer in self.root.tk.call("after","info"):
            self.root.after_cancel(timer)
        self.root.destroy()
        self.app=None
        gc.collect()
        self.temp.cleanup()
        self.assertEqual(self.callback_errors,[])

    def use_conversation(self):
        scope=("synthetic-chat",1,2)
        session=self.app.conversations.current(scope)
        self.app.conversation_scope=scope
        self.app.conversation_id=session
        return scope,session

    def test_mode_selection_and_settings_save_keep_rag_enabled(self):
        app=self.app
        values=tuple(app.context_mode_box.cget("values"))
        rag=next(value for value in values if "RAG" in value)
        app.context_mode_box.current(values.index(rag))
        app.context_mode_box.event_generate("<<ComboboxSelected>>")
        self.root.update()
        self.assertEqual(app.config["context_mode"],"rag")
        saved=json.loads((self.data/"settings.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["context_mode"],"rag")
        app.settings_dialog()
        dialog=next(child for child in self.root.winfo_children() if isinstance(child,SettingsDialog))
        self.assertEqual(dialog.context_mode.get(),rag)
        dialog.save()
        self.assertEqual(app.context_mode.get(),rag)
        self.assertIn(app.context_mode.get(),values)
        full=next(value for value in values if value!=rag)
        app.context_mode_box.current(values.index(full))
        app.context_mode_box.event_generate("<<ComboboxSelected>>")
        self.root.update()
        self.assertEqual(app.config["context_mode"],"full")

    def test_export_keeps_old_interrupted_text_after_later_success(self):
        scope,session=self.use_conversation()
        conv=self.app.conversations
        first=conv.begin_request(scope,session,"第一次提问")
        conv.update_request(first,partial="第一段未完成正文",partial_reasoning="第一段模型思考")
        conv.update_request(first,error="合成网络中断")
        second=conv.begin_request(scope,session,"第二次提问")
        conv.complete_request(scope,session,second,{"question":"第二次提问","answer":"完整回答"})
        self.assertIsNone(conv.unfinished(scope,session))
        self.app.conversation_turns=conv.turns(scope,session)
        target=self.data/"conversation.md"
        with patch("app.filedialog.asksaveasfilename",return_value=str(target)):
            self.app.export_answer()
        exported=target.read_text(encoding="utf-8")
        for text in ("第一段未完成正文","第一段模型思考","完整回答"):
            self.assertIn(text,exported)
        self.assertLess(exported.index("第一次提问"),exported.index("第二次提问"))
        self.assertEqual(exported.count("第一段未完成正文"),1)
        self.assertNotIn("第一段",json.dumps(model_history(conv.turns(scope,session)),ensure_ascii=False))

    def test_export_failure_shows_error_and_preserves_saved_conversation(self):
        scope,session=self.use_conversation()
        self.app.conversations.append(scope,session,{"question":"测试问题","answer":"已保存回答"})
        with patch("app.filedialog.asksaveasfilename",return_value=str(self.data/"export.md")),\
                patch("app.Path.write_text",side_effect=PermissionError("合成写入失败")),\
                patch("app.messagebox.showerror") as error:
            self.app.export_answer()
        error.assert_called_once()
        self.assertIn("未保存",self.app.status.get())
        self.assertEqual(self.app.conversations.turns(scope,session)[0]["answer"],"已保存回答")


if __name__=="__main__":
    unittest.main()
