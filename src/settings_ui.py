"""Editable API controls and sanitized request previews."""
from __future__ import annotations

import json
import tkinter as tk
from tkinter import ttk,messagebox
from deepseek_api import Client,TEXT_MODELS,validate_options,AUTO_THINKING_OUTPUT_TOKENS,MAX_OUTPUT_TOKENS
from secrets_local import save_secret
from vision import DEFAULT_VISION_PROMPT,MEDIA_JSON_PROMPT,vlm_concurrency
from emoji_cdn import download_concurrency as validate_download_concurrency
from full_context import context_limit,input_limit,MAX_REQUEST_BYTES
from display import display_for, ScrollableFrame
from ui_theme import APP_NAME,ANALYSIS_MODE_LABELS
from fluent_widgets import apply_window_style


class SettingsDialog(tk.Toplevel):
    def __init__(self,parent,data,config,on_save):
        super().__init__(parent)
        self.withdraw()
        self.data,self.on_save=data,on_save
        self.display=display_for(parent)
        self.title("API 设置 · "+APP_NAME)
        self.display.fit_window(self,(760,720),(640,480),owner=parent)
        self.transient(parent)
        shell=ttk.Frame(self,padding=18)
        shell.pack(fill="both",expand=True)
        shell.columnconfigure(0,weight=1)
        shell.rowconfigure(2,weight=1)
        ttk.Label(shell,text="DeepSeek API 密钥（留空保留已保存的密钥）").grid(row=0,column=0,sticky="w")
        self.key=tk.StringVar()
        ttk.Entry(shell,textvariable=self.key,show="•").grid(row=1,column=0,sticky="ew",pady=(6,12))
        tabs=ttk.Notebook(shell)
        tabs.grid(row=2,column=0,sticky="nsew")
        general_panel=ScrollableFrame(tabs,width=640,padding=14)
        images_panel=ScrollableFrame(tabs,width=640,padding=14)
        general=general_panel.content
        images=images_panel.content
        preview=ttk.Frame(tabs,padding=14)
        local=ttk.Frame(tabs,padding=14)
        for frame,label in ((general_panel,"模型与推理"),(images_panel,"图片与表情"),(preview,"messages 预览"),(local,"微信目录")):
            tabs.add(frame,text=label)
        self.model=tk.StringVar(value=config.get("model","deepseek-flash"))
        self.thinking=tk.StringVar(value=config.get("thinking","disabled") or "默认")
        self.effort=tk.StringVar(value=config.get("reasoning_effort","") or "默认")
        self.max_tokens=tk.StringVar(value=str(config.get("max_tokens") or 0))
        self.context_mode=tk.StringVar(value=ANALYSIS_MODE_LABELS.get(config.get("context_mode"),ANALYSIS_MODE_LABELS["full"]))
        ttk.Label(general,text="上下文模式").pack(anchor="w",pady=(8,4))
        ttk.Combobox(general,textvariable=self.context_mode,values=tuple(ANALYSIS_MODE_LABELS.values()),state="readonly").pack(fill="x")
        ttk.Label(general,text="RAG：完整发送所选范围内全部关键词匹配原文，补入前后对话并合并重叠窗口，素材解读完整保留；只压缩历史问答，没有独立的 RAG token 或匹配条数限制。向量检索尚未启用。",wraplength=640).pack(anchor="w",pady=6)
        for label,variable,values in (("model · 文字检索模型",self.model,TEXT_MODELS),
                ("thinking.type · 思考模式",self.thinking,("默认","enabled","disabled")),
                ("reasoning_effort · 推理努力程度",self.effort,("默认","none","low","high","max"))):
            ttk.Label(general,text=label).pack(anchor="w",pady=(8,4))
            ttk.Combobox(general,textvariable=variable,values=values,state="readonly").pack(fill="x")
        ttk.Label(general,text="max_tokens · 总输出预算，思考＋回答（0 为自动）").pack(anchor="w",pady=(12,4))
        ttk.Entry(general,textvariable=self.max_tokens).pack(fill="x")
        ttk.Label(general,text=f"思考与正式回答共用输出预算。思考模式自动分析预算为{AUTO_THINKING_OUTPUT_TOKENS:,} token（128K）；手动可设置512–{MAX_OUTPUT_TOKENS:,}（最高384K）。预留更多输出会减少可用输入空间，可能增加耗时与用量；上限并非承诺的回答长度。",wraplength=640).pack(anchor="w",pady=6)
        ttk.Label(general,text="单次上下文上限（token，包含输入及预留输出）").pack(anchor="w",pady=(12,4))
        self.context_limit=tk.StringVar(value=str(config.get("context_limit",1_000_000)))
        ttk.Entry(general,textvariable=self.context_limit).pack(fill="x")
        ttk.Label(general,text="单次输入上限（token，默认 800000）").pack(anchor="w",pady=(12,4))
        self.max_input_tokens=tk.StringVar(value=str(config.get("max_input_tokens",800_000)))
        ttk.Entry(general,textvariable=self.max_input_tokens).pack(fill="x")
        ttk.Label(general,text=f"两种模式均检查单次输入上限、上下文窗口、至少25%窗口余量、回答空间及{MAX_REQUEST_BYTES/1048576:g}MiB本地请求体保护；字节上限不等于token上限。RAG保留全部匹配原文，超限先减少问答历史，再自动缩小前后窗口并显示减少量；匹配原文本身仍超限则不发送。全文模式超限时保留较新记录并提示实际范围。本地原文及会话继续保留。",wraplength=640).pack(anchor="w",pady=8)
        ttk.Label(general,text="关闭思考时，推理努力请选择默认或 none；开启时可选 low / high / max。",wraplength=640).pack(anchor="w",pady=8)
        ttk.Label(general,text="附加系统提示词（加入回答请求的 messages.system）").pack(anchor="w",pady=(8,4))
        self.system_prompt=tk.Text(general,height=5,wrap="word",font=("Microsoft YaHei UI",10))
        self.system_prompt.pack(fill="both",expand=True)
        self.system_prompt.insert("1.0",config.get("system_prompt",""))

        self.vision_enabled=tk.BooleanVar(value=config.get("vision_enabled",True))
        ttk.Checkbutton(images,text="同步时启用图片和静态表情解读",variable=self.vision_enabled).pack(anchor="w")
        self.emoji_enabled=tk.BooleanVar(value=config.get("emoji_enabled",True))
        ttk.Checkbutton(images,text="识别静态表情（没有大小下限；动态表情不识别）",variable=self.emoji_enabled).pack(anchor="w",pady=4)
        self.emoji_cdn_enabled=tk.BooleanVar(value=config.get("emoji_cdn_enabled",True))
        ttk.Checkbutton(images,text="本地表情缺失或无法解码时，从消息自带的微信 CDN 下载完整素材",variable=self.emoji_cdn_enabled).pack(anchor="w",pady=4)
        download_row=ttk.Frame(images)
        download_row.pack(fill="x",pady=4)
        ttk.Label(download_row,text="表情下载并发数（1–200）").pack(side="left")
        self.download_concurrency=tk.StringVar(value=str(config.get("download_concurrency",8)))
        ttk.Spinbox(download_row,from_=1,to=200,textvariable=self.download_concurrency,width=7).pack(side="left",padx=8)
        ttk.Label(images,text="下载在本地准备阶段进行；只下载当前范围的表情，不取缩略图。调用 VLM 仍须另外确认。",wraplength=640).pack(anchor="w",pady=4)
        concurrency_row=ttk.Frame(images)
        concurrency_row.pack(fill="x",pady=6)
        ttk.Label(concurrency_row,text="VLM 并发数（1–200）").pack(side="left")
        self.vlm_concurrency=tk.StringVar(value=str(config.get("vlm_concurrency",200)))
        ttk.Spinbox(concurrency_row,from_=1,to=200,textvariable=self.vlm_concurrency,width=7).pack(side="left",padx=8)
        ttk.Label(images,text="每次调用 VLM 前均须确认；先在本地筛选并去重，再显示需发送的素材数量。",wraplength=640).pack(anchor="w",pady=4)
        ttk.Label(images,text="图片 VLM 模型：deepseek-flash（官方支持视觉输入）").pack(anchor="w",pady=(14,6))
        ttk.Label(images,text="普通图片：50 KB < 大小 < 3 MB；静态表情：0 < 大小 < 3 MB。\n按实际帧数区分动静态，单帧 GIF 也可识别。",wraplength=640).pack(anchor="w",pady=4)
        ttk.Label(images,text="原始素材和 XML 留在本地；文字存档采用消息引用和素材词典。\n同一素材只复用一份成功解读；修改模型或提示词不会重跑历史。\n失败可用本地副本重试，重新识别需点击主界面对应按钮。",wraplength=640).pack(anchor="w",pady=8)
        ttk.Label(images,text="图片提示词（messages.system）").pack(anchor="w",pady=(10,4))
        self.vision_prompt=tk.Text(images,height=10,wrap="word",font=("Microsoft YaHei UI",10))
        self.vision_prompt.pack(fill="both",expand=True)
        self.vision_prompt.insert("1.0",config.get("vision_prompt",DEFAULT_VISION_PROMPT))
        ttk.Label(images,text="确认调用后，符合条件的图片会发送给 DeepSeek。未填写密钥或取消确认时保留待处理状态。",wraplength=640).pack(anchor="w",pady=10)

        ttk.Label(preview,text="messages 由当前问题、检索原文或图片自动组装。下面仅展示请求结构。",wraplength=640).pack(anchor="w")
        self.preview_kind=tk.StringVar(value="文字请求")
        ttk.Combobox(preview,textvariable=self.preview_kind,values=("文字请求","图片请求","静态表情请求"),state="readonly").pack(fill="x",pady=8)
        self.preview=tk.Text(preview,wrap="word",height=18,font=("Consolas",10))
        self.preview.pack(fill="both",expand=True)
        ttk.Button(preview,text="刷新预览",command=self.refresh_preview).pack(anchor="e",pady=(8,0))
        ttk.Label(local,text="微信数据根目录（自动定位失败时填写）").pack(anchor="w")
        self.db_dir=tk.StringVar(value=config.get("db_dir",""))
        ttk.Entry(local,textvariable=self.db_dir).pack(fill="x",pady=8)
        ttk.Label(local,text="通常是包含账号子目录的 xwechat_files 目录。",wraplength=640).pack(anchor="w")
        note=ttk.Label(shell,text="API 密钥保存在本机加密设置中；也支持 DEEPSEEK_API_KEY 环境变量。",wraplength=700)
        note.grid(row=3,column=0,sticky="ew",pady=(12,8))
        buttons=ttk.Frame(shell)
        buttons.grid(row=4,column=0,sticky="ew")
        ttk.Button(buttons,text="保存设置",command=self.save,style="Primary.TButton").pack(side="right")
        ttk.Button(buttons,text="取消",command=self.destroy).pack(side="right",padx=8)
        for var in (self.model,self.thinking,self.effort,self.max_tokens,self.context_limit,self.max_input_tokens,self.preview_kind,self.context_mode):
            var.trace_add("write",lambda *_:self.refresh_preview())
        self.refresh_preview()
        self.display.adapt(shell)
        general_panel.bind_wheel()
        images_panel.bind_wheel()
        shell.bind("<Configure>",lambda event:note.configure(wraplength=max(100,event.width-self.display.px(36))))
        self.bind("<Map>",lambda event:apply_window_style(self) if event.widget is self else None,add="+")
        self.deiconify()
        self.grab_set()

    def options(self):
        thinking="" if self.thinking.get()=="默认" else self.thinking.get()
        effort="" if self.effort.get()=="默认" else self.effort.get()
        try:
            tokens=int(self.max_tokens.get()) or None
        except ValueError:
            raise ValueError("max_tokens 请输入整数，0 表示自动") from None
        validate_options(self.model.get(),thinking,effort,tokens)
        concurrency=vlm_concurrency(self.vlm_concurrency.get())
        download_concurrency=validate_download_concurrency(self.download_concurrency.get())
        return {"model":self.model.get(),"thinking":thinking,"reasoning_effort":effort,
            "context_mode":"rag" if getattr(self,"context_mode",None) and self.context_mode.get()==ANALYSIS_MODE_LABELS["rag"] else "full",
            "max_tokens":tokens,"context_limit":context_limit(self.context_limit.get()),
            "max_input_tokens":input_limit(self.max_input_tokens.get()),"system_prompt":self.system_prompt.get("1.0","end").strip(),
            "vision_enabled":self.vision_enabled.get(),
            "emoji_enabled":self.emoji_enabled.get(),
            "vlm_concurrency":concurrency,
            "emoji_cdn_enabled":self.emoji_cdn_enabled.get(),"download_concurrency":download_concurrency,
            "vision_prompt":self.vision_prompt.get("1.0","end").strip() or DEFAULT_VISION_PROMPT,
            "db_dir":self.db_dir.get().strip()}

    def refresh_preview(self):
        try:
            options=self.options()
            client=Client("preview-only",options["model"],thinking=options["thinking"],
                reasoning_effort=options["reasoning_effort"],max_tokens=options["max_tokens"])
            vision=self.preview_kind.get() in ("图片请求","静态表情请求")
            if vision:
                messages=[{"role":"system","content":options["vision_prompt"]+"\n"+MEDIA_JSON_PROMPT},
                    {"role":"user","content":[{"type":"text","text":"请解读这张"+("静态表情" if self.preview_kind.get()=="静态表情请求" else "图片")},
                        {"type":"image_url","image_url":{"url":"data:image/jpeg;base64,<符合条件的图片>","detail":"high"}}]}]
            else:
                description=("<当前问题、历史摘要、近期问答、全部关键词匹配原文和完整去重素材词典>"
                    if options["context_mode"]=="rag" else "<当前问题、所选范围全部已存消息、去重素材词典及实际覆盖信息>")
                messages=[{"role":"system","content":options["system_prompt"] or "<根据原文回答的系统提示词>"},
                    {"role":"user","content":description}]
            text=json.dumps(client.build_payload(messages,vision=vision,json_mode=vision,stream=not vision),ensure_ascii=False,indent=2)
        except ValueError as exc:
            text="设置提示："+str(exc)
        self.preview.configure(state="normal")
        self.preview.delete("1.0","end")
        self.preview.insert("1.0",text)
        self.preview.configure(state="disabled")

    def save(self):
        try:
            options=self.options()
            if self.key.get().strip():
                save_secret(self.data/"deepseek-key.bin",self.key.get().strip().encode())
            self.on_save(options)
            self.destroy()
        except Exception as exc:
            messagebox.showerror("设置未保存",str(exc),parent=self)
