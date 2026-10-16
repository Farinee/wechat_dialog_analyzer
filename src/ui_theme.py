"""Shared desktop palette; sizing always follows the existing Display object."""
from tkinter import ttk
from fluent_widgets import FluentElements,apply_window_style

APP_NAME="微信聊天分析助手"
DECODE_LABEL="聊天解码并储存"
ANALYSIS_MODE_LABELS={"rag":"RAG 检索","full":"全文分析"}
COLORS={"canvas":"#f3f3f3","surface":"#ffffff","ink":"#242424","muted":"#616161",
    "accent":"#0f6cbd","hover":"#115ea3","pressed":"#0f548c","tint":"#e8f2fc",
    "border":"#e5e5e5","soft":"#fafafa","disabled":"#f5f5f5"}


def install_theme(root,display):
    style=ttk.Style(root)
    style.theme_use("clam")
    root.configure(background=COLORS["canvas"])
    elements=getattr(root,"_chat_fluent_elements",None)
    if elements is None:
        elements=FluentElements(root,display,COLORS)
        root._chat_fluent_elements=elements
    for key,value in {"background":COLORS["surface"],"foreground":COLORS["ink"],
            "insertBackground":COLORS["accent"],"selectBackground":COLORS["accent"],
            "selectForeground":"#ffffff","highlightBackground":COLORS["border"],
            "highlightColor":COLORS["accent"],"highlightThickness":1,"borderWidth":0}.items():
        root.option_add("*Text."+key,value)
    for key,value in {"background":COLORS["surface"],"foreground":COLORS["ink"],
            "selectBackground":COLORS["accent"],"selectForeground":"#ffffff"}.items():
        root.option_add("*TCombobox*Listbox."+key,value)

    def refresh():
        c=COLORS
        normal=display.font(("Microsoft YaHei UI",10))
        bold=display.font(("Microsoft YaHei UI",10,"bold"))
        style.configure(".",background=c["surface"],foreground=c["ink"],bordercolor=c["border"],
            lightcolor=c["border"],darkcolor=c["border"],focuscolor=c["accent"])
        style.configure("TFrame",background=c["surface"])
        style.configure("App.TFrame",background=c["canvas"])
        style.configure("Card.TFrame",background=c["canvas"],borderwidth=0,relief="flat")
        style.configure("Input.TFrame",background=c["surface"],borderwidth=0,relief="flat")
        style.configure("TLabel",background=c["surface"],foreground=c["ink"])
        style.configure("Muted.TLabel",foreground=c["muted"])
        style.configure("App.TLabel",background=c["canvas"],foreground=c["muted"])
        style.configure("Brand.TLabel",background=c["canvas"],foreground=c["ink"])
        style.configure("Badge.TLabel",background=c["canvas"],foreground=c["accent"],
            padding=display.px((12,6)),font=normal)
        style.configure("TButton",background=c["surface"],foreground=c["ink"],bordercolor=c["border"],
            padding=display.px((12,8)),borderwidth=0,relief="flat",focusthickness=0)
        style.map("TButton",background=[("!disabled",c["surface"]),("disabled",c["surface"])],
            foreground=[("disabled",c["muted"])])
        style.configure("Primary.TButton",background=c["surface"],foreground="#ffffff",bordercolor=c["accent"],
            font=bold,padding=display.px((12,9)))
        style.map("Primary.TButton",background=[("!disabled",c["surface"]),("disabled",c["surface"])],
            foreground=[("disabled",c["muted"]),("!disabled","#ffffff")])
        for name in ("TEntry","TCombobox","TSpinbox"):
            style.configure(name,fieldbackground=c["surface"],background=c["surface"],foreground=c["ink"],
                bordercolor=c["border"],arrowcolor=c["muted"],selectbackground=c["accent"],selectforeground="#ffffff")
            style.map(name,fieldbackground=[("disabled",c["soft"]),("readonly",c["surface"])],
                foreground=[("disabled",c["muted"]),("readonly",c["ink"])],bordercolor=[("focus",c["accent"])])
        style.configure("TEntry",padding=display.px((8,6)))
        style.configure("TCombobox",padding=display.px((8,6)),borderwidth=0,relief="flat")
        style.configure("TSpinbox",font=normal,padding=display.px((4,3)),arrowsize=display.px(14))
        style.configure("TCheckbutton",background=c["surface"],foreground=c["ink"])
        style.map("TCheckbutton",background=[("active",c["surface"])],indicatorbackground=[("selected",c["accent"]),
            ("!selected",c["surface"])],indicatorforeground=[("selected","#ffffff")])
        style.configure("Treeview",background=c["surface"],fieldbackground=c["surface"],foreground=c["ink"],
            borderwidth=0,rowheight=normal.metrics("linespace")+display.px(10))
        style.map("Treeview",background=[("selected",c["tint"])],foreground=[("selected",c["pressed"])])
        style.configure("Treeview.Heading",background=c["soft"],foreground=c["muted"],relief="flat",font=bold,
            padding=display.px((6,7)))
        style.map("Treeview.Heading",background=[("active",c["tint"])])
        style.configure("TNotebook",background=c["surface"],borderwidth=0,tabmargins=0)
        style.configure("TNotebook.Tab",background=c["surface"],foreground=c["muted"],borderwidth=0,
            padding=display.px((14,8)))
        style.map("TNotebook.Tab",background=[("!disabled",c["surface"])],
            foreground=[("selected",c["pressed"]),("active",c["ink"])],
            padding=[("selected",display.px((14,8)))],
            lightcolor=[("selected",c["surface"]),("!selected",c["surface"])])
        style.configure("TLabelframe",background=c["surface"],bordercolor=c["border"],relief="solid")
        style.configure("TLabelframe.Label",background=c["surface"],foreground=c["muted"],font=bold)
        style.configure("TSeparator",background=c["border"])
        for orient in ("Vertical","Horizontal"):
            style.configure(orient+".TPanedwindow",background=c["canvas"],sashwidth=display.px(10))
            style.configure(orient+".TScrollbar",background="#b3b3b3",troughcolor=c["surface"],
                width=display.px(8),arrowsize=display.px(8),borderwidth=0,arrowcolor=c["muted"],relief="flat")
            style.map(orient+".TScrollbar",background=[("active","#929292"),("pressed",c["muted"])])
        elements.apply()
        branding=getattr(root,"_chat_branding",None)
        if branding is not None:
            branding.refresh()

    # Reapply scaled styles when Display detects a different monitor DPI.
    root._chat_theme=refresh
    display.style()
    root.bind("<Map>",lambda event:apply_window_style(root) if event.widget is root else None,add="+")
