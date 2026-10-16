"""Compact sticker metadata for retrieval without inventing image semantics."""
from __future__ import annotations

import hashlib
import html
import re
import xml.etree.ElementTree as ET

EMOJI_KINDS = ("动画表情", "表情", "自定义表情", "静态表情", "动态表情")
REPLY_KINDS = ("文件/链接/卡片", "引用回复")


def _message_xml(content):
    text=_xml_text(content)
    if (len(text)>512*1024 or not text.startswith(("<msg", "<?xml"))
            or "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper()):
        return None
    try:
        root=ET.fromstring(text)
        return root if root.tag=="msg" else None
    except ET.ParseError:
        return None


def _quoted_text(content, kind, depth):
    """Keep quoted text literal; summarize only actual quoted media envelopes."""
    if kind=="1":
        return content.strip()
    root=_message_xml(content)
    if root is not None:
        app=root.find("appmsg")
        if app is not None:
            if app.findtext("type")=="57":
                return _reply_text(app,depth+1) if depth<3 else "[多层引用回复，详见原始资料]"
            title=(app.findtext("title") or "").strip()
            return "[文件/链接/卡片]"+(" "+title if title else "")
    labels={"3":"图片","34":"语音","43":"视频","47":"表情","49":"文件/链接/卡片","62":"视频"}
    return "["+labels.get(kind,"非文本消息")+"]"


def _reply_text(app, depth=0):
    refer=app.find("refermsg")
    title=(app.findtext("title") or "").strip()
    if refer is None:
        return "[引用回复]\n"+(title or "[回复正文为空]")+"\n引用：原消息信息缺失"
    name=" ".join((refer.findtext("displayname") or refer.findtext("chatusr") or "未知发言人").split())
    content=refer.findtext("content") or ""
    quote=_quoted_text(content,(refer.findtext("type") or "").strip(),depth)
    return "[引用回复]\n"+(title or "[回复正文为空]")+"\n引用 "+name+"：\n"+(quote or "[原消息内容为空]")


def compact_reply(kind, content):
    """Convert appmsg type 57 only; XML quoted in ordinary text stays unchanged."""
    if kind not in REPLY_KINDS or "<appmsg" not in content:
        return None
    root=_message_xml(content)
    app=root.find("appmsg") if root is not None else None
    if app is None or (app.findtext("type") or "").strip()!="57":
        return None
    return _reply_text(app)


def _xml_text(content):
    # Group message bodies may start with "sender_username:\n" before XML.
    return re.sub(r"^[A-Za-z0-9_.@-]{1,128}:\r?\n", "",content.strip(),count=1)


def compact_picture(content):
    text=_xml_text(content)
    if "<img" not in text or not text.startswith(("<msg","<img","<?xml")):
        return content
    match=re.search(r'\bmd5\s*=\s*[\'\"]([0-9a-fA-F]{32})[\'\"]',content)
    return "[图片"+(" #"+match[1].lower()[:12] if match else "")+"]"


def emoji_metadata(content):
    """Parse local metadata only; never expand DTDs or resolve URLs."""
    if "<emoji" not in content:
        return {}
    text=_xml_text(content)
    if not text.startswith(("<msg", "<emoji", "<?xml")):
        return {}
    node=None
    if len(text)<=256*1024 and "<!DOCTYPE" not in text.upper() and "<!ENTITY" not in text.upper():
        try:
            root=ET.fromstring(text)
            if root.tag=="emoji":
                node=root
            elif root.tag=="msg" and root.find("emoji") is not None:
                node=root.find("emoji")
            else:
                return {}
        except ET.ParseError:
            pass
        if node is None:
            # WeChat sometimes leaves '&' unescaped in CDN attributes. Read only
            # the bounded sticker start tag; never interpret declarations/URLs.
            tag=re.search(r"<emoji\b([^>]*)>",text,re.S)
            if tag:
                allowed={"md5","androidmd5","s60v3md5","s60v5md5","externmd5","aeskey","desc","attachedtext","cdnurl","encrypturl"}
                return {key:html.unescape(value) for key,quote,value in
                    re.findall(r"([a-zA-Z0-9_]+)\s*=\s*(['\"])(.*?)\2",tag[1],re.S) if key in allowed}
    return dict(node.attrib) if node is not None else {}


def compact_emoji(kind, content):
    """Keep quoted XML in text messages unchanged. Asset metadata stays local."""
    if kind not in EMOJI_KINDS or "<emoji" not in content:
        return None
    text=_xml_text(content)
    if not text.startswith(("<msg", "<emoji", "<?xml")):
        return None
    attrs=emoji_metadata(content)
    digest=""
    for key in ("md5","androidmd5","s60v3md5","externmd5"):
        value=attrs.get(key,"").strip()
        if re.fullmatch(r"[0-9a-fA-F]{32}",value):
            digest=value.lower()
            break
    if not digest:
        digest=hashlib.sha256(text.encode("utf-8")).hexdigest()
    descriptions=[]
    for key in ("desc","attachedtext"):
        value=" ".join(attrs.get(key,"").split())[:160]
        if value and value not in descriptions:
            descriptions.append(value)
    caption="；".join(descriptions)[:200]
    # WeChat's database type does not distinguish still and animated stickers.
    return "[表情"+("："+caption if caption else "")+" #"+digest[:12]+"]"
