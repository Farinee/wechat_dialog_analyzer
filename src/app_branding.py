"""Display the approved logo without modifying the original project asset."""
from pathlib import Path
import tkinter as tk
from tkinter import ttk
from PIL import Image,ImageTk


LOGO_PATH=Path(__file__).resolve().parents[1]/"resources"/"branding"/"logo.png"


class AppBranding:
    def __init__(self,root,display,source):
        self.root,self.display,self.source=root,display,source
        self.label=None
        self.cache={}
        self.icons=[ImageTk.PhotoImage(source.resize((size,size),Image.Resampling.LANCZOS),master=root)
                    for size in (256,128,64,32,16)]
        root.iconphoto(True,*self.icons)

    def attach_header(self,parent):
        self.label=ttk.Label(parent,style="App.TLabel")
        self.label.pack(side="left",padx=(0,8))
        self.refresh()

    def refresh(self):
        if self.label is None or not self.label.winfo_exists():
            return
        size=max(1,self.display.px(42))
        if size not in self.cache:
            # Scaling is for display only; the supplied PNG stays untouched.
            self.cache[size]=ImageTk.PhotoImage(self.source.resize((size,size),Image.Resampling.LANCZOS),master=self.root)
        self.label.configure(image=self.cache[size])


def install_branding(root,display,path=LOGO_PATH):
    try:
        with Image.open(path) as image:
            source=image.convert("RGBA")
        branding=AppBranding(root,display,source)
    except (OSError,tk.TclError):
        # A missing optional logo must not block local records or startup.
        return None
    root._chat_branding=branding
    return branding
