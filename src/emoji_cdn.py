"""Bounded WeChat CDN fallback. No VLM calls, arbitrary URLs or thumbnail use."""
from __future__ import annotations
import hashlib
import io
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor,wait,FIRST_COMPLETED
from PIL import Image

LIMIT=3*1024*1024


class CDNError(RuntimeError):
    pass


class CDNTooLarge(CDNError):
    pass


def checked_url(value):
    if not isinstance(value,str) or len(value)>8192 or any(ord(c)<32 for c in value):
        raise CDNError("表情 CDN 地址无效")
    try:
        parts=urllib.parse.urlsplit(value)
        host=(parts.hostname or "").lower()
        port=parts.port
    except ValueError:
        raise CDNError("表情 CDN 地址无效") from None
    allowed=any(host==d or host.endswith("."+d) for d in ("qpic.cn","qq.com","gtimg.com"))
    if parts.scheme not in ("http","https") or not allowed or parts.username or parts.password or port not in (None,80,443):
        raise CDNError("仅允许消息自带的腾讯表情 CDN 地址")
    return value


class CheckedRedirect(urllib.request.HTTPRedirectHandler):
    max_redirections=3
    max_repeats=2
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        checked_url(newurl)
        if req.type=="https" and urllib.parse.urlsplit(newurl).scheme!="https":
            raise CDNError("表情 CDN 重定向降级，已停止下载")
        return super().redirect_request(req,fp,code,msg,headers,newurl)


def fetch_bytes(url):
    checked_url(url)
    # Prefer TLS even when historical message XML contains an http URL.
    parts=urllib.parse.urlsplit(url)
    urls=[url]
    if parts.scheme=="http":
        urls.insert(0,urllib.parse.urlunsplit(parts._replace(scheme="https",netloc=parts.hostname)))
    for index,candidate in enumerate(urls):
        request=urllib.request.Request(candidate,headers={"User-Agent":"Mozilla/5.0","Accept":"image/*","Accept-Encoding":"identity"})
        try:
            opener=urllib.request.build_opener(CheckedRedirect())
            with opener.open(request,timeout=15) as response:
                checked_url(response.geturl())
                length=response.headers.get("Content-Length")
                if length and length.isdigit() and int(length)>LIMIT+16:
                    raise CDNTooLarge("CDN 表情超过 3 MB，已跳过")
                data=response.read(LIMIT+17)
                if len(data)>LIMIT+16:
                    raise CDNTooLarge("CDN 表情超过 3 MB，已跳过")
                return data
        except CDNError:
            raise
        except urllib.error.HTTPError as exc:
            raise CDNError(f"表情 CDN 返回 HTTP {exc.code}；地址可能已过期") from None
        except (urllib.error.URLError,TimeoutError,socket.timeout,OSError):
            # Some old CDN edges cannot serve valid TLS. Retry only the exact
            # HTTP URL already present in the message; never downgrade HTTPS
            # messages or disable certificate validation. MD5 is checked above
            # the transport before the downloaded original enters the cache.
            if index+1==len(urls):
                raise CDNError("表情 CDN 连接失败或超时，下次同步可重试") from None


def readable(data):
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format not in ("PNG","JPEG","GIF","WEBP"):
                return False
            image.verify()
        return True
    except Exception:
        return False


def decrypt_resource(data,key_text):
    if not re.fullmatch(r"[0-9a-fA-F]{32}",key_text or "") or not data or len(data)%16:
        return []
    from cryptography.hazmat.primitives.ciphers import Cipher,algorithms,modes
    key=bytes.fromhex(key_text)
    results=[]
    for mode in (modes.CBC(key),modes.CBC(bytes(16)),modes.ECB()):
        decryptor=Cipher(algorithms.AES(key),mode).decryptor()
        output=decryptor.update(data)+decryptor.finalize()
        pad=output[-1] if output else 0
        if 0<pad<=16 and output[-pad:]==bytes([pad])*pad:
            output=output[:-pad]
        if readable(output):
            results.append(output)
    return results


def candidates(metadata):
    expected={str(metadata.get(k,"")).lower() for k in ("md5","androidmd5","s60v3md5","s60v5md5")
        if re.fullmatch(r"[0-9a-fA-F]{32}",str(metadata.get(k,"")))}
    result=[]
    for field in ("cdnurl","encrypturl"):
        url=str(metadata.get(field,"")).strip()
        if not url:
            continue
        try:
            checked_url(url)
        except CDNError:
            continue
        key=hashlib.sha256((url+"\0"+metadata.get("aeskey","")+"\0"+",".join(sorted(expected))).encode()).hexdigest()
        if not any(c[0]==key for c in result):
            result.append((key,url,expected))
    return result


class EmojiCDN:
    def __init__(self,media,account,*,fetch=None,progress=lambda text:None):
        self.media,self.account=media,account
        self.fetch=fetch or fetch_bytes
        self.progress=progress
        self.failures={}

    def get(self,metadata):
        choices=candidates(metadata)
        if not choices:
            raise CDNError("消息未保留可用的完整表情 CDN 地址")
        errors=[]
        too_large=False
        for request_key,url,expected in choices:
            cached=self.media.by_cdn(self.account,request_key)
            if cached:
                return {"asset_hash":cached["hash"],"size":cached["bytes"],"quality":"cdn-cache"}
            if request_key in self.failures:
                error=self.failures[request_key]
                errors.append(error)
                too_large=too_large or isinstance(error,CDNTooLarge)
                continue
            try:
                self.progress("正在从微信 CDN 下载当前范围的完整表情（尚未调用 VLM）")
                source=self.fetch(url)
                if len(source)>LIMIT+16:
                    raise CDNTooLarge("CDN 表情超过 3 MB，已跳过")
                options=[source] if readable(source) else decrypt_resource(source,metadata.get("aeskey",""))
                candidate_too_large=False
                for raw in options:
                    if len(raw)>=LIMIT:
                        candidate_too_large=True
                        continue
                    if expected and hashlib.md5(raw).hexdigest() not in expected:
                        continue
                    return {"bytes":raw,"source_bytes":source,"size":len(raw),"quality":"wechat-cdn",
                        "cdn_request_key":request_key}
                if candidate_too_large:
                    raise CDNTooLarge("CDN 表情超过或等于 3 MB，已跳过")
                raise CDNError("CDN 返回内容不是可校验的原始表情图片，或与消息 MD5 不符")
            except CDNError as exc:
                self.failures[request_key]=exc
                errors.append(exc)
                too_large=too_large or isinstance(exc,CDNTooLarge)
        if too_large:
            raise CDNTooLarge("CDN 表情大小不符合要求，已跳过")
        raise errors[-1] if errors else CDNError("表情 CDN 暂不可用")


def parallel_sources(entries,cdn,concurrency,stop=None):
    """Fetch distinct resources with bounded concurrency, yielding rows to the owner thread."""
    concurrency=download_concurrency(concurrency)
    groups={}
    for index,row,meta in entries:
        digest=meta.get("md5","").lower()
        key=digest if re.fullmatch(r"[0-9a-f]{32}",digest) else hashlib.sha256(repr(sorted(meta.items())).encode()).hexdigest()
        job=groups.setdefault(key,{"rows":[],"metas":[]})
        job["rows"].append((index,row))
        if meta not in job["metas"]:
            job["metas"].append(meta)
    def stopped():
        return stop is not None and stop.is_set()
    def fetch(job):
        error=CDNError("表情下载已停止")
        for meta in job["metas"]:
            if stopped():
                break
            try:
                return cdn.get(meta),None
            except CDNError as exc:
                error=exc
        return None,error
    workers=min(concurrency,len(groups))
    if not workers:
        return
    iterator=iter(groups.values())
    exhausted=False
    futures={}
    completed=0
    with ThreadPoolExecutor(max_workers=workers,thread_name_prefix="emoji-cdn") as pool:
        while futures or not exhausted:
            while not exhausted and not stopped() and len(futures)<workers:
                try:
                    job=next(iterator)
                except StopIteration:
                    exhausted=True
                    break
                futures[pool.submit(fetch,job)]=job
            if stopped():
                for future in futures:
                    future.cancel()
                return
            if not futures:
                continue
            done,_=wait(futures,timeout=0.2,return_when=FIRST_COMPLETED)
            for future in done:
                job=futures.pop(future)
                try:
                    source,error=future.result()
                except Exception:
                    source,error=None,CDNError("表情 CDN 下载处理失败，下次同步可重试")
                completed+=1
                cdn.progress(f"表情 CDN 下载 {completed}/{len(groups)} · 在途 {len(futures)}/{workers}（尚未调用 VLM）")
                for index,row in job["rows"]:
                    yield index,row,source,error


def download_concurrency(value):
    if isinstance(value,bool) or not isinstance(value,(str,int)) or not str(value).strip().isdigit():
        raise ValueError("表情下载并发数请输入 1 至 200 的整数")
    value=int(value)
    if not 1<=value<=200:
        raise ValueError("表情下载并发数请输入 1 至 200 的整数")
    return value
