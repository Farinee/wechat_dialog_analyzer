import gc
import sys
from pathlib import Path
import tkinter as tk
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"src"))
from markdown_view import MarkdownText


class MarkdownTests(unittest.TestCase):
    def tearDown(self):
        # Tk objects may form cycles after destroy(). Collect after test locals
        # are released, before later media tests start worker threads.
        gc.collect()

    def test_streaming_updates_preserve_reading_position_and_follow_only_at_bottom(self):
        root=tk.Tk()
        root.geometry("600x350")
        try:
            view=MarkdownText(root)
            view.pack(fill="both",expand=True)
            answer="\n".join(f"第{i}行内容" for i in range(180))
            view.render_conversation([{"question":"测试","answer":answer}])
            root.update()
            view.yview("30.0")
            root.update()
            top=view.index("@0,0")
            view.render_conversation([{"question":"测试","answer":answer+"\n新增内容"}],preserve_view=True)
            root.update()
            self.assertEqual(view.index("@0,0"),top)
            view.see("end")
            root.update()
            view.render_conversation([{"question":"测试","answer":answer+"\n新增内容"*30}],preserve_view=True)
            root.update()
            self.assertIsNotNone(view.dlineinfo("end-1c"))
        finally:
            root.destroy()

    def test_headings_inline_code_lists_tables_links_and_safe_literal_html(self):
        root=tk.Tk()
        root.withdraw()
        try:
            view=MarkdownText(root)
            answer=("# 标题\n**加粗** 和 *斜体* 与 ~~删除~~\n- 一项\n1. 二项\n> 引用\n"
                "```python\nprint('**保持原样**')\n```\n"
                "| 名称 | 金额 |\n| --- | ---: |\n| 预算 | 100万元 |\n"
                "[链接](https://example.invalid/test)\n<script>literal</script>")
            with patch("markdown_view.webbrowser.open") as browser:
                view.render_conversation([{"question":"测试","answer":answer}])
                browser.assert_not_called()
            content=view.get("1.0","end")
            self.assertIn("标题",content)
            self.assertNotIn("# 标题",content)
            self.assertIn("• 一项",content)
            self.assertIn("print('**保持原样**')",content)
            self.assertIn("预算\t100万元",content)
            self.assertIn("<script>literal</script>",content)
            for tag in ("h1","bold","italic","strike","quote","code","table","link"):
                self.assertTrue(view.tag_ranges(tag),tag)
            view.render_conversation([])
            self.assertEqual(view._actions,[])
        finally:
            root.destroy()


if __name__=="__main__":
    unittest.main()
