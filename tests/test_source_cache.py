import hashlib
import io
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from PIL import Image

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from source_cache import SourceCache
from wechat_source import Source


class SourceCacheTests(unittest.TestCase):
    def setUp(self):
        folder=ROOT/"work"/"test-data"
        folder.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=folder)
        self.path=Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def source(self):
        source=Source.__new__(Source)
        source.data_dir=self.path/"meta"
        source.account="test"
        source.db=SimpleNamespace(account_dir=str(self.path/"wechat"),_find_weixin_pids=Mock(side_effect=AssertionError("No scanning")))
        return source

    def test_repeated_undecodable_sticker_reads_once_across_runs_and_retries_if_changed(self):
        digest="a"*32
        path=self.path/"wechat"/"business"/"emoticon"/"Persist"/digest
        path.parent.mkdir(parents=True)
        path.write_bytes(b"\x11"*64)
        meta={"md5":digest,"aeskey":"00"*16}
        reads=[]
        original=Path.read_bytes
        def read(file):
            if file==path:
                reads.append(file)
            return original(file)
        with patch.object(Path,"read_bytes",read):
            source=self.source()
            for _ in range(25):
                with self.assertRaisesRegex(RuntimeError,"暂不支持"):
                    source.emoji_asset("chosen",1,"part0",meta)
            self.assertEqual(len(reads),1)
            other=self.source()
            with self.assertRaises(RuntimeError):
                other.emoji_asset("chosen",2,"part0",meta)
            self.assertEqual(len(reads),1)
            retry=self.source()
            retry.retry_media_failures()
            for _ in range(2):
                with self.assertRaises(RuntimeError):
                    retry.emoji_asset("chosen",2,"part0",meta)
            self.assertEqual(len(reads),2)
            output=io.BytesIO()
            Image.new("RGB",(20,20),"red").save(output,format="PNG")
            path.write_bytes(output.getvalue())
            self.assertEqual(other.emoji_asset("chosen",3,"part0",meta)["bytes"],output.getvalue())
            self.assertEqual(len(reads),3)
            source.db._find_weixin_pids.assert_not_called()

    def test_file_inventory_reuses_unchanged_directories_and_notices_new_files(self):
        folder=self.path/"files"/"nested"
        folder.mkdir(parents=True)
        first=folder/"first.dat"
        first.write_bytes(b"first")
        cache=SourceCache(self.path/"meta")
        self.assertEqual(list(cache.files(folder)),[first])
        cached=SourceCache(self.path/"meta")
        with patch("source_cache.os.scandir",side_effect=AssertionError("Directory should be cached")):
            self.assertEqual(list(cached.files(folder)),[first])
        old=folder.stat().st_mtime_ns
        second=folder/"second.dat"
        second.write_bytes(b"second")
        os.utime(folder,ns=(old+1000000000,old+1000000000))
        self.assertEqual(set(cached.files(folder)),{first,second})


if __name__=="__main__":
    unittest.main()
