"""Selectable Markdown in native Tk; no HTML execution or automatic media loads."""
import re
import tkinter as tk
import webbrowser
from display import display_for
from ui_theme import COLORS


class MarkdownText(tk.Text):
    def __init__(self,parent,**options):
        super().__init__(parent,**options)
        self.display=display_for(self.winfo_toplevel())
        for level in range(1,7):
            self.tag_configure(f"h{level}",font=self.display.font(("Microsoft YaHei UI",max(11,21-level*2),"bold")),
                spacing1=self.display.px(8),spacing3=self.display.px(4))
        for name,spec in {"bold":("Microsoft YaHei UI",11,"bold"),"italic":("Microsoft YaHei UI",11,"italic"),
            "bolditalic":("Microsoft YaHei UI",11,"bold italic"),"code":("Consolas",10),"table":("Consolas",10)}.items():
            self.tag_configure(name,font=self.display.font(spec))
        self.tag_configure("code",background=COLORS["soft"],lmargin1=self.display.px(10),lmargin2=self.display.px(10))
        self.tag_configure("quote",foreground=COLORS["muted"],lmargin1=self.display.px(18),lmargin2=self.display.px(18))
        self.tag_configure("link",foreground=COLORS["hover"],underline=True)
        self.tag_configure("strike",overstrike=True)
        self.tag_configure("role",font=self.display.font(("Microsoft YaHei UI",11,"bold")),
            foreground=COLORS["accent"],spacing1=self.display.px(12),spacing3=self.display.px(5))
        self.tag_configure("muted",foreground=COLORS["muted"])
        self._actions=[]
        self._tables=[]

    def begin(self):
        self.configure(state="normal")
        self.delete("1.0","end")
        for tag in self._actions:
            self.tag_delete(tag)
        self._actions=[]
        for tag in self._tables:
            self.tag_delete(tag)
        self._tables=[]

    def finish(self,follow_end=True):
        self.configure(state="disabled")
        if follow_end:
            self.see("end")

    def action(self,text,callback,tags=()):
        tag=f"action_{len(self._actions)}"
        self._actions.append(tag)
        self.insert("end",text,(*tags,"link",tag))
        self.tag_bind(tag,"<Button-1>",lambda _:callback())
        self.tag_bind(tag,"<Enter>",lambda _:self.configure(cursor="hand2"))
        self.tag_bind(tag,"<Leave>",lambda _:self.configure(cursor="xterm"))

    def inline(self,text,tags=(),depth=0):
        if depth>8:
            self.insert("end",text,tags)
            return
        pattern=re.compile(r"(`+)(.+?)\1|\[([^\]\n]+)\]\((https?://[^\s)]+)\)|(\*\*\*|___|\*\*|__|~~|\*|_)(?=\S)(.+?)(?<=\S)\5|\\([\\`*_{}\[\]()#+.!>|~-])")
        pos=0
        for match in pattern.finditer(text):
            self.insert("end",text[pos:match.start()],tags)
            if match[1]:
                self.insert("end",match[2],(*tags,"code"))
            elif match[3]:
                self.action(match[3],lambda url=match[4]:webbrowser.open(url),tags)
            elif match[5]:
                style={"***":"bolditalic","___":"bolditalic","**":"bold","__":"bold","~~":"strike","*":"italic","_":"italic"}[match[5]]
                self.inline(match[6],(*tags,style),depth+1)
            else:
                self.insert("end",match[7],tags)
            pos=match.end()
        self.insert("end",text[pos:],tags)

    def markdown(self,text):
        lines=text.replace("\r\n","\n").split("\n")
        fence=None
        index=0
        while index<len(lines):
            line=lines[index]
            marker=re.match(r"^\s{0,3}(`{3,}|~{3,})(.*)$",line)
            if fence:
                if marker and marker[1][0]==fence[0] and len(marker[1])>=len(fence):
                    fence=None
                else:
                    self.insert("end",line+"\n",("code",))
            elif marker:
                fence=marker[1]
            elif index+1<len(lines) and "|" in line and re.fullmatch(r"\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*",lines[index+1]):
                rows=[[cell.strip() for cell in line.strip().strip("|").split("|")]]
                index+=2
                while index<len(lines) and "|" in lines[index] and lines[index].strip():
                    rows.append([cell.strip() for cell in lines[index].strip().strip("|").split("|")])
                    index+=1
                # Native tab stops retain readable columns without executing HTML.
                font=self.display.font(("Consolas",10))
                widths=[min(self.display.px(420),max(font.measure(row[col]) if col<len(row) else 0 for row in rows))
                    +self.display.px(18) for col in range(max(map(len,rows)))]
                layout=f"table_layout_{len(self._tables)}"
                self._tables.append(layout)
                self.tag_configure(layout,tabs=tuple(sum(widths[:i+1]) for i in range(len(widths))))
                for r,row in enumerate(rows):
                    self.inline("\t".join(row),("table",layout,"bold") if r==0 else ("table",layout))
                    self.insert("end","\n")
                continue
            else:
                heading=re.match(r"^ {0,3}(#{1,6})\s+(.+?)\s*#*\s*$",line)
                listing=re.match(r"^(\s*)(?:[-+*]|(\d+)[.)])\s+(.+)$",line)
                if heading:
                    self.inline(heading[2],(f"h{len(heading[1])}",))
                elif re.fullmatch(r"\s*(?:---+|\*\*\*+|___+)\s*",line):
                    self.insert("end","─"*36,("muted",))
                elif line.lstrip().startswith(">"):
                    self.inline(re.sub(r"^\s*>\s?","",line),("quote",))
                elif listing:
                    self.insert("end",listing[1]+(listing[2]+". " if listing[2] else "• "))
                    self.inline(listing[3])
                else:
                    self.inline(line)
                self.insert("end","\n")
            index+=1

    def render_conversation(self,turns,on_sources=None,pending="",*,preserve_view=False):
        # Streaming should follow new text only while the reader is at the end.
        restore=preserve_view and self.winfo_ismapped() and self.dlineinfo("end-1c") is None
        top=self.index("@0,0") if restore else None
        self.begin()
        if not turns and not pending:
            self.insert("end","从聊天记录中找到答案\n",("h2",))
            self.insert("end","整理讨论、查找细节，或继续追问。每轮分析都围绕当前聊天和日期范围。\n\n",("muted",))
            self.insert("end","输入问题后点击分析，回答中的引用可随时回到原文核对。\n",("muted",))
        for turn in turns:
            self.insert("end","你\n",("role",))
            self.insert("end",turn["question"]+"\n")
            self.insert("end","助手\n",("role",))
            self.markdown(turn["answer"])
            if on_sources and not turn.get("incomplete"):
                self.action("查看本轮引用",lambda value=turn:on_sources(value))
                self.insert("end","\n")
        if pending:
            self.insert("end","你\n",("role",))
            self.insert("end",pending+"\n")
            self.insert("end","正在检索并回答…\n",("muted",))
        self.finish(follow_end=not restore)
        if restore:
            self.yview(top)
