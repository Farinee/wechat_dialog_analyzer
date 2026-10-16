"""DPI-aware rounded ttk elements; native widget behavior remains in ttk."""
from __future__ import annotations
import ctypes
import sys
from tkinter import ttk,TclError
from PIL import Image,ImageDraw,ImageTk


class FluentElements:
    def __init__(self,root,display,colors):
        self.root,self.display,self.colors=root,display,colors
        self.cache={}

    def _surface(self,fill,stroke,*,radius=4,shadow=False,underline=None):
        scale=self.display.scale
        size=max(24,round(32*scale))
        ss=3
        image=Image.new("RGBA",(size*ss,size*ss))
        draw=ImageDraw.Draw(image)
        unit=scale*ss
        edge=max(1,round(unit))
        box=(edge,edge,size*ss-edge-1,size*ss-edge*2-1)
        if shadow:
            draw.rounded_rectangle((edge,edge*2,box[2],size*ss-edge-1),radius=round(radius*unit),fill="#e2e2e2")
        draw.rounded_rectangle(box,radius=max(1,round(radius*unit)),fill=fill,outline=stroke,width=edge)
        if underline:
            y=box[3]-edge
            draw.line((round(5*unit),y,box[2]-round(4*unit),y),fill=underline,width=max(2,round(2*unit)))
        image=image.resize((size,size),Image.Resampling.LANCZOS)
        return ImageTk.PhotoImage(image,master=self.root)

    def _check(self,selected=False,disabled=False,focus=False):
        c=self.colors
        unit=self.display.scale*3
        size=max(16,round(22*self.display.scale))
        image=Image.new("RGBA",(size*3,size*3))
        draw=ImageDraw.Draw(image)
        color=c["muted"] if disabled else c["accent"]
        draw.rounded_rectangle((round(2*unit),round(2*unit),round(19*unit),round(19*unit)),
            radius=round(3*unit),fill=color if selected else c["surface"],
            outline=color if selected or focus else "#858585",width=max(1,round(unit)))
        if selected:
            draw.line([(round(6*unit),round(10*unit)),(round(9*unit),round(13*unit)),
                (round(15*unit),round(7*unit))],fill="#ffffff",width=max(2,round(1.6*unit)),joint="curve")
        if focus:
            draw.rounded_rectangle((0,0,size*3-1,size*3-1),radius=round(4*unit),outline=c["ink"],width=max(1,round(unit)))
        return ImageTk.PhotoImage(image.resize((size,size),Image.Resampling.LANCZOS),master=self.root)

    def _chevron(self,disabled=False):
        scale=self.display.scale
        unit=scale*3
        width,height=round(22*scale),round(18*scale)
        image=Image.new("RGBA",(width*3,height*3))
        draw=ImageDraw.Draw(image)
        draw.line([(round(6*unit),round(6*unit)),(round(11*unit),round(11*unit)),
            (round(16*unit),round(6*unit))],fill="#ababab" if disabled else self.colors["muted"],
            width=max(2,round(1.3*unit)),joint="curve")
        return ImageTk.PhotoImage(image.resize((width,height),Image.Resampling.LANCZOS),master=self.root)

    def apply(self):
        c=self.colors
        style=ttk.Style(self.root)
        scale=round(self.display.scale,4)
        if scale not in self.cache:
            prefix=f"Fluent{len(self.cache)}"
            photos=[]
            def surface(fill,stroke,**kwargs):
                image=self._surface(fill,stroke,**kwargs)
                photos.append(image)
                return image
            def element(name,normal,*states,border=6):
                key=prefix+"."+name
                style.element_create(key,"image",normal,*states,border=self.display.px(border),padding=0,sticky="nsew")
                return key
            button=element("Button",surface(c["surface"],"#d1d1d1"),
                ("disabled",surface(c["disabled"],c["border"])),
                ("pressed",surface("#ededed","#d4d4d4")),
                ("focus",surface(c["surface"],c["ink"])),
                ("active",surface("#f6f6f6","#c7c7c7")))
            primary=element("Primary",surface(c["accent"],c["accent"]),
                ("disabled",surface("#e4e4e4","#e4e4e4")),
                ("pressed",surface(c["pressed"],c["pressed"])),
                ("focus",surface(c["accent"],c["ink"])),
                ("active",surface(c["hover"],c["hover"])))
            field=element("Field",surface(c["surface"],"#cfcfcf"),
                ("disabled",surface(c["disabled"],c["border"])),
                ("focus",surface(c["surface"],"#b9b9b9",underline=c["accent"])),
                ("active",surface(c["surface"],"#ababab")))
            card=element("Card",surface(c["surface"],"#e5e5e5",radius=8,shadow=True),border=10)
            badge=element("Badge",surface(c["tint"],c["tint"],radius=10),border=12)
            tab=element("Tab",surface(c["soft"],c["soft"],radius=4),
                ("selected",surface(c["tint"],c["tint"],radius=4,underline=c["accent"])),
                ("active",surface("#eeeeee","#eeeeee",radius=4)))
            def check(**kwargs):
                image=self._check(**kwargs)
                photos.append(image)
                return image
            indicator=element("Check",check(),("selected","disabled",check(selected=True,disabled=True)),
                ("disabled",check(disabled=True)),("selected","focus",check(selected=True,focus=True)),
                ("selected",check(selected=True)),("focus",check(focus=True)),border=0)
            arrow_photos=[self._chevron(),self._chevron(disabled=True)]
            photos.extend(arrow_photos)
            arrow=element("Combobox.downarrow",arrow_photos[0],("disabled",arrow_photos[1]),border=0)
            self.cache[scale]=dict(button=button,primary=primary,field=field,card=card,badge=badge,tab=tab,
                indicator=indicator,arrow=arrow,photos=photos)
        e=self.cache[scale]
        for name,border in (("TButton",e["button"]),("Primary.TButton",e["primary"])):
            style.layout(name,[(border,{"sticky":"nsew","children":[("Button.padding",{
                "sticky":"nsew","children":[("Button.label",{"sticky":"nsew"})]})]})])
        style.layout("TEntry",[(e["field"],{"sticky":"nsew","children":[("Entry.padding",{
            "sticky":"nsew","children":[("Entry.textarea",{"sticky":"nsew"})]})]})])
        style.layout("TCombobox",[(e["field"],{"sticky":"nsew","children":[("Combobox.padding",{
            "sticky":"nsew","children":[(e["arrow"],{"side":"right","sticky":""}),
                ("Combobox.textarea",{"sticky":"nsew"})]})]})])
        style.layout("Card.TFrame",[(e["card"],{"sticky":"nsew"})])
        style.layout("Input.TFrame",[(e["field"],{"sticky":"nsew"})])
        style.layout("Badge.TLabel",[(e["badge"],{"sticky":"nsew","children":[("Label.padding",{
            "sticky":"nsew","children":[("Label.label",{"sticky":"nsew"})]})]})])
        style.layout("TNotebook.Tab",[(e["tab"],{"sticky":"nsew","children":[("Notebook.padding",{
            "sticky":"nsew","children":[("Notebook.label",{"sticky":"nsew"})]})]})])
        style.layout("TCheckbutton",[("Checkbutton.padding",{"sticky":"nsew","children":[
            (e["indicator"],{"side":"left","sticky":""}),
            ("Checkbutton.focus",{"side":"left","sticky":"w","children":[("Checkbutton.label",{"sticky":"nsew"})]})]})])
        for orient,sticky in (("Vertical","ns"),("Horizontal","ew")):
            style.layout(orient+".TScrollbar",[(orient+".Scrollbar.trough",{"sticky":sticky,"children":[
                (orient+".Scrollbar.thumb",{"sticky":"nsew"})]})])


def apply_window_style(window):
    """Use supported Win11 window corners; retain native chrome elsewhere."""
    if sys.platform!="win32" or sys.getwindowsversion().build<22000:
        return
    from ctypes import wintypes
    try:
        user=ctypes.WinDLL("user32",use_last_error=True)
        user.GetParent.argtypes=[wintypes.HWND]
        user.GetParent.restype=wintypes.HWND
        hwnd=user.GetParent(window.winfo_id()) or window.winfo_id()
        dwm=ctypes.WinDLL("dwmapi")
        dwm.DwmSetWindowAttribute.argtypes=[wintypes.HWND,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD]
        dwm.DwmSetWindowAttribute.restype=ctypes.c_long
        rounded=ctypes.c_int(2)
        dwm.DwmSetWindowAttribute(hwnd,33,ctypes.byref(rounded),ctypes.sizeof(rounded))
        caption=ctypes.c_uint(0x00f3f3f3)
        dwm.DwmSetWindowAttribute(hwnd,35,ctypes.byref(caption),ctypes.sizeof(caption))
    except (OSError,AttributeError,TclError):
        pass
