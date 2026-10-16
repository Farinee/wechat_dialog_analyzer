"""Single-chat date-scoped archives and DeepSeek retrieval."""
from __future__ import annotations
import argparse
import json
import os
import queue
import sys
import threading
import tkinter as tk
from datetime import datetime, timedelta
from pathlib import Path
from tkinter import ttk, messagebox, filedialog
from deepseek_api import Client
from secrets_local import load_secret
from storage import Store, TZ, TIMEZONE_LABEL, epoch, display_time, display_sync_time
from vision import DEFAULT_VISION_PROMPT
from chat_list import visible_chats, size_label
from archive import sync_range, covered_ranges, missing_ranges, export_dirty_days, archive_dir
from media_store import citation_text,dictionary_lines
from display import enable_high_dpi, display_for, ScrollableFrame
from conversation import Conversations,model_history,export_markdown,unfinished_turn
from markdown_view import MarkdownText
from ui_theme import APP_NAME,DECODE_LABEL,ANALYSIS_MODE_LABELS,COLORS,install_theme
from app_branding import install_branding


def application_dir():
    return Path(__file__).resolve().parents[1]


def read_config(data):
    try:
        return json.loads((data/"settings.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def write_config(data, settings):
    tmp=data/"settings.tmp"
    tmp.write_text(json.dumps(settings,ensure_ascii=False,indent=2),encoding="utf-8")
    tmp.replace(data/"settings.json")


class App:
    def __init__(self, root, data, auto=True, source_factory=None):
        self.root,self.data=root,Path(data)
        self.data.mkdir(parents=True,exist_ok=True)
        self.config=read_config(self.data)
        self.store=Store(self.data/"messages.sqlite3")
        self.events,self.stop=queue.Queue(),threading.Event()
        self.busy,self.loaded=False,False
        self.account,self.groups,self.last_answer=None,[],None
        self.conversations=Conversations(self.data/"conversations.sqlite3")
        self.conversation_scope,self.conversation_id=None,None
        self.conversation_turns=[]
        self.pending_question=""
        self.partial_turn=None
        self.draft_timer=None
        self.session_ids=[]
        self.sync_errors=[]
        self.local_timer=None
        self.refresh_local_pending=False
        saved=self.config.get("selected",[])
        self.selected_names={saved[0]} if saved else set()
        if source_factory is None:
            from wechat_source import Source
            source_factory=Source
        self.source_factory=source_factory
        self.display=display_for(root)
        root.title(APP_NAME)
        self.display.fit_window(root,(1380,940))
        install_theme(root,self.display)
        self.branding=install_branding(root,self.display)
        shell=ttk.Frame(root,padding=20,style="App.TFrame")
        shell.pack(fill="both",expand=True)
        shell.columnconfigure(0,weight=1)
        shell.rowconfigure(2,weight=1)
        header=ttk.Frame(shell,style="App.TFrame")
        header.grid(row=0,column=0,sticky="ew")
        if self.branding:
            self.branding.attach_header(header)
        ttk.Label(header,text=APP_NAME,font=("Microsoft YaHei UI",22,"bold"),style="Brand.TLabel").pack(side="left")
        ttk.Label(header,text="难不成Ta真的喜欢我？",style="Badge.TLabel").pack(side="right")
        #subtitle=ttk.Label(shell,text="选择聊天与日期，整理本地记录，让每一次提问都有据可查。 · "+TIMEZONE_LABEL,style="App.TLabel")
        #subtitle.grid(row=1,column=0,sticky="ew",pady=(4,12))
        self.panes=ttk.Panedwindow(shell,orient="horizontal")
        self.panes.grid(row=2,column=0,sticky="nsew")
        self.sidebar=ScrollableFrame(self.panes,width=450,padding=16,style="Card.TFrame")
        left,right=self.sidebar.content,ttk.Frame(self.panes,padding=16,style="Card.TFrame")
        self.panes.add(self.sidebar,weight=2)
        self.panes.add(right,weight=3)
        ttk.Label(left,text="聊天列表",font=("Microsoft YaHei UI",13,"bold")).pack(anchor="w")
        ttk.Label(left,text="选择一个群聊或联系人，开始整理记录",style="Muted.TLabel").pack(anchor="w",pady=(3,0))
        self.chat_kind=tk.StringVar(value="群聊")
        kind_box=ttk.Combobox(left,textvariable=self.chat_kind,values=("群聊","个人"),state="readonly")
        kind_box.pack(fill="x",pady=(8,0))
        kind_box.bind("<<ComboboxSelected>>",lambda _:self.render_groups())
        self.filter=tk.StringVar()
        self.filter.trace_add("write",lambda *_:self.render_groups())
        ttk.Label(left,text="按聊天名称筛选",style="Muted.TLabel").pack(anchor="w",pady=(10,0))
        ttk.Entry(left,textvariable=self.filter).pack(fill="x",pady=(4,6))
        self.hide_small=tk.BooleanVar(value=self.config.get("hide_small_chats",True))
        ttk.Checkbutton(left,text="仅显示 ≥1000 条且 ≥50 KB 的聊天",variable=self.hide_small,command=self.change_list_view).pack(anchor="w")
        self.chat_sort=tk.StringVar(value="消息条数（多→少）" if self.config.get("chat_sort_by")=="message_count" else "内容大小（大→小）")
        sort_box=ttk.Combobox(left,textvariable=self.chat_sort,values=("内容大小（大→小）","消息条数（多→少）"),state="readonly")
        sort_box.pack(fill="x",pady=4)
        sort_box.bind("<<ComboboxSelected>>",lambda _:self.change_list_view())
        ttk.Label(left,text="本机历史正文统计，大小不含图片和语音",font=("Microsoft YaHei UI",9),style="Muted.TLabel").pack(anchor="w",pady=(0,6))
        tree_frame=ttk.Frame(left)
        tree_frame.pack(fill="both",expand=True)
        self.tree=ttk.Treeview(tree_frame,columns=("chosen","name","count","size"),show="headings",selectmode="browse",height=9)
        self.tree.tag_configure("stripe",background=COLORS["soft"])
        for key,label,width in (("chosen","选",30),("name","聊天名称",210),("count","消息条数",78),("size","内容大小",90)):
            self.tree.heading(key,text=label)
            self.tree.column(key,width=width,stretch=key=="name",anchor="w" if key=="name" else "center")
        scroll=ttk.Scrollbar(tree_frame,orient="vertical",command=self.tree.yview)
        self.connect_scrollbar(self.tree,scroll)
        self.tree.pack(side="left",fill="both",expand=True)
        scroll.pack(side="right",fill="y")
        self.tree.bind("<ButtonRelease-1>",self.toggle_group)
        self.tree.bind("<space>",self.toggle_group)
        self.tree.bind("<Return>",self.toggle_group)
        self.selection_label=tk.StringVar(value="尚未选择聊天")
        ttk.Label(left,textvariable=self.selection_label,wraplength=420,style="Muted.TLabel").pack(anchor="w",pady=6)
        ttk.Separator(left,orient="horizontal").pack(fill="x",pady=(6,10))
        ttk.Label(left,text="保存范围",font=("Microsoft YaHei UI",11,"bold")).pack(anchor="w",pady=(0,4))
        dates=ttk.Frame(left)
        dates.pack(fill="x",pady=4)
        self.since=tk.StringVar(value=self.config.get("since",(datetime.now(TZ)-timedelta(days=30)).strftime("%Y-%m-%d")))
        self.until=tk.StringVar(value=self.config.get("until",datetime.now(TZ).strftime("%Y-%m-%d")))
        self.date_entries=[]
        for column,label,var in ((0,"起始日期",self.since),(1,"结束日期（含当天）",self.until)):
            cell=ttk.Frame(dates)
            cell.grid(row=0,column=column,sticky="ew",padx=(0,8) if column==0 else 0)
            dates.columnconfigure(column,weight=1)
            ttk.Label(cell,text=label).pack(anchor="w")
            entry=ttk.Entry(cell,textvariable=var,width=15)
            entry.pack(fill="x",pady=(3,0))
            self.date_entries.append(entry)
        ttk.Label(left,text="格式 YYYY-MM-DD；切换日期只读取本地记录",font=("Microsoft YaHei UI",9),style="Muted.TLabel").pack(anchor="w",pady=(4,2))
        self.force_check=tk.BooleanVar(value=False)
        ttk.Checkbutton(left,text="重新检查已存日期（补导后来下载的历史）",variable=self.force_check).pack(anchor="w",pady=(5,2))
        self.sync_button=ttk.Button(self.sidebar,text=DECODE_LABEL,command=self.save_and_sync,style="Primary.TButton")
        self.sync_button.grid(row=1,column=0,columnspan=2,sticky="ew",pady=(12,0))
        self.reanalyze_button=ttk.Button(left,text="重新识别本范围已缓存素材",command=self.reanalyze_media)
        self.reanalyze_button.pack(fill="x",pady=3)
        self.local_button=ttk.Button(left,text="读取本地原文",command=lambda:self.show_local(persist=True))
        self.local_button.pack(fill="x",pady=3)
        self.refresh_button=ttk.Button(left,text="从微信刷新聊天列表",command=lambda:self.load_groups(False))
        self.refresh_button.pack(fill="x",pady=3)
        actions=ttk.Frame(left)
        actions.pack(fill="x",pady=(3,0))
        ttk.Button(actions,text="打开文本存档目录",command=self.open_archive).pack(side="left",fill="x",expand=True,padx=(0,5))
        ttk.Button(actions,text="API 设置",command=self.settings_dialog).pack(side="left",fill="x",expand=True)
        ttk.Label(right,text="聊天分析",font=("Microsoft YaHei UI",13,"bold")).pack(anchor="w")
        #ttk.Label(right,text="围绕所选记录提问、追问，并核对原文",style="Muted.TLabel").pack(anchor="w",pady=(3,0))
        conversation_bar=ttk.Frame(right)
        conversation_bar.pack(fill="x",pady=(6,0))
        self.session_box=ttk.Combobox(conversation_bar,state="readonly")
        self.session_box.pack(side="left",fill="x",expand=True)
        self.session_box.bind("<<ComboboxSelected>>",lambda _:self.select_conversation())
        self.new_conversation_button=ttk.Button(conversation_bar,text="新对话",command=self.new_conversation)
        self.new_conversation_button.pack(side="left",padx=(6,0))
        self.question_frame=ttk.Frame(right,style="Input.TFrame",padding=4)
        self.question_frame.pack(fill="x",pady=(8,6))
        self.question=tk.Text(self.question_frame,height=3,wrap="word",font=("Microsoft YaHei UI",11),
            relief="flat",highlightthickness=0,padx=12,pady=8)
        self.question.pack(fill="both",expand=True)
        self.question.bind("<FocusIn>",lambda _:self.question_frame.state(("focus",)),add="+")
        self.question.bind("<FocusOut>",lambda _:self.question_frame.state(("!focus",)),add="+")
        self.question.bind("<Control-Return>",lambda _:self.ask())
        self.question.bind("<<Modified>>",self.question_modified)
        toolbar=ttk.Frame(right)
        toolbar.pack(fill="x",pady=(0,8))
        self.ask_button=ttk.Button(toolbar,text="读取所选范围并分析",command=self.ask,style="Primary.TButton")
        self.ask_button.pack(side="left")
        ttk.Button(toolbar,text="导出对话",command=self.export_answer).pack(side="left",padx=8)
        self.context_mode=tk.StringVar(value=ANALYSIS_MODE_LABELS.get(self.config.get("context_mode"),ANALYSIS_MODE_LABELS["full"]))
        self.context_mode_box=ttk.Combobox(toolbar,textvariable=self.context_mode,values=tuple(ANALYSIS_MODE_LABELS.values()),state="readonly",width=10)
        self.context_mode_box.pack(side="left",padx=4)
        self.context_mode_box.bind("<<ComboboxSelected>>",lambda _:self.change_context_mode())
        panels=ttk.Panedwindow(right,orient="vertical")
        panels.pack(fill="both",expand=True)
        self.tabs=ttk.Notebook(panels)
        panels.add(self.tabs,weight=3)
        self.answer_text=self.text_panel(self.tabs,"对话",11,markdown=True)
        source_frame=ttk.Frame(self.tabs)
        self.tabs.add(source_frame,text="原文")
        self.preview_scope=None
        self.preview_page_index=0
        self.preview_counts={}
        self.preview_day=tk.StringVar()
        day_bar=ttk.Frame(source_frame)
        day_bar.pack(fill="x",pady=(4,2))
        ttk.Label(day_bar,text="预览日期").pack(side="left")
        self.preview_day_box=ttk.Combobox(day_bar,textvariable=self.preview_day,state="readonly",width=12)
        self.preview_day_box.pack(side="left",padx=4)
        self.preview_day_box.bind("<<ComboboxSelected>>",lambda _:self.show_local(keep_answer=True))
        self.previous_day_button=ttk.Button(day_bar,text="上一天",command=lambda:self.move_preview_day(-1))
        self.previous_day_button.pack(side="left",padx=2)
        self.next_day_button=ttk.Button(day_bar,text="下一天",command=lambda:self.move_preview_day(1))
        self.next_day_button.pack(side="left",padx=2)
        page_bar=ttk.Frame(source_frame)
        page_bar.pack(fill="x",pady=(0,2))
        self.previous_page_button=ttk.Button(page_bar,text="上一页",command=lambda:self.move_preview_page(-1))
        self.previous_page_button.pack(side="left")
        self.next_page_button=ttk.Button(page_bar,text="下一页",command=lambda:self.move_preview_page(1))
        self.next_page_button.pack(side="left",padx=4)
        self.preview_label=tk.StringVar(value="本地存档 · 每页最多 200 条")
        ttk.Label(page_bar,textvariable=self.preview_label).pack(side="left")
        self.source_tabs=ttk.Notebook(source_frame)
        self.source_tabs.pack(fill="both",expand=True)
        self.raw_sources_text=self.text_panel(self.source_tabs,"原始资料层",10)
        self.sources_text=self.text_panel(self.source_tabs,"可检索文字层",10)
        self.source_tabs.select(1)
        coverage_frame=ttk.Frame(panels,style="Card.TFrame",padding=12)
        panels.add(coverage_frame,weight=1)
        ttk.Label(coverage_frame,text="同步范围",font=("Microsoft YaHei UI",10,"bold")).pack(anchor="w",pady=(0,6))
        self.coverage_widget=self.text_panel(coverage_frame,None,9,height=8)
        self.answer_text.begin()
        self.answer_text.markdown(f"## 从聊天记录中找到答案\n\n选择一个聊天和日期范围，点击「{DECODE_LABEL}」，即可开始分析。\n\n- **整理记录**：文字和已有图片解读按天保存到本地。\n- **连续提问**：梳理讨论、查找细节，继续追问。\n- **核对依据**：随时查看本轮引用，回到原始消息。\n\n已保存的记录可直接读取，提问时无需重新解码。")
        self.answer_text.finish()
        self.set_text(self.sources_text,"选择聊天及日期后，这里展示本地原文；检索完成后展示引用的原文。")
        self.set_text(self.raw_sources_text,"选择聊天及日期后，这里展示本地留存的原始消息正文、媒体 XML 和素材文件位置。")
        self.status=tk.StringVar(value="准备就绪")
        footer=ttk.Label(shell,textvariable=self.status,wraplength=1250,style="App.TLabel")
        footer.grid(row=3,column=0,sticky="ew",pady=(10,0))
        self.display.adapt(shell)
        self.sidebar.bind_wheel()
        shell.bind("<Configure>",lambda event:footer.configure(wraplength=max(100,event.width-self.display.px(36))))
        self.since.trace_add("write",self.schedule_local)
        self.until.trace_add("write",self.schedule_local)
        self.update_preview_controls()
        self.update_coverage()
        root.protocol("WM_DELETE_WINDOW",self.close)
        root.after(100,self.poll)
        if auto:
            root.after(250,lambda:self.load_groups(True))

    def text_panel(self,parent,title,font_size,height=12,markdown=False):
        frame=ttk.Frame(parent)
        if title:
            parent.add(frame,text=title)
        else:
            frame.pack(fill="both",expand=True)
        widget=MarkdownText if markdown else tk.Text
        text=widget(frame,wrap="word",font=("Microsoft YaHei UI",font_size),relief="flat",highlightthickness=0,
            padx=12,pady=10,height=height)
        scroll=ttk.Scrollbar(frame,orient="vertical",command=text.yview)
        self.connect_scrollbar(text,scroll)
        text.pack(side="left",fill="both",expand=True)
        scroll.pack(side="right",fill="y")
        return text

    @staticmethod
    def connect_scrollbar(widget,scroll):
        def update(first,last):
            scroll.set(first,last)
            if float(first)<=0 and float(last)>=1:
                scroll.pack_forget()
            elif not scroll.winfo_manager():
                scroll.pack(side="right",fill="y")
        widget.configure(yscrollcommand=update)

    @staticmethod
    def set_text(widget,value):
        widget.configure(state="normal")
        widget.delete("1.0","end")
        widget.insert("1.0",value)
        widget.configure(state="disabled")

    def set_preview_text(self, widget, value):
        if len(value)>160000:
            value=value[:160000]+"\n\n[本页预览达到长度上限，完整内容仍保存在本地；可翻页或查看按天文本。]"
        self.set_text(widget,value)

    def ensure_conversation(self):
        scope=self.store.scope
        if scope is None:
            return
        if self.conversation_scope!=scope:
            self.save_question_draft()
            self.conversation_scope=scope
            self.conversation_id=self.conversations.current(scope)
            self.pending_question=""
            self.load_conversation()

    def load_conversation(self,restore_draft=True):
        self.conversation_turns=self.conversations.turns(self.conversation_scope,self.conversation_id)
        sessions=self.conversations.sessions(self.conversation_scope)
        self.session_ids=[s["id"] for s in sessions]
        self.session_box.configure(values=[s["created"][:19].replace("T"," ")+" · "+" ".join((s["title"] or "新对话").split())[:24] for s in sessions])
        if self.conversation_id in self.session_ids:
            self.session_box.current(self.session_ids.index(self.conversation_id))
        self.last_answer=self.conversation_turns[-1] if self.conversation_turns else None
        unfinished=self.conversations.unfinished(self.conversation_scope,self.conversation_id)
        self.partial_turn=unfinished_turn(unfinished)
        if self.partial_turn:
            self.partial_turn["answer"]="> 未完成回答：以下为已收到并保存的片段，不能视为完整分析。\n\n"+self.partial_turn["answer"]
        if restore_draft:
            draft=self.conversations.draft(self.conversation_scope,self.conversation_id)
            self.question.delete("1.0","end")
            self.question.insert("1.0",draft)
            self.question.edit_modified(False)
            if unfinished:
                self.status.set("上次请求未完成，问题及已收到片段已恢复；服务端可能已计费，未自动重发")
        self.render_conversation()

    def question_modified(self,_event=None):
        if not self.question.edit_modified():
            return
        self.question.edit_modified(False)
        if self.draft_timer is not None:
            self.root.after_cancel(self.draft_timer)
        self.draft_timer=self.root.after(500,self.save_question_draft)

    def save_question_draft(self):
        if self.draft_timer is not None:
            self.root.after_cancel(self.draft_timer)
            self.draft_timer=None
        if self.conversation_scope is not None and self.conversation_id is not None:
            text=self.question.get("1.0","end-1c")
            if self.busy and text==self.pending_question:
                return
            self.conversations.save_draft(self.conversation_scope,self.conversation_id,text)

    def render_conversation(self,*,preserve_view=False):
        turns=self.conversation_turns
        pending=self.pending_question
        if self.partial_turn:
            turns=[*turns,self.partial_turn]
            if pending==self.partial_turn["question"]:
                pending=""
        self.answer_text.render_conversation(turns,self.show_answer_sources,pending,preserve_view=preserve_view)

    def new_conversation(self):
        if self.busy:
            return
        try:
            self.prepare_scope(persist=True)
        except ValueError as exc:
            self.status.set(str(exc))
            return
        self.save_question_draft()
        self.conversation_scope=self.store.scope
        self.conversation_id=self.conversations.create(self.conversation_scope)
        self.pending_question=""
        self.load_conversation()
        self.show_local(keep_answer=True)
        self.status.set("新对话已开始，仍只检索当前聊天和日期范围")

    def select_conversation(self):
        if self.busy or self.session_box.current()<0 or self.store.scope!=self.conversation_scope:
            return
        self.save_question_draft()
        self.conversation_id=self.session_ids[self.session_box.current()]
        self.conversations.select(self.conversation_scope,self.conversation_id)
        self.pending_question=""
        self.load_conversation()
        if self.last_answer:
            self.show_answer_sources(self.last_answer)
        else:
            self.show_local(keep_answer=True)

    def render_groups(self):
        self.tree.delete(*self.tree.get_children())
        counts={}
        for group in self.groups:
            counts[group["name"]]=counts.get(group["name"],0)+1
        sort_by="message_count" if self.chat_sort.get()=="消息条数（多→少）" else "content_bytes"
        rows=visible_chats(self.groups,self.chat_kind.get(),self.filter.get().strip().casefold(),self.hide_small.get(),sort_by)
        duplicates={}
        for row_index,(index,group) in enumerate(rows):
            label=group["name"]
            if counts[label]>1:
                duplicates[label]=duplicates.get(label,0)+1
                label+=f" · 同名聊天 {duplicates[label]}"
            count=f"{group['message_count']:,}" if "message_count" in group else "—"
            active=group["username"] in self.selected_names
            self.tree.insert("","end",iid=str(index),values=("●" if active else "○",label,count,size_label(group.get("content_bytes"))),
                tags=("stripe",) if row_index%2 else ())
            if active:
                self.tree.selection_set(str(index))
        hidden=len(self.selected_names-{group["username"] for _,group in rows})
        selected=next((g["name"] for g in self.groups if g["username"] in self.selected_names),"尚未选择")
        self.selection_label.set(f"显示 {len(rows)} 个；当前：{selected}"+("（被筛选隐藏，仍保留选择）" if hidden else ""))

    def change_list_view(self):
        self.config["hide_small_chats"]=self.hide_small.get()
        self.config["chat_sort_by"]="message_count" if self.chat_sort.get()=="消息条数（多→少）" else "content_bytes"
        write_config(self.data,self.config)
        self.render_groups()

    def toggle_group(self,event):
        if self.busy:
            return
        item=self.tree.identify_row(event.y) if getattr(event,"num",None)==1 else self.tree.focus()
        if not item:
            return
        username=self.groups[int(item)]["username"]
        if self.selected_names=={username}:
            return
        self.selected_names={username}
        self.sync_errors=[]
        saved=self.config.get("chat_ranges",{}).get(self.account,{}).get(username)
        if saved:
            self.since.set(saved["start"])
            self.until.set(saved["end"])
        self.force_check.set(False)
        self.render_groups()
        self.show_local()
        self.status.set("已选择一个聊天，请确认起止日期，再同步保存；原有本地记录继续保留")

    def schedule_local(self,*_):
        if self.local_timer is not None:
            self.root.after_cancel(self.local_timer)
        self.local_timer=self.root.after(250,self.show_local)

    def prepare_scope(self,persist=False):
        if not self.loaded or len(self.selected_names)!=1:
            raise ValueError("请先从列表选择一个聊天")
        start_text,end_text=self.since.get().strip(),self.until.get().strip()
        try:
            start,end=epoch(start_text),epoch(end_text,end=True)
        except ValueError:
            raise ValueError("请填写 YYYY-MM-DD 格式的起止日期") from None
        if start is None or end is None or start>=end:
            raise ValueError("起始日期不能晚于结束日期，日期不能为空")
        if end_text>datetime.now(TZ).strftime("%Y-%m-%d"):
            raise ValueError("结束日期不能晚于今天（北京时间）")
        group=next((g for g in self.groups if g["username"] in self.selected_names),None)
        if group is None:
            raise ValueError("当前聊天不在此账号的列表中，请重新选择")
        self.store.set_selection(self.account,[group],self.selected_names,start)
        self.store.set_scope(Store.chat_id(self.account,group["username"]),start,end)
        if persist:
            self.config.update(account=self.account,selected=[group["username"]],since=start_text,until=end_text)
            self.config.setdefault("chat_ranges",{}).setdefault(self.account,{})[group["username"]]={"start":start_text,"end":end_text}
            write_config(self.data,self.config)
        return group

    def show_local(self,persist=False,reset_page=True,keep_answer=False):
        if self.local_timer is not None:
            self.root.after_cancel(self.local_timer)
            self.local_timer=None
        if self.busy:
            return
        try:
            group=self.prepare_scope(persist)
        except ValueError as exc:
            self.save_question_draft()
            self.store.scope=None
            self.last_answer=None
            self.conversation_scope=None
            self.conversation_id=None
            self.conversation_turns=[]
            self.pending_question=""
            self.session_box.configure(values=())
            self.session_box.set("")
            self.set_text(self.answer_text,str(exc))
            self.preview_scope=None
            self.preview_counts={}
            self.preview_day.set("")
            self.preview_day_box.configure(values=())
            self.preview_label.set("请选择有效的聊天和日期范围")
            self.update_preview_controls()
            self.set_text(self.sources_text,str(exc))
            self.set_text(self.raw_sources_text,str(exc))
            self.set_text(self.coverage_widget,str(exc)+"\n所有日期均为"+TIMEZONE_LABEL)
            return
        changed_scope=self.preview_scope!=self.store.scope
        self.ensure_conversation()
        self.preview_counts={r["day"]:r["count"] for r in self.store.preview_days()}
        days=list(self.preview_counts)
        if changed_scope or self.preview_day.get() not in days:
            self.preview_day.set(days[-1] if days else "")
            reset_page=True
        self.preview_scope=self.store.scope
        self.preview_day_box.configure(values=days)
        if reset_page:
            self.preview_page_index=0
        count=self.preview_counts.get(self.preview_day.get(),0)
        pages=max(1,(count+199)//200)
        self.preview_page_index=max(0,min(self.preview_page_index,pages-1))
        rows=self.store.preview_page(self.preview_day.get(),self.preview_page_index) if days else []
        self.preview_label.set(f"本地存档 · {count} 条 · 第 {self.preview_page_index+1}/{pages} 页")
        self.update_preview_controls()
        lines=[f"{group['name']} · {self.preview_day.get() or '暂无日期'} · 北京时间",
            f"本日 {count} 条 · 第 {self.preview_page_index+1}/{pages} 页，每页最多 200 条，按时间先后显示。"]
        for row in rows:
            lines.append(f"\n[{display_time(row['timestamp'])}] {row['sender']} · {row['kind']}\n{citation_text(row)}")
        lines.extend(dictionary_lines(rows))
        if not rows:
            lines.append("\n此日期范围尚无本地消息，请先同步。")
        self.set_preview_text(self.sources_text,"\n".join(lines))
        self.show_originals(rows,lines[:2])
        self.update_coverage()

    def update_preview_controls(self):
        days=list(self.preview_counts)
        index=days.index(self.preview_day.get()) if self.preview_day.get() in days else -1
        count=self.preview_counts.get(self.preview_day.get(),0)
        self.preview_day_box.configure(state="disabled" if self.busy or not days else "readonly")
        for button,enabled in ((self.previous_day_button,index>0),(self.next_day_button,0<=index<len(days)-1),
            (self.previous_page_button,self.preview_page_index>0),
            (self.next_page_button,(self.preview_page_index+1)*200<count)):
            button.configure(state="normal" if enabled and not self.busy else "disabled")

    def move_preview_day(self, step):
        if self.busy:
            return
        days=list(self.preview_counts)
        if self.preview_day.get() in days:
            index=days.index(self.preview_day.get())+step
            if 0<=index<len(days):
                self.preview_day.set(days[index])
                self.show_local(keep_answer=True)

    def move_preview_page(self, step):
        if not self.busy:
            self.preview_page_index+=step
            self.show_local(reset_page=False,keep_answer=True)

    def show_originals(self, rows, header):
        lines=[*header,"原始资料仅在本地预览；媒体 XML 不作为检索文字或模型上下文。"]
        refs={row["id"]:row.get("ref","") for row in rows}
        originals=self.store.original_records([row["id"] for row in rows])
        labels={"pending":"待解读","done":"已解读","unavailable":"本机完整素材缺失",
            "skipped_size":"大小不符","skipped_animated":"动态未识别","failed":"处理失败"}
        for row in originals:
            ref=f"[{refs[row['id']]}] " if refs[row["id"]] else ""
            content=row["original_content"]
            if len(content)>8000:
                content=content[:8000]+"\n[此条原始资料较长，预览截取前 8000 字，完整内容仍保存在本地原始资料层。]"
            lines.append(f"\n{ref}[{display_time(row['timestamp'])}] {row['sender']}\n"+content)
            if row.get("media_status"):
                lines.append("素材状态："+labels.get(row["media_status"],row["media_status"]))
            if row.get("asset_id"):
                folder=self.data/"assets"/row["asset_id"]
                for label,path in (("原始素材",row["original_path"]),("识别输入",row["input_path"]),
                    ("源缓存副本",str((folder/"source.bin").relative_to(self.data)))):
                    if path and (self.data/path).is_file():
                        lines.append(label+"："+str((self.data/path).resolve()))
        if not originals:
            lines.append("\n此范围暂无留存的原始资料，请先同步。")
        self.set_preview_text(self.raw_sources_text,"\n".join(lines))

    def run(self,task):
        if self.busy:
            return
        if self.local_timer is not None:
            self.root.after_cancel(self.local_timer)
            self.local_timer=None
        self.busy=True
        self.update_preview_controls()
        for button in (self.ask_button,self.sync_button,self.refresh_button,self.local_button,self.reanalyze_button,self.new_conversation_button):
            button.configure(state="disabled")
        self.session_box.configure(state="disabled")
        self.context_mode_box.configure(state="disabled")
        for entry in self.date_entries:
            entry.configure(state="disabled")
        def worker():
            try:
                task()
            except Exception as exc:
                self.events.put(("error",str(exc)))
            finally:
                self.events.put(("done",None))
        threading.Thread(target=worker,daemon=True).start()

    def progress(self,text):
        self.events.put(("status",text))

    def confirm_vlm(self, plan):
        """Worker waits; only the Tk thread opens the confirmation dialog."""
        request={"plan":plan,"event":threading.Event(),"approved":False}
        self.events.put(("confirm_vlm",request))
        while not request["event"].wait(0.1):
            if self.stop.is_set():
                return False
        return request["approved"] is True and not self.stop.is_set()

    def make_source(self):
        return self.source_factory(self.data,account=self.config.get("account"),db_dir=self.config.get("db_dir"))

    def make_client(self,optional=False):
        key=os.environ.get("DEEPSEEK_API_KEY","") or load_secret(self.data/"deepseek-key.bin").decode()
        if optional and not key:
            return None
        return Client(key,self.config.get("model","deepseek-flash"),thinking=self.config.get("thinking","disabled"),reasoning_effort=self.config.get("reasoning_effort",""),max_tokens=self.config.get("max_tokens") or None,system_prompt=self.config.get("system_prompt",""),vision_prompt=self.config.get("vision_prompt","") or DEFAULT_VISION_PROMPT)

    def update_messages(self,force=False):
        return sync_range(self.store,self.make_source,client_factory=lambda:self.make_client(optional=True),
            vision_enabled=self.config.get("vision_enabled",True),force=force,progress=self.progress,stop=self.stop,
            emoji_enabled=self.config.get("emoji_enabled",True),vlm_concurrency=self.config.get("vlm_concurrency",200),
            confirm_vlm=self.confirm_vlm,emoji_cdn_enabled=self.config.get("emoji_cdn_enabled",True),
            download_concurrency=self.config.get("download_concurrency",8))

    def reanalyze_media(self):
        if self.busy:
            return
        try:
            self.prepare_scope(persist=True)
            if not self.config.get("vision_enabled",True):
                raise ValueError("请先在 API 设置中启用素材解读")
            self.make_client()
        except ValueError as exc:
            self.status.set(str(exc))
            return
        count=self.store.media.request_reanalysis(include_emoji=self.config.get("emoji_enabled",True))
        if not count:
            self.status.set("当前范围没有可重新识别的已缓存静态素材，请先同步消息")
            return
        self.status.set("正在重新识别本范围素材，完成后更新关联文本…")
        self.run(lambda:self.events.put(("sync",self.update_messages())))

    def load_groups(self,prefer_local=False):
        if self.busy:
            return
        self.status.set("正在恢复本地列表…" if prefer_local else "正在从微信刷新列表及统计，请保持微信已登录…")
        def task():
            export_dirty_days(self.store)
            if prefer_local:
                try:
                    catalog=json.loads((self.data/"catalog.json").read_text(encoding="utf-8"))
                    if catalog["account"] and isinstance(catalog["groups"],list) and catalog["groups"] and catalog["account"]==self.config.get("account",catalog["account"]):
                        self.events.put(("groups",(catalog["account"],catalog["groups"])))
                        self.progress("已恢复本地列表和存档；选择日期后可读取本地原文或补同步")
                        return
                except (OSError,ValueError,KeyError,TypeError):
                    pass
                # Older versions have chat metadata in SQLite but no catalog.
                with self.store.connect() as conn:
                    rows=conn.execute("SELECT account,username,name FROM chats WHERE account=? ORDER BY name",
                        (self.config.get("account"),)).fetchall()
                if rows:
                    groups=[{"username":r["username"],"name":r["name"],
                        "kind":"群聊" if r["username"].endswith("@chatroom") else "个人"} for r in rows]
                    self.events.put(("groups",(rows[0]["account"],groups)))
                    self.progress("已恢复本地聊天信息和存档；刷新列表可重新统计条数与大小")
                    return
            source=self.make_source()
            try:
                groups=source.groups()
                if hasattr(source,"contacts"):
                    groups+=source.contacts()
                if hasattr(source,"chat_statistics"):
                    groups=source.chat_statistics(groups,self.progress,self.stop)
                self.store.refresh_names(source.account,groups)
                tmp=self.data/"catalog.tmp"
                tmp.write_text(json.dumps({"account":source.account,"groups":groups},ensure_ascii=False),encoding="utf-8")
                tmp.replace(self.data/"catalog.json")
                self.events.put(("groups",(source.account,groups)))
                self.progress(f"已加载 {len(groups)} 个聊天；选择一个聊天和日期后再同步")
            finally:
                source.close()
        self.run(task)

    def save_and_sync(self):
        if self.busy:
            return
        try:
            self.prepare_scope(persist=True)
        except ValueError as exc:
            self.status.set(str(exc))
            return
        force=self.force_check.get()
        self.status.set("正在检查本地日期存档…")
        self.run(lambda:self.events.put(("sync",self.update_messages(force))))

    def open_archive(self):
        if self.busy:
            return
        try:
            self.prepare_scope(persist=True)
        except ValueError as exc:
            self.status.set(str(exc))
            return
        folder=archive_dir(self.store,self.store.scope[0])
        if not folder.exists():
            self.status.set("此聊天尚未生成文本文件，请先同步所选日期")
            return
        os.startfile(folder)

    def settings_dialog(self):
        if self.busy:
            return
        from settings_ui import SettingsDialog
        def save(options):
            self.config.update(options)
            self.config.pop("rag_input_tokens",None)
            self.context_mode.set(ANALYSIS_MODE_LABELS.get(self.config.get("context_mode"),ANALYSIS_MODE_LABELS["full"]))
            write_config(self.data,self.config)
            self.status.set("API 设置已保存；同步时处理当前日期范围内尚未解读的图片")
        SettingsDialog(self.root,self.data,self.config,save)

    def change_context_mode(self):
        if self.busy:
            return
        self.config["context_mode"]="rag" if self.context_mode.get()==ANALYSIS_MODE_LABELS["rag"] else "full"
        write_config(self.data,self.config)
        self.status.set("已切换为"+self.context_mode.get()+"；本地消息和已有问答继续保留")

    def ask(self):
        if self.busy:
            return
        try:
            self.prepare_scope(persist=True)
        except ValueError as exc:
            self.status.set(str(exc))
            return
        question=self.question.get("1.0","end").strip()
        if not question or len(question)>3000:
            self.status.set("请输入 1 至 3000 字的问题")
            return
        self.update_coverage()
        self.ensure_conversation()
        history=model_history(self.conversation_turns)
        turns=list(self.conversation_turns)
        mode=self.config.get("context_mode","full")
        session=self.conversation_id
        scope=self.conversation_scope
        self.question.delete("1.0","end")
        self.question.insert("1.0",question)
        self.question.edit_modified(False)
        request_id=self.conversations.begin_request(scope,session,question)
        self.pending_question=question
        self.partial_turn=None
        self.render_conversation()
        def task():
            try:
                def notice(info):
                    self.conversations.update_request(request_id,coverage=info)
                    self.events.put(("context_notice",info))
                def partial(text):
                    self.conversations.update_request(request_id,partial=text)
                    self.events.put(("answer_partial",{"scope":scope,"session":session,"question":question,"text":text}))
                def reasoning(text):
                    self.conversations.update_request(request_id,partial_reasoning=text)
                    self.events.put(("reasoning_partial",{"scope":scope,"session":session,"question":question,"text":text}))
                answer=self.make_client().answer(self.store,question,self.progress,history=history,stop=self.stop,
                    context_limit=self.config.get("context_limit",1_000_000),
                    max_input_tokens=self.config.get("max_input_tokens",800_000),notice=notice,on_partial=partial,
                    mode=mode,turns=turns,session=session,on_reasoning=reasoning)
                answer.update(question=question,conversation_id=session,conversation_scope=scope,
                    request_id=request_id,autosaved=True)
                answer["source_text"]=self.answer_source_text(answer)
                answer["coverage_text"]=self.coverage_text(answer["coverage"])
                self.conversations.complete_request(scope,session,request_id,answer)
                self.events.put(("answer",answer))
            except Exception as exc:
                self.conversations.update_request(request_id,error=str(exc))
                raise
        self.status.set("准备压缩历史并检索当前范围的本地记录…" if mode=="rag" else "准备读取当前日期范围的全部本地记录…")
        self.run(task)

    def export_answer(self):
        # Read committed snapshots rather than waiting for UI event delivery.
        turns=list(self.conversation_turns)
        if self.conversation_scope is not None and self.conversation_id is not None:
            turns=self.conversations.export_turns(self.conversation_scope,self.conversation_id)
        if not turns:
            messagebox.showinfo("尚无回答","先完成一次检索。",parent=self.root)
            return
        path=filedialog.asksaveasfilename(parent=self.root,title="保存对话",defaultextension=".md",filetypes=[("Markdown","*.md"),("文字文件","*.txt")])
        if path:
            try:
                Path(path).write_text(export_markdown(turns),encoding="utf-8")
            except OSError as exc:
                self.status.set("对话未保存："+str(exc))
                messagebox.showerror("对话未保存",str(exc),parent=self.root)
                return
            self.status.set("对话、API 返回的思考内容及各轮引用已保存；未完成内容已单独标记")

    @staticmethod
    def answer_source_text(payload):
        sources="检索条件（同时受所选聊天和日期限制）：\n"+json.dumps(payload["plan"],ensure_ascii=False,indent=2)
        if payload.get("analysis_coverage"):
            sources+="\n\n"+payload["analysis_coverage"]["notice"]
        statistics=payload.get("retrieval_statistics")
        if statistics:
            sources+=f"\n\n本轮全部匹配记录：{statistics['count']} 条（完整本地统计，不受正文展示条数限制）"
            if statistics.get("earliest_time"):
                sources+=f"\n实际最早：{statistics['earliest_time']}\n实际最晚：{statistics['latest_time']}（均为北京时间）"
            if payload.get("has_more"):
                sources+="\n以下原文仅展示本轮部分证据，正文可能截取；不影响上述统计。"
            elif statistics.get("all_matches_included"):
                sources+=("\n以下为本轮全部匹配原文及补充的前后对话，已完整提供给模型；匹配数量、补充上下文及所选范围全部消息分别统计。"
                    if payload.get("conversation_passages") else "\n以下为本轮全部匹配原文，已完整提供给模型；匹配范围与所选范围全部消息分别统计。")
        passages={p["start_ref"]:p for p in payload.get("conversation_passages",[])}
        for row in payload["sources"]:
            if row["ref"] in passages:
                passage=passages[row["ref"]]
                sources+=f"\n\n对话片段 {passage['ref']} · [{passage['start_ref']}] 至 [{passage['end_ref']}] · {passage['message_count']} 条（时间相邻，不保证完整话题）"
            role={"match":" · 关键词命中","context":" · 补充上下文"}.get(row.get("retrieval_role"),"")
            sources+=f"\n\n[{row['ref']}] {row['group']} · {row['time']} · {row['sender']}{role}\n{row['text']}"
        if payload.get("assets"):
            sources+="\n\n素材词典（AI 解读）："
            for digest,value in payload["assets"].items():
                sources+=f"\n\n素材 #{digest[:16]}\n"+value["description"]
        return sources

    def show_answer_sources(self,payload):
        self.last_answer=payload
        sources=self.answer_source_text(payload)
        self.set_preview_text(self.sources_text,sources)
        self.show_originals(payload["sources"],["本轮对话引用的原始资料 · 北京时间"])
        self.preview_label.set("本轮对话引用 · 选择日期可返回按天预览")
        self.update_coverage()
        payload["source_text"]=sources
        payload.setdefault("coverage_text",self.coverage_widget.get("1.0","end"))

    def poll(self):
        show_after=False
        try:
            while True:
                kind,payload=self.events.get_nowait()
                if kind=="status":
                    self.status.set(payload)
                elif kind=="context_notice":
                    self.status.set(payload["notice"])
                    self.set_text(self.coverage_widget,self.coverage_widget.get("1.0","end").rstrip()+"\n\n本次模型输入：\n"+payload["notice"])
                elif kind=="answer_partial":
                    if tuple(payload["scope"])==self.conversation_scope and payload["session"]==self.conversation_id:
                        reasoning=(self.partial_turn or {}).get("reasoning_content","")
                        self.partial_turn={"question":payload["question"],"answer":"> 未完成回答：已收到片段已自动保存，正在接收。\n\n"+payload["text"],
                            "reasoning_content":reasoning,"incomplete":True}
                        self.render_conversation(preserve_view=True)
                elif kind=="reasoning_partial":
                    if tuple(payload["scope"])==self.conversation_scope and payload["session"]==self.conversation_id:
                        first_partial=self.partial_turn is None
                        if self.partial_turn is None:
                            self.partial_turn={"question":payload["question"],"answer":"> 未完成回答：已收到片段已自动保存，正在接收。\n\n已记录模型思考，尚未收到回答正文。","incomplete":True}
                        self.partial_turn["reasoning_content"]=payload["text"]
                        if first_partial:
                            self.render_conversation(preserve_view=True)
                elif kind=="confirm_vlm":
                    plan=payload["plan"]
                    text=(f"聊天：{plan['chat']}\n日期：{plan['start']} 至 {plan['end']}（北京时间）\n"
                        f"模型：{plan['model']}\n\n"
                        f"需调用 VLM：{plan['unique_assets']} 个不同素材\n"
                        f"其中图片 {plan['image_assets']} 个，静态表情 {plan['emoji_assets']} 个\n"
                        f"对应 {plan['queued_messages']} 条消息，同一素材只调用一次\n"
                        f"本次最多同时调用 {plan['concurrency']} 个（设置上限 {plan['concurrency_limit']}）\n\n"
                        "确认后会将这些素材发送给所配置的 VLM。\n取消会保留文字和本地素材，本次不发送图片。\n\n是否开始？")
                    try:
                        if not self.stop.is_set():
                            self.status.set("等待你确认调用 VLM…")
                            payload["approved"]=messagebox.askyesno("确认调用 VLM",text,parent=self.root,default="no") is True
                    finally:
                        payload["event"].set()
                elif kind=="groups":
                    self.account,self.groups=payload
                    if self.config.get("account") and self.config["account"]!=self.account:
                        self.selected_names=set()
                    self.loaded=True
                    self.render_groups()
                    show_after=True
                elif kind=="sync":
                    self.sync_errors=payload["errors"]
                    text=("已复用本地历史存档" if payload["reused"] else "已同步所选日期")+f"，新增 {payload['added']} 条；更新 {payload['files']} 个日期文本"
                    if payload.get("scope"):
                        bounds=payload["scope"]
                        text=f"{bounds['name']} · {bounds['start']} 至 {bounds['end']}（北京时间）· "+text
                    vision=payload.get("vision")
                    if vision:
                        text+=f"；素材解读 {vision['done']} 条（复用 {vision['cached']}），跳过动态 {vision.get('animated',0)} 条，失败 {vision['failed']} 条"
                        text+=f"；实际 VLM 调用 {vision.get('api_calls',0)} 次"
                        if vision.get("notice"):
                            text+="；"+vision["notice"]
                    if payload["errors"]:
                        text+="；同步未完成："+"；".join(payload["errors"])
                    self.status.set(text)
                    self.force_check.set(False)
                    show_after=True
                elif kind=="answer":
                    self.ensure_conversation()
                    if payload.get("autosaved") and tuple(payload["conversation_scope"])!=self.conversation_scope:
                        self.pending_question=""
                        self.status.set("回答已自动保存至原会话，切回该聊天及日期范围即可查看")
                        continue
                    self.show_answer_sources(payload)
                    self.pending_question=""
                    if payload.get("question"):
                        scope=tuple(payload.get("conversation_scope") or self.conversation_scope)
                        session=payload.get("conversation_id") or self.conversation_id
                        if not payload.get("autosaved"):
                            self.conversations.append(scope,session,payload)
                        self.load_conversation(restore_draft=False)
                        if self.question.get("1.0","end").strip()==payload["question"]:
                            self.question.delete("1.0","end")
                            self.question.edit_modified(False)
                            self.save_question_draft()
                    else:
                        self.answer_text.render_conversation([dict(payload,question="")],self.show_answer_sources)
                    self.tabs.select(0)
                    info=payload.get("analysis_coverage")
                    span=((f" · RAG 已发送 {info.get('included_matched_messages',info['included_messages'])}/{info.get('matched_messages',info['total_messages'])} 条匹配原文，补充上下文 {info.get('context_messages',0)} 条，约 {info['estimated_input_tokens']} 输入 token"
                        if info.get("mode")=="rag" else f" · 已发送 {info['included_messages']}/{info['total_messages']} 条，排除较早 {info['omitted_messages']} 条") if info else "")
                    self.status.set(f"回答已保存{span} · {len(payload['sources'])} 条原文可核对，可继续追问或查看本轮引用")
                elif kind=="error":
                    if self.partial_turn:
                        self.partial_turn["answer"]=self.partial_turn["answer"].replace("已收到片段已自动保存，正在接收。","连接已中断，以下片段已自动保存，不能视为完整分析。",1)
                    if self.pending_question:
                        self.pending_question=""
                        self.render_conversation()
                    self.status.set("操作失败："+payload[:240])
                    self.update_coverage(extra="操作失败："+payload)
                elif kind=="done":
                    self.busy=False
                    self.update_preview_controls()
                    for button in (self.ask_button,self.sync_button,self.refresh_button,self.local_button,self.reanalyze_button,self.new_conversation_button):
                        button.configure(state="normal")
                    self.session_box.configure(state="readonly")
                    self.context_mode_box.configure(state="readonly")
                    for entry in self.date_entries:
                        entry.configure(state="normal")
        except queue.Empty:
            pass
        if show_after:
            self.refresh_local_pending=True
        if self.refresh_local_pending and not self.busy:
            self.refresh_local_pending=False
            self.show_local()
        self.root.after(100,self.poll)

    @staticmethod
    def coverage_text(groups):
        lines=["当前检索范围（仅当前聊天及所选日期的本地记录）：","所有时间均为"+TIMEZONE_LABEL]
        for group in groups:
            span=display_time(group["earliest"])+" 至 "+display_time(group["latest"]) if group["earliest"] is not None else "暂无消息"
            lines.append(f"\n{group['name']} · {group['count']} 条\n本地原文时间：{span}\n最近成功同步：{display_sync_time(group['synced_at'])}")
        if not groups:
            lines.append("尚未选择聊天")
        lines.append("\n检索仅覆盖本机已有、已导入消息；图片解读为 AI 生成，语音和其他附件尚未解析。")
        return "\n".join(lines)

    def update_coverage(self,extra=""):
        if self.store.scope is None:
            self.set_text(self.coverage_widget,"请先选择一个聊天和日期范围。\n所有时间均为"+TIMEZONE_LABEL)
            return
        chat,start,end=self.store.scope
        groups=self.store.stats()
        group=groups[0] if groups else None
        text=(f"聊天：{group['name']}\n" if group else "")
        text+=f"所选日期：{display_time(start)[:10]} 至 {display_time(end-1)[:10]}（含首尾日期，均为北京时间）"
        if group:
            text+=f"\n本地消息：{group['count']} 条 · 最近成功同步：{display_sync_time(group['synced_at'])}"
            if group["earliest"] is not None:
                text+=f"\n范围内实际最早：{display_time(group['earliest'])}\n范围内实际最晚：{display_time(group['latest'])}"
                text+="\n以上为所选范围内完整本地统计，不受回答引用条数限制。"
        until=min(end,int(datetime.now(TZ).timestamp())+1)
        gaps=missing_ranges(self.store,chat,start,until)
        text+="\n本次日期范围"+("尚有未同步部分；提问只使用已存内容。" if gaps else "已完成文字同步。")
        for row in covered_ranges(self.store,chat):
            a,b=max(start,row["start"]),min(end,row["end"])
            if a<b:
                text+=f"\n已保存：{display_time(a)} 至 {display_time(b-1)}"
        labels={"pending":"待解读","done":"已解读","unavailable":"本机完整素材缺失","skipped_size":"大小不符","skipped_animated":"动态未识别","failed":"失败"}
        states=self.store.image_status()
        if states:
            text+="\n图片与表情："+"；".join(labels.get(r["status"],r["status"])+f" {r['count']}" for r in states)
        for failure in self.store.image_failures():
            text+="\n图片处理："+failure["error"]
        if self.sync_errors:
            text+="\n\n上次同步的问题：\n"+"\n".join(self.sync_errors)
        if extra:
            text+="\n\n"+extra
        text+="\n文本位置："+str(archive_dir(self.store,chat))
        text+="\n仅覆盖本机已导入消息。图片解读为 AI 生成；语音及其他附件尚未解析。"
        self.set_text(self.coverage_widget,text)

    def close(self):
        self.save_question_draft()
        self.stop.set()
        if self.busy:
            self.status.set("正在结束当前操作并保存本地数据…")
            self.root.after(200,self.close)
            return
        self.root.destroy()


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--data-dir")
    parser.add_argument("--no-auto",action="store_true")
    parser.add_argument("--smoke-test",action="store_true")
    parser.add_argument("--verify-source",action="store_true")
    args=parser.parse_args()
    data=Path(args.data_dir) if args.data_dir else application_dir()/"data"
    if sys.stderr is None:
        sys.stderr=open(os.devnull,"w")
    if sys.stdout is None:
        sys.stdout=open(os.devnull,"w")
    if args.verify_source:
        from wechat_source import Source
        data.mkdir(parents=True,exist_ok=True)
        try:
            source=Source(data)
            try:
                report={"groups":len(source.groups()),"unkeyed":len(source.db.unkeyed),"ok":True}
            finally:
                source.close()
        except Exception as exc:
            report={"ok":False,"error":str(exc)}
        (data/"source-check.json").write_text(json.dumps(report,ensure_ascii=False),encoding="utf-8")
        return
    enable_high_dpi()
    root=tk.Tk()
    root.withdraw()
    App(root,data,auto=not args.no_auto and not args.smoke_test)
    root.deiconify()
    if args.smoke_test:
        root.after(500,root.destroy)
    root.mainloop()


if __name__=="__main__":
    main()
