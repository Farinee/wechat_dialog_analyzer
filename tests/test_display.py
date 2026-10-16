import ctypes
from pathlib import Path
import sys
import tempfile
import tkinter as tk
from tkinter import font as tkfont,ttk
import unittest
from unittest.mock import Mock,patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from app import App
from display import Display,enable_high_dpi
from settings_ui import SettingsDialog
from fluent_widgets import apply_window_style


class DisplayTests(unittest.TestCase):
    def setUp(self):
        folder=ROOT/"work"/"test-data"
        folder.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=folder)
        self.root=tk.Tk()
        self.root.withdraw()

    def tearDown(self):
        for timer in self.root.tk.call("after","info"):
            self.root.after_cancel(timer)
        self.root.destroy()
        self.temp.cleanup()

    def test_native_awareness_is_pointer_safe_and_respects_already_set_context(self):
        api=Mock()
        api.SetProcessDpiAwarenessContext.return_value=False
        with patch("display.sys.platform","win32"),patch("display.ctypes.WinDLL",return_value=api) as library,patch("display.ctypes.get_last_error",return_value=5):
            enable_high_dpi()
        self.assertEqual(library.call_count,1)
        self.assertEqual(api.SetProcessDpiAwarenessContext.argtypes,[ctypes.c_void_p])
        self.assertEqual(api.SetProcessDpiAwarenessContext.call_args.args[0].value,ctypes.c_void_p(-4).value)

    def test_175_percent_scales_tree_rows_columns_and_all_font_controls(self):
        self.enterContext(patch("display.window_dpi",return_value=168))
        self.enterContext(patch("display.work_area",return_value=(0,0,3840,2080)))
        app=App(self.root,Path(self.temp.name),auto=False)
        self.root.deiconify()
        self.root.update()
        self.assertAlmostEqual(app.display.scale,1.75)
        font=tkfont.Font(root=self.root,font=ttk.Style(self.root).lookup("Treeview","font"))
        rowheight=int(ttk.Style(self.root).lookup("Treeview","rowheight"))
        self.assertGreaterEqual(rowheight,font.metrics("linespace")+12)
        self.assertEqual(app.tree.column("count","width"),round(78*1.75))
        self.assertGreater(self.root.winfo_width(),2000)
        self.assertLess(self.root.winfo_width(),3840)
        for entry in app.date_entries:
            self.assertGreaterEqual(entry.winfo_height(),font.metrics("linespace"))

    def test_constrained_200_percent_workspace_keeps_controls_reachable_and_settings_save_visible(self):
        with patch("display.window_dpi",return_value=192),patch("display.work_area",return_value=(-1920,0,1920,1040)):
            app=App(self.root,Path(self.temp.name),auto=False)
            self.root.deiconify()
            self.root.update()
            self.assertLessEqual(self.root.winfo_width(),1920)
            self.assertLessEqual(self.root.winfo_height(),1040-72)
            self.assertGreater(app.sidebar.content.winfo_reqheight(),app.sidebar.canvas.winfo_height())
            self.assertTrue(app.sidebar.bar.winfo_ismapped())
            app.sidebar.canvas.yview_moveto(1)
            self.root.update()
            view_bottom=app.sidebar.canvas.winfo_rooty()+app.sidebar.canvas.winfo_height()
            button_bottom=app.refresh_button.winfo_rooty()+app.refresh_button.winfo_height()
            self.assertLessEqual(button_bottom,view_bottom)
            self.assertGreaterEqual(button_bottom,app.sidebar.canvas.winfo_rooty())
            dialog=SettingsDialog(self.root,Path(self.temp.name),{},lambda _:None)
            self.root.update()
            buttons=[]
            def walk(widget):
                if isinstance(widget,ttk.Button) and widget.cget("text")=="保存设置":
                    buttons.append(widget)
                for child in widget.winfo_children():
                    walk(child)
            walk(dialog)
            self.assertEqual(len(buttons),1)
            save=buttons[0]
            self.assertTrue(save.winfo_ismapped())
            self.assertLessEqual(save.winfo_rooty()+save.winfo_height(),dialog.winfo_rooty()+dialog.winfo_height())
            dialog.destroy()

    def test_monitor_dpi_change_updates_existing_fonts_and_geometry_without_drift(self):
        display=Display(self.root,dpi=96)
        frame=ttk.Frame(self.root,padding=10)
        frame.pack(fill="both",expand=True,padx=8)
        text=tk.Text(frame,font=("Microsoft YaHei UI",11),padx=12,pady=10)
        text.pack(fill="both",expand=True)
        display.adapt(frame)
        font=tkfont.Font(root=self.root,font=text.cget("font"))
        before=font.metrics("linespace")
        with patch("display.window_dpi",return_value=192),patch("display.work_area",return_value=(0,0,3840,2080)):
            display.check_monitor()
        self.assertEqual(int(text.cget("padx")),24)
        self.assertGreater(tkfont.Font(root=self.root,font=text.cget("font")).metrics("linespace"),before*1.7)
        with patch("display.window_dpi",return_value=96),patch("display.work_area",return_value=(0,0,1920,1040)):
            display.check_monitor()
        self.assertEqual(int(text.cget("padx")),12)
        self.assertEqual(tkfont.Font(root=self.root,font=text.cget("font")).metrics("linespace"),before)

    def test_fluent_images_follow_monitor_dpi_reuse_resources_and_keep_controls_native(self):
        with patch("display.window_dpi",return_value=96),patch("display.work_area",return_value=(0,0,1920,1040)):
            app=App(self.root,Path(self.temp.name),auto=False)
        resources=self.root._chat_fluent_elements
        first=resources.cache[1.0]["photos"][0]
        with patch("display.window_dpi",return_value=168),patch("display.work_area",return_value=(0,0,3840,2080)):
            app.display.check_monitor()
        larger=resources.cache[1.75]["photos"][0]
        self.assertAlmostEqual(larger.width()/first.width(),1.75,delta=.05)
        with patch("display.window_dpi",return_value=96),patch("display.work_area",return_value=(0,0,1920,1040)):
            app.display.check_monitor()
        self.assertEqual(len(resources.cache),2)
        self.assertIs(resources.cache[1.0]["photos"][0],first)
        invoked=[]
        button=ttk.Button(self.root,text="合成操作",style="Primary.TButton",command=lambda:invoked.append(1))
        button.invoke()
        button.state(("disabled",))
        button.invoke()
        self.assertEqual(invoked,[1])
        combo=ttk.Combobox(self.root,values=("群聊","个人"),state="readonly")
        combo.pack()
        self.root.deiconify()
        self.root.update()
        try:
            combo.event_generate("<Button-1>",x=combo.winfo_width()-10,y=combo.winfo_height()//2)
            self.root.update()
            popup=self.root.tk.call("ttk::combobox::PopdownWindow",str(combo))
            self.assertTrue(self.root.tk.call("winfo","ismapped",popup))
        finally:
            self.root.tk.call("ttk::combobox::Unpost",str(combo))

    def test_fluent_native_window_effects_are_optional_on_older_windows(self):
        with patch("fluent_widgets.sys.platform","win32"),patch("fluent_widgets.sys.getwindowsversion",return_value=Mock(build=19045)),\
                patch("fluent_widgets.ctypes.WinDLL") as library:
            apply_window_style(self.root)
        library.assert_not_called()


if __name__=="__main__":
    unittest.main()
