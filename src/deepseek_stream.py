"""Read DeepSeek SSE incrementally; never retry an interrupted paid request."""
import codecs
import http.client
import json
import time


STREAM_WAIT_SECONDS=30*60
STREAM_IDLE_SECONDS=10*60


def read_stream(response,*,progress,on_partial,stop,api_error,started=None,on_reasoning=None):
    if started is None:
        started=time.monotonic()
    last_progress=last_saved=started
    pieces=[]
    reasoning_pieces=[]
    saved_length=saved_reasoning_length=reasoning_length=content_length=0
    finish=None
    usage=None
    decoder=codecs.getincrementaldecoder("utf-8")()
    buffer=""
    data=[]
    done=False

    def snapshot(force=False):
        nonlocal last_saved,saved_length,saved_reasoning_length
        now=time.monotonic()
        length=content_length
        changed=length!=saved_length or reasoning_length!=saved_reasoning_length
        if changed and (force or now-last_saved>=1 or length-saved_length+reasoning_length-saved_reasoning_length>=2048):
            if length!=saved_length:
                on_partial("".join(pieces))
            if reasoning_length!=saved_reasoning_length and on_reasoning is not None:
                on_reasoning("".join(reasoning_pieces))
            saved_length=length
            saved_reasoning_length=reasoning_length
            last_saved=now

    def check():
        if stop is not None and stop.is_set():
            raise api_error("已停止接收；服务端可能已产生费用，本次未自动重发，已收到片段保留在本地")
        if time.monotonic()-started>=STREAM_WAIT_SECONDS:
            raise api_error("等待 DeepSeek 回复超过 30 分钟；服务端可能已产生费用，本次未自动重发，已收到片段保留在本地")

    def event():
        nonlocal done,finish,usage,reasoning_length,content_length
        if not data:
            return
        raw="\n".join(data)
        data.clear()
        if raw=="[DONE]":
            done=True
            return
        try:
            payload=json.loads(raw)
            if not isinstance(payload,dict) or payload.get("error"):
                raise ValueError()
            if payload.get("usage") is not None:
                usage=payload["usage"]
            for choice in payload.get("choices",[]):
                if choice.get("index",0)!=0:
                    continue
                delta=choice.get("delta") or {}
                text=delta.get("content")
                reasoning=delta.get("reasoning_content")
                if text is not None and not isinstance(text,str):
                    raise ValueError()
                if reasoning is not None and not isinstance(reasoning,str):
                    raise ValueError()
                if text:
                    pieces.append(text)
                    content_length+=len(text)
                if isinstance(reasoning,str):
                    reasoning_pieces.append(reasoning)
                    reasoning_length+=len(reasoning)
                if choice.get("finish_reason") is not None:
                    finish=choice["finish_reason"]
        except (ValueError,TypeError,KeyError,AttributeError):
            raise api_error("DeepSeek 流式回复异常；本次未自动重发，已收到片段保留在本地") from None

    def line(value):
        if value.endswith("\r"):
            value=value[:-1]
        if not value:
            event()
        elif value.startswith("data:"):
            value=value[5:]
            data.append(value[1:] if value.startswith(" ") else value)
        # SSE comments (including keep-alive) and other fields are ignored.

    read=getattr(response,"read1",None) or response.read
    progress("已连接 DeepSeek，等待长文本处理或首个回答片段…")
    try:
        while not done:
            check()
            chunk=read(65536)
            check()
            if not chunk:
                buffer+=decoder.decode(b"",final=True)
                if buffer:
                    line(buffer)
                event()
                if not done:
                    raise api_error("DeepSeek 连接提前结束，无法确认回答完整；服务端可能已产生费用，本次未自动重发，已收到片段保留在本地")
                break
            buffer+=decoder.decode(chunk)
            while "\n" in buffer and not done:
                value,buffer=buffer.split("\n",1)
                line(value)
            snapshot()
            now=time.monotonic()
            if now-last_progress>=1:
                length=content_length
                stage=(f"正在接收回答 · 已收到 {length:,} 字" if length else
                    (f"模型正在思考 · 已返回 {reasoning_length:,} 字思考信息" if reasoning_length else
                     "连接正常 · 等待长文本处理及首个回答片段"))
                progress(f"{stage} · 已等待 {int(now-started)} 秒")
                last_progress=now
        if finish is None:
            raise api_error("DeepSeek 回复缺少完成标记；本次未自动重发，已收到片段保留在本地")
        return {"choices":[{"message":{"content":"".join(pieces),"reasoning_content":"".join(reasoning_pieces)},
            "finish_reason":finish}],"usage":usage}
    except (OSError,http.client.HTTPException):
        raise api_error("DeepSeek 接收超时或连接中断；服务端可能已产生费用，本次未自动重发，已收到片段保留在本地") from None
    except UnicodeError:
        raise api_error("DeepSeek 流式回复编码异常；本次未自动重发，已收到片段保留在本地") from None
    finally:
        snapshot(force=True)
