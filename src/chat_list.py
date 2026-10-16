"""Numeric chat filtering and ordering, independent of the rendered labels."""
MIN_MESSAGES=1000
MIN_CONTENT_BYTES=50*1024


def visible_chats(groups,kind="群聊",term="",hide_small=True,sort_by="content_bytes"):
    term=term.strip().casefold()
    rows=[]
    for index,group in enumerate(groups):
        actual_kind=group.get("kind","群聊" if group["username"].endswith("@chatroom") else "个人")
        if actual_kind!=kind or (term and term not in group["name"].casefold()):
            continue
        known="message_count" in group and "content_bytes" in group
        if hide_small and known and (group["message_count"]<MIN_MESSAGES or group["content_bytes"]<MIN_CONTENT_BYTES):
            continue
        rows.append((index,group))
    primary="message_count" if sort_by=="message_count" else "content_bytes"
    secondary="content_bytes" if primary=="message_count" else "message_count"
    rows.sort(key=lambda pair:(-pair[1].get(primary,-1),-pair[1].get(secondary,-1),pair[1]["name"].casefold()))
    return rows


def size_label(value):
    if value is None:
        return "—"
    if value>=1024*1024:
        return f"{value/(1024*1024):,.2f} MB"
    return f"{value/1024:,.2f} KB"
