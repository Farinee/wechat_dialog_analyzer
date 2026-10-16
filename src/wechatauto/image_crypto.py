"""Read-only image decryption subset of wechatauto-replica/media.py.
Apache-2.0; see LICENSE. No UI automation or sending methods are included.
Adapted: non-blocking scans and app-controlled output/key storage.
"""


from __future__ import annotations


import ctypes


import glob


import hashlib


import json


import os


import re


import struct


import tempfile


import time


from typing import List, Optional, Tuple


from wechatauto.logger import wxlog


V1_MAGIC = b"\x07\x08\x05\x56\x02\x05"


V2_MAGIC = b"\x07\x08\x56\x32\x08\x07"


V1_HEADER_SZ = 22  # 6B sig + 16B xor key


AES16_RE = re.compile(rb"[0-9a-zA-Z]{16,32}")


DEFAULT_SAVE_PATH = ""


def _jpeg_like(pt: bytes) -> bool:
    return (
        (pt[:3] == b"\xff\xd8\xff")
        or pt[:4] in (b"\x89PNG", b"GIF8", b"RIFF")
        or pt[:4] == b"wxgf"  # 微信动画表情容器
    )


def aligned_aes_block_size(aes_size: int) -> int:
    return aes_size + (16 - aes_size % 16) if aes_size % 16 else aes_size + 16


_FOOTER_MAX = 32


_IMG_END_MARK = (
    (b"\xff\xd8", b"\xff\xd9"),
    (b"\x89PNG\r\n\x1a\n", b"\x49\x45\x4e\x44\xae\x42\x60\x82"),
)


def strip_container_footer(plain: bytes) -> Tuple[bytes, int]:
    """按图片结束标记裁掉尾部页脚，返回 (明文, 被裁掉的字节数)。"""
    for soi, end in _IMG_END_MARK:
        if not plain.startswith(soi) or len(plain) <= len(end):
            continue
        if plain.endswith(end):
            return plain, 0
        i = plain.rfind(end)
        if i > 0 and len(plain) - i - len(end) <= _FOOTER_MAX:
            return plain[:i + len(end)], len(plain) - i - len(end)
    return plain, 0


class MediaDownloader:


    def __init__(self, db, save_dir: Optional[str] = None,
                 image_key: Optional[str] = None,
                 cfg_dword: Optional[int] = None):
        self.db = db
        self.save_dir = save_dir or DEFAULT_SAVE_PATH
        self._image_key = image_key  # 显式注入的图片 AES 密钥
        self._cfg_dword = cfg_dword  # cfg+0x40, 派生图片密钥(最佳方案)
        self._xor_key: Optional[int] = None
        self._img_key: Optional[Tuple[str, int]] = None
        self._key_probe: Optional[bytes] = None


    @staticmethod
    def derive_image_keys(cfg_dword: int, wxid: str) -> Tuple[str, int]:
        """cfgDword 派生图片密钥(微信 4.x 最佳方案, 实测 3000/3000 验证)。

        imageXorKey = cfgDword & 0xFF
        imageAesKey = MD5(str(cfgDword) + wxid)[:16]   # 前 16 位即真 AES-128 密钥
        """
        xor_key = cfg_dword & 0xFF
        aes_key = hashlib.md5(
            ("%d" % cfg_dword + wxid).encode("utf-8")).hexdigest()[:16]
        return aes_key, xor_key


    def _derive_cfg_key(self) -> Optional[Tuple[str, int]]:
        """cfgDword 派生并验证; 优先显式注入, 否则用 db.cfg_dword(自动提取)。"""
        cfg_dword = self._cfg_dword
        if cfg_dword is None:
            cfg_dword = getattr(self.db, "cfg_dword", None)
        if not cfg_dword:
            return None
        aes_key, xor_key = self.derive_image_keys(cfg_dword, self.db.wxid)
        if self._validate_key(aes_key):
            return aes_key, xor_key
        return None


    def _probe_ct(self, dat_path: Optional[str] = None) -> bytes:
        """取一张 V2 图片的密文首块，作为密钥反测试样"""
        if self._key_probe is not None:
            return self._key_probe
        if dat_path is None:
            base = os.path.join(self.db.account_dir, "msg", "attach")
            hits = glob.glob(os.path.join(base, "*", "*", "Img", "*.dat"))
            if not hits:
                return b""
            dat_path = hits[0]
        with open(dat_path, "rb") as f:
            head = f.read(32)
        if head[:6] == V2_MAGIC:
            self._key_probe = head[15:31]
        else:
            self._key_probe = head[V1_HEADER_SZ: V1_HEADER_SZ + 16]
        return self._key_probe


    def _validate_key(self, aes_key: str) -> bool:
        """用真实密文首块反测密钥是否有效"""
        probe = self._probe_ct()
        if not probe:
            return False
        try:
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
            dec = Cipher(algorithms.AES(aes_key.encode()), modes.ECB()).decryptor()
            return _jpeg_like(dec.update(probe) + dec.finalize())
        except Exception:
            return False


    def _key_store(self) -> str:
        return os.path.join(self.db.workdir, "image_keys.json")


    def _load_persisted_key(self) -> Optional[str]:
        try:
            with open(self._key_store(), "r", encoding="utf-8") as f:
                saved = json.load(f)
            key = saved.get(self.db.account)
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        if key and self._validate_key(key):
            return key
        return None


    def _persist_key(self, aes_key: str) -> None:
        try:
            with open(self._key_store(), "r", encoding="utf-8") as f:
                saved = json.load(f)
        except (OSError, ValueError, json.JSONDecodeError):
            saved = {}
        saved[self.db.account] = aes_key
        try:
            os.makedirs(os.path.dirname(self._key_store()), exist_ok=True)
            with open(self._key_store(), "w", encoding="utf-8") as f:
                json.dump(saved, f, indent=2)
        except OSError:
            pass


    def _collect_templates(self, limit: int = 32, keep: int = 16) -> List[str]:
        """递归收集 *_t.dat 缩略图模板: 按修改时间降序取前 keep 个"""
        base = os.path.join(self.db.account_dir, "msg", "attach")
        hits = glob.glob(os.path.join(base, "*", "*", "Img", "*_t.dat"))
        hits.sort(key=os.path.getmtime, reverse=True)
        return hits[:keep]


    def _get_xor_key(self, templates: List[str]) -> Optional[int]:
        """文件尾统计推 XOR 密钥: 缩略图明文为 JPEG, 尾部固定 FF D9。

        读每个模板最后 2 字节 (x, y), 统计出现最多的组合;
        xorKey = x ^ 0xFF 且校验 y ^ 0xD9 == xorKey 才返回。
        """
        tails: Dict[Tuple[int, int], int] = {}
        for p in templates:
            try:
                with open(p, "rb") as f:
                    f.seek(-2, 2)
                    tail = f.read(2)
            except OSError:
                continue
            if len(tail) == 2:
                tails[(tail[0], tail[1])] = tails.get((tail[0], tail[1]), 0) + 1
        for (x, y), _ in sorted(tails.items(), key=lambda kv: -kv[1]):
            key = x ^ 0xFF
            if y ^ 0xD9 == key:
                return key
        return None


    def _derive_xor_key(self, dat_path: str) -> int:
        """从同图缩略图 <md5>_t.dat 尾部 FF D9 反推单字节 XOR 密钥。

        结束标记允许出现在文件末尾之前（最多回看 ``_FOOTER_MAX`` 字节）——
        Sns 缓存在 FF D9 之后还挂着页脚，只比对末两字节会退化成错误密钥。
        """
        for cand in (
            dat_path[:-4] + "_t.dat",
            dat_path[:-4] + "_h.dat",
            dat_path,
        ):
            if not os.path.exists(cand):
                continue
            try:
                with open(cand, "rb") as f:
                    f.seek(0, 2)
                    back = min(f.tell(), 2 + _FOOTER_MAX)
                    f.seek(-back, 2)
                    tail = f.read()
            except OSError:
                continue
            for off in range(len(tail) - 1):
                key = tail[-2 - off] ^ 0xFF
                if tail[-1 - off] ^ key == 0xD9:
                    return key

        return 0x88


    def _install_xor_key(self) -> int:
        """账号级单字节 XOR 密钥：cfgDword 低字节(权威) → 缩略图统计 → 0x88。

        缓存到 ``self._xor_key``。逐文件猜尾部只作兜底，因为带页脚的容器
        会让尾部判据失效。
        """
        if self._xor_key is not None:
            return self._xor_key
        derived = self._derive_cfg_key()
        if derived:
            self._xor_key = derived[1]
        else:
            self._xor_key = self._get_xor_key(self._collect_templates())
        if self._xor_key is None:
            self._xor_key = 0x88
        return self._xor_key


    def _scan_aes_key(self, monitor: bool = False,
                      monitor_timeout: float = 120.0) -> Optional[str]:
        """扫描 Weixin.exe 内存穷举候选密钥, AES-ECB 解密探针验证。

        候选两类模式(YARA 思路):
        - ASCII: 非字母数字 + 连续 32 个 [a-zA-Z0-9] + 非字母数字结尾,
          每候选取前 16 字节作 AES-128 key 解密探针, 明文为 JPEG SOI 命中;
        - UTF-16LE: 字母数字与 0x00 交错形式。
        """
        probe = self._probe_ct()
        if not probe:
            return None
        pids = self.db._find_weixin_pids()
        if not pids:
            return None
        from . import db as _dbmod
        k32 = _dbmod._k32
        MBI = _dbmod._MBI

        ASCII32_RE = re.compile(rb"[^a-zA-Z0-9]([a-zA-Z0-9]{32})[^a-zA-Z0-9]")
        U16_RE = re.compile(rb"(?:[a-zA-Z0-9]\x00){32}")

        def read_mem(h, addr: int, n: int):
            buf = ctypes.create_string_buffer(n)
            br = ctypes.c_size_t(0)
            if k32.ReadProcessMemory(h, ctypes.c_void_p(addr), buf, n, ctypes.byref(br)) and br.value:
                return buf.raw[: br.value]
            return None

        def _try_key(key16: bytes) -> bool:
            try:
                from cryptography.hazmat.primitives.ciphers import (
                    Cipher, algorithms, modes,
                )
                pt = Cipher(algorithms.AES(key16), modes.ECB()).decryptor()
                out = pt.update(probe) + pt.finalize()
            except Exception as exc:
                wxlog.debug(f'图片密钥试解失败：{exc!r}')
                return False
            return out[:3] == b"\xff\xd8\xff" or _jpeg_like(out)

        def _candidates(buf: bytes):
            for m in ASCII32_RE.finditer(buf):
                yield m.group(1)[:16].encode() if isinstance(m.group(1), str) else m.group(1)[:16]
            for m in U16_RE.finditer(buf):
                yield bytes(b for i, b in enumerate(m.group()) if i % 2 == 0)[:16]

        def _scan_once() -> Optional[str]:
            # 保持微信进程原顺序扫描(主进程靠前, 命中率高)
            for pid in pids:
                h = k32.OpenProcess(0x0010 | 0x0400, False, pid)
                if not h:
                    continue
                try:
                    addr = 0
                    while True:
                        mbi = MBI()
                        r = k32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi))
                        if r == 0:
                            break
                        if (
                            mbi.State == 0x1000
                            and (mbi.Protect & 0xFF) & 0xE6
                            and not (mbi.Protect & 0x100)
                            and 0 < mbi.RegionSize < 0x2000000
                        ):
                            buf = read_mem(h, mbi.BaseAddress or 0, mbi.RegionSize)
                            if buf:
                                for key in _candidates(buf):
                                    if _try_key(key):
                                        return key.decode("ascii", "replace")
                        addr = (mbi.BaseAddress or 0) + mbi.RegionSize
                finally:
                    k32.CloseHandle(h)
            return None

        return _scan_once()


    def decrypt_image(self, dat_path: str, aes_key: Optional[str] = None,
                      xor_key: Optional[int] = None) -> bytes:
        """解密单个 .dat 为图片字节（自动识别 v1/v2 格式）"""
        with open(dat_path, "rb") as f:
            data = f.read()
        if not data:
            raise ValueError("空文件: %s" % dat_path)
        magic = data[:6]
        if magic == V2_MAGIC:
            return self._decrypt_v2(data, dat_path, aes_key, xor_key)
        if magic == V1_MAGIC:
            if xor_key is None:
                xor_key = self._derive_xor_key(dat_path)
            key = data[6:22]
            body = data[22:]
            return bytes(b ^ (xor_key & 0xFF) for b in body)
        # 早期纯异或格式：逐字节 ^ 0xFF（无签名），按 JPEG/PNG 魔数回退判断
        for cand in dict.fromkeys((data[0] ^ 0xff, data[0] ^ 0x89, data[0] ^ 0x47, data[0] ^ 0x52, 0x88, 0x30, 0xff, 0xe9)):
            out = bytes(b ^ cand for b in data)
            if out[:3] == b"\xff\xd8\xff" or out[:4] == b"\x89PNG":
                return out
        raise ValueError("无法识别的图片加密格式: %s" % dat_path)


    def _resolve_aes_key(self) -> Optional[str]:
        """统一密钥解析：显式注入 → cfgDword派生 → 本地缓存 → 内存扫描"""
        if self._image_key and self._validate_key(self._image_key):
            return self._image_key
        # 尝试cfgDword派生
        derived = self._derive_cfg_key()
        if derived:
            return derived[0]
        cached = self._load_persisted_key()
        if cached:
            return cached
        key = self._scan_aes_key(monitor=False)
        if key:
            self._persist_key(key)
        return key


    def _decrypt_v2(self, data: bytes, dat_path: str,
                    aes_key: Optional[str], xor_key: Optional[int]) -> bytes:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        aes_size, xor_size = struct.unpack_from("<LL", data, 6)
        if xor_key is None:
            xor_key = self._install_xor_key()
        if aes_key is None:
            aes_key = self._resolve_aes_key()
            if not aes_key:
                raise RuntimeError(
                    "无法获取图片 AES 密钥：请保持微信登录，并先在微信聊天中"
                    "打开（点击查看大图）任意一张图片，再重试 detect_image_key()；"
                    "或通过 MediaDownloader(image_key='...') 手动传入密钥。"
                )
        aes_blk = aligned_aes_block_size(aes_size)
        off = 15
        aes_data = data[off: off + aes_blk]
        off += aes_blk
        raw_data = data[off: len(data) - xor_size]
        xor_data = data[len(data) - xor_size:]
        dec = Cipher(algorithms.AES(aes_key.encode()), modes.ECB()).decryptor()
        pt = dec.update(aes_data) + dec.finalize()
        pad = pt[-1] if pt else 0
        if 1 <= pad <= 16 and all(b == pad for b in pt[-pad:]):
            pt = pt[:-pad]
        out = pt + raw_data + bytes(b ^ (xor_key & 0xFF) for b in xor_data)
        out, footer = strip_container_footer(out)
        if footer:
            wxlog.debug("V2 尾部 %d 字节页脚已按结束标记裁剪: %s"
                        % (footer, dat_path))
        return out
