"""Merge bounded neighboring-message windows without dropping keyword matches."""
from bisect import bisect_left,bisect_right
from itertools import groupby


CONTEXT_RADIUS=20
CONTEXT_SECONDS=15*60
CONTEXT_IDLE_SECONDS=5*60
CONTEXT_POLICY={"messages_before":CONTEXT_RADIUS,"messages_after":CONTEXT_RADIUS,
                "seconds_each_side":CONTEXT_SECONDS,"idle_break_seconds":CONTEXT_IDLE_SECONDS}


def conversation_windows(metadata,matched_ids,check=lambda:None,*,radius=CONTEXT_RADIUS):
    """Return disjoint [start,end) positions, split at long conversation gaps.

    Metadata must be chronological by (timestamp, seq, id), inside query scope.
    Windows describe temporal proximity, not a semantic topic guarantee.
    """
    if not metadata or not matched_ids:
        return []
    stamps=[row["timestamp"] for row in metadata]
    windows=[]
    session_start=0
    for session_end in range(1,len(metadata)+1):
        check()
        if session_end<len(metadata) and stamps[session_end]-stamps[session_end-1]<=CONTEXT_IDLE_SECONDS:
            continue
        session_windows=[]
        for position in range(session_start,session_end):
            check()
            if metadata[position]["id"] not in matched_ids:
                continue
            left=max(session_start,position-radius,bisect_left(stamps,stamps[position]-CONTEXT_SECONDS))
            right=min(session_end,position+radius+1,bisect_right(stamps,stamps[position]+CONTEXT_SECONDS))
            if session_windows and left<=session_windows[-1][1]:
                session_windows[-1]=(session_windows[-1][0],max(right,session_windows[-1][1]))
            else:
                session_windows.append((left,right))
        windows.extend(session_windows)
        session_start=session_end
    return windows


def narrow_context(records,radius,check=lambda:None):
    """Narrow an existing frozen snapshot; never reread live messages/index.

    Each original passage is contiguous in source order. Process passages
    separately so removing gaps cannot invent new neighboring relationships.
    """
    if isinstance(radius,bool) or not isinstance(radius,int) or not 0<=radius<=CONTEXT_RADIUS:
        raise ValueError("上下文窗口必须是 0 至 20 的整数")
    narrowed=[]
    passages=[]
    for _,values in groupby(records,key=lambda row:row["passage_ref"]):
        check()
        group=list(values)
        matches={row["id"] for row in group if row["retrieval_role"]=="match"}
        for left,right in conversation_windows(group,matches,check,radius=radius):
            passage={"ref":f"C{len(passages)+1}","message_ids":[],"matched_ids":[]}
            for position in range(left,right):
                check()
                row=dict(group[position],passage_ref=passage["ref"])
                narrowed.append(row)
                passage["message_ids"].append(row["id"])
                if row["retrieval_role"]=="match":
                    passage["matched_ids"].append(row["id"])
            passages.append(passage)
    if {row["id"] for row in narrowed if row["retrieval_role"]=="match"}!={row["id"] for row in records if row["retrieval_role"]=="match"}:
        raise RuntimeError("缩小上下文未保留全部匹配原文，本次未发送原文分析请求")
    return narrowed,passages
