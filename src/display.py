"""Windows DPI awareness, shared Tk metrics and scrollable control panels."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import sys
import tkinter as tk
from tkinter import font as tkfont, ttk


def enable_high_dpi():
    """Call before Tk creates any HWND; do not change Windows user settings."""
    if sys.platform != "win32":
        return
    try:
        api=ctypes.WinDLL("user32",use_last_error=True)
        setter=api.SetProcessDpiAwarenessContext
        setter.argtypes=[ctypes.c_void_p]
        setter.restype=wintypes.BOOL
        if setter(ctypes.c_void_p(-4)) or ctypes.get_last_error()==5:
            return
    except (AttributeError,OSError):
        pass
    try:
        setter=ctypes.WinDLL("shcore").SetProcessDpiAwareness
        setter.argtypes=[ctypes.c_int]
        setter.restype=ctypes.c_long
        if setter(2) in (0,-2147024891):
            return
    except (AttributeError,OSError):
        pass
    try:
        ctypes.WinDLL("user32").SetProcessDPIAware()
    except (AttributeError,OSError):
        pass


def _window_api(window):
    api=ctypes.WinDLL("user32",use_last_error=True)
    api.GetAncestor.argtypes=[wintypes.HWND,wintypes.UINT]
    api.GetAncestor.restype=wintypes.HWND
    return api,api.GetAncestor(window.winfo_id(),2) or window.winfo_id()


def window_dpi(window):
    if sys.platform=="win32":
        try:
            api,hwnd=_window_api(window)
            api.GetDpiForWindow.argtypes=[wintypes.HWND]
            api.GetDpiForWindow.restype=wintypes.UINT
            dpi=api.GetDpiForWindow(hwnd)
            if dpi:
                return float(dpi)
        except (AttributeError,OSError,tk.TclError):
            pass
    return float(window.winfo_fpixels("1i"))


def work_area(window):
    """Monitor work area excludes the taskbar; coordinates may be negative."""
    if sys.platform=="win32":
        class MonitorInfo(ctypes.Structure):
            _fields_=[("cbSize",wintypes.DWORD),("rcMonitor",wintypes.RECT),
                ("rcWork",wintypes.RECT),("dwFlags",wintypes.DWORD)]
        try:
            api,hwnd=_window_api(window)
            api.MonitorFromWindow.argtypes=[wintypes.HWND,wintypes.DWORD]
            api.MonitorFromWindow.restype=wintypes.HANDLE
            api.GetMonitorInfoW.argtypes=[wintypes.HANDLE,ctypes.POINTER(MonitorInfo)]
            api.GetMonitorInfoW.restype=wintypes.BOOL
            info=MonitorInfo()
            info.cbSize=ctypes.sizeof(info)
            if api.GetMonitorInfoW(api.MonitorFromWindow(hwnd,2),ctypes.byref(info)):
                area=info.rcWork
                return area.left,area.top,area.right-area.left,area.bottom-area.top
        except (AttributeError,OSError,tk.TclError):
            pass
    return 0,0,window.winfo_screenwidth(),window.winfo_screenheight()


class Display:
    def __init__(self,root,dpi=None):
        self.root=root
        self.dpi=float(dpi or window_dpi(root))
        self.scale=self.dpi/96
        root.tk.call("tk","scaling",self.dpi/72)
        self.fonts={}
        self.metrics={}
        self.columns={}
        self.timer=None
        self.closed=False
        root._chat_display=self
        for name in ("TkDefaultFont","TkTextFont","TkMenuFont","TkHeadingFont",
                "TkCaptionFont","TkSmallCaptionFont","TkTooltipFont","TkFixedFont"):
            font=tkfont.nametofont(name,root=root)
            font.configure(family="Consolas" if name=="TkFixedFont" else "Microsoft YaHei UI")
            self.fonts[name]=(font,10)
        self.apply_fonts()
        root.bind("<Configure>",self.on_configure,add="+")
        root.bind("<Destroy>",self.on_destroy,add="+")

    def px(self,value):
        if isinstance(value,(tuple,list)):
            return tuple(self.px(v) for v in value)
        return round(float(value)*self.scale)

    def font(self,spec):
        key=str(spec)
        if key not in self.fonts:
            font=tkfont.Font(root=self.root,font=spec)
            size=int(font.cget("size"))
            points=size if size>0 else abs(size)*72/self.dpi
            self.fonts[key]=(font,points)
        font,points=self.fonts[key]
        # Explicit pixels invalidate cached Tk 8.6 fonts when monitor DPI changes.
        font.configure(size=-max(1,round(points*self.dpi/72)))
        return font

    def apply_fonts(self):
        for font,points in self.fonts.values():
            font.configure(size=-max(1,round(points*self.dpi/72)))
        self.root.option_add("*Font","TkDefaultFont")
        self.root.option_add("*TCombobox*Listbox.font","TkDefaultFont")

    def style(self):
        style=ttk.Style(self.root)
        normal=self.fonts["TkDefaultFont"][0]
        heading=self.font(("Microsoft YaHei UI",10,"bold"))
        style.configure(".",font=normal)
        for name in ("TLabel","TButton","TEntry","TCombobox","TCheckbutton","TLabelframe.Label","TNotebook.Tab"):
            style.configure(name,font=normal)
        style.configure("TButton",padding=self.px((9,6)))
        style.configure("TEntry",padding=self.px((4,3)))
        style.configure("TCombobox",padding=self.px((4,3)),arrowsize=self.px(14))
        style.configure("TCheckbutton",indicatorsize=self.px(14),indicatormargin=self.px((2,2,5,2)))
        style.configure("TNotebook.Tab",padding=self.px((10,5)))
        style.configure("Treeview",font=normal,rowheight=normal.metrics("linespace")+self.px(8))
        style.configure("Treeview.Heading",font=heading,padding=self.px((4,5)))
        for orient in ("Vertical","Horizontal"):
            style.configure(orient+".TScrollbar",width=self.px(15),arrowsize=self.px(13))
            style.configure(orient+".TPanedwindow",sashwidth=self.px(6))
        theme=getattr(self.root,"_chat_theme",None)
        if theme is not None:
            theme()

    def distances(self,widget,value):
        parts=value if isinstance(value,(tuple,list)) else widget.tk.splitlist(str(value))
        return tuple(widget.winfo_pixels(v) for v in parts)

    def adapt(self,container):
        """Capture original pixel distances once; fonts and character counts differ."""
        def visit(widget):
            if widget not in self.metrics:
                options={}
                for key in ("padding","padx","pady","borderwidth","highlightthickness","insertwidth","wraplength"):
                    if key in widget.keys():
                        value=widget.cget(key)
                        if str(value):
                            options[key]=self.distances(widget,value)
                if isinstance(widget,tk.Canvas):
                    for key in ("width","height"):
                        options[key]=self.distances(widget,widget.cget(key))
                manager=widget.winfo_manager()
                layout={}
                if manager in ("pack","grid"):
                    info=widget.pack_info() if manager=="pack" else widget.grid_info()
                    for key in ("padx","pady","ipadx","ipady"):
                        if key in info:
                            layout[key]=self.distances(widget,info[key])
                self.metrics[widget]=(options,manager,layout)
                if "font" in widget.keys() and str(widget.cget("font")):
                    widget.configure(font=self.font(widget.cget("font")))
                if isinstance(widget,ttk.Treeview):
                    self.columns[widget]={name:{key:int(widget.column(name,key)) for key in ("width","minwidth")}
                        for name in widget.cget("columns")}
            for child in widget.winfo_children():
                visit(child)
        visit(container)
        self.apply_metrics()

    def apply_metrics(self):
        self.apply_fonts()
        self.style()
        for widget,(options,manager,layout) in list(self.metrics.items()):
            if not widget.winfo_exists():
                self.metrics.pop(widget)
                self.columns.pop(widget,None)
                continue
            for key,values in options.items():
                scaled=self.px(values)
                widget.configure(**{key:scaled[0] if len(scaled)==1 else scaled})
            if manager in ("pack","grid") and widget.winfo_manager()==manager:
                values={key:(self.px(v)[0] if len(v)==1 else self.px(v)) for key,v in layout.items()}
                (widget.pack_configure if manager=="pack" else widget.grid_configure)(**values)
        for tree,columns in self.columns.items():
            for name,values in columns.items():
                tree.column(name,**{key:self.px(value) for key,value in values.items()})

    def fit_window(self,window,size,minimum=(1000,620),owner=None):
        x,y,width,height=work_area(owner or window)
        margin=self.px(22)
        # Leave space for the title bar and resize frame as well as the taskbar.
        available=(max(200,width-2*margin),max(180,height-2*margin-self.px(36)))
        wanted=tuple(min(self.px(v),a) for v,a in zip(size,available))
        window.minsize(*(min(self.px(v),a) for v,a in zip(minimum,available)))
        window.geometry(f"{wanted[0]}x{wanted[1]}{x+(width-wanted[0])//2:+d}{y+margin:+d}")

    def on_configure(self,event):
        if event.widget is self.root and not self.closed:
            if self.timer is not None:
                self.root.after_cancel(self.timer)
            self.timer=self.root.after(150,self.check_monitor)

    def check_monitor(self):
        self.timer=None
        if self.closed:
            return
        dpi=window_dpi(self.root)
        if abs(dpi-self.dpi)>=1:
            self.dpi=dpi
            self.scale=dpi/96
            self.root.tk.call("tk","scaling",dpi/72)
            self.apply_metrics()
            # The old minimum must not force a window off a smaller monitor.
            _,_,width,height=work_area(self.root)
            self.root.minsize(min(self.px(1000),width-self.px(44)),min(self.px(620),height-self.px(80)))

    def on_destroy(self,event):
        if event.widget is self.root:
            self.closed=True
            if self.timer is not None:
                self.root.after_cancel(self.timer)
                self.timer=None


def display_for(window):
    root=window._root()
    return getattr(root,"_chat_display",None) or Display(root)


class ScrollableFrame(ttk.Frame):
    """Keep form controls reachable on screens with little logical workspace."""
    def __init__(self,parent,width=480,**kwargs):
        super().__init__(parent,**kwargs)
        self.columnconfigure(0,weight=1)
        self.rowconfigure(0,weight=1)
        background=ttk.Style(self).lookup("TFrame","background") or "#ffffff"
        self.canvas=tk.Canvas(self,width=width,height=1,borderwidth=0,highlightthickness=0,background=background)
        self.bar=ttk.Scrollbar(self,orient="vertical",command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.bar.set)
        self.canvas.grid(row=0,column=0,sticky="nsew")
        self.content=ttk.Frame(self.canvas)
        self.window=self.canvas.create_window(0,0,window=self.content,anchor="nw")
        self.canvas.bind("<Configure>",self.reflow)
        self.content.bind("<Configure>",self.reflow)
        self.tag="PanelWheel"+str(self)
        self.bind_class(self.tag,"<MouseWheel>",self.wheel)
        self.bind("<Destroy>",self.cleanup,add="+")

    def reflow(self,event=None):
        width,height=self.canvas.winfo_width(),self.canvas.winfo_height()
        wanted=max(self.content.winfo_reqheight(),height)
        self.canvas.itemconfigure(self.window,width=width,height=wanted)
        self.canvas.configure(scrollregion=(0,0,width,wanted))
        if wanted>height+1:
            self.bar.grid(row=0,column=1,sticky="ns")
        else:
            self.bar.grid_remove()
            self.canvas.yview_moveto(0)

    def bind_wheel(self):
        def visit(widget):
            if not isinstance(widget,(ttk.Treeview,tk.Text,tk.Listbox)):
                widget.bindtags((*widget.bindtags(),self.tag))
            for child in widget.winfo_children():
                visit(child)
        visit(self.content)
        self.canvas.bindtags((*self.canvas.bindtags(),self.tag))

    def wheel(self,event):
        if self.canvas.yview()!=(0.0,1.0):
            self.canvas.yview_scroll(-int(event.delta/120) or (-1 if event.delta>0 else 1),"units")
            return "break"

    def cleanup(self,event):
        if event.widget is self:
            self.unbind_class(self.tag,"<MouseWheel>")
