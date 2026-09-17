"""编码兜底的测试。

这几条都是**实测踩出来的坑**，不是想当然：

    1. 从管道、文件或系统 API 拿到的字符串里可能出现"孤立代理字符"
       （解码失败留下的残渣，长得像 '\udc80'）。这种东西一旦流进日志落盘，
       会抛 UnicodeEncodeError —— 后果不只是少一行日志，
       而是整个日志系统开始往控制台喷堆栈，最该用的功能反而最先瘫。
    2. 中文 Windows 上，记事本和不少编辑器"另存为 ANSI"存出来的是 GBK。
       config/persona.md、keywords.txt 被存成 GBK 太常见了，
       用 utf-8 硬读会直接抛 UnicodeDecodeError，报错信息对新手完全看不懂。

零依赖可跑：
    python -m unittest discover tests -v
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from xiaoyu.text import read_text, sanitize


class TestSanitize(unittest.TestCase):
    def test_normal_text_untouched(self):
        text = "正常文本 with ascii 混着"
        self.assertEqual(sanitize(text), text)

    def test_surrogate_becomes_readable_escape(self):
        cleaned = sanitize("前面\udc80后面")
        self.assertIn(r"\udc80", cleaned, "坏字符应该转成可读的转义写法")
        self.assertIn("前面", cleaned)
        self.assertIn("后面", cleaned)

    def test_result_is_always_encodable(self):
        """核心保证：sanitize 之后必须一定能编码成 utf-8。"""
        for bad in ("\ud800", "\udfff", "\udc80\udcba", "abc\ud800def\udc00"):
            sanitize(bad).encode("utf-8")

    def test_normal_multibyte_survives(self):
        """别把正常的多字节字符一起搞坏了。"""
        self.assertEqual(sanitize("好耶 🎉，中文标点：。"), "好耶 🎉，中文标点：。")


class TestReadText(unittest.TestCase):
    def _write(self, tmp: str, name: str, data: bytes) -> Path:
        path = Path(tmp) / name
        path.write_bytes(data)
        return path

    def test_utf8(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, "a.md", "你好，小宇。\n".encode("utf-8"))
            self.assertEqual(read_text(path), "你好，小宇。\n")

    def test_utf8_with_bom(self):
        """记事本"另存为 UTF-8"会加 BOM，BOM 必须被吃掉，不能跑到正文里。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, "b.md", "你好".encode("utf-8-sig"))
            self.assertEqual(read_text(path), "你好")

    def test_gbk(self):
        """记事本"另存为 ANSI"存出来的就是 GBK。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, "c.md", "你好，小宇。".encode("gbk"))
            self.assertEqual(read_text(path), "你好，小宇。")

    def test_utf16(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, "d.md", "你好".encode("utf-16"))
            self.assertEqual(read_text(path), "你好")

    def test_undecodable_bytes_do_not_raise(self):
        """真的是垃圾字节时，宁可丢几个字符，也不能让程序起不来。"""
        with tempfile.TemporaryDirectory() as tmp:
            for name, data in (("e1.bin", b"\xff\xff\xff"), ("e2.bin", b"\xff\xfe\x41")):
                read_text(self._write(tmp, name, data))     # 不抛异常即通过

    def test_missing_file_raises_oserror(self):
        missing = Path(tempfile.gettempdir()) / "xiaoyu_绝对不存在的文件_9f3a.txt"
        with self.assertRaises(OSError):
            read_text(missing)


class TestLoggerSurvivesBadBytes(unittest.TestCase):
    """日志系统不能被一个坏字符搞瘫 —— 这是项目的硬要求。"""

    def setUp(self):
        try:
            from loguru import logger
        except ImportError:                                   # pragma: no cover
            self.skipTest("未安装 loguru")
        self.logger = logger

    def tearDown(self):
        self.logger.remove()

    def test_redact_handles_surrogates(self):
        from xiaoyu.logger import _redact

        _redact("坏字符 \udc80 混在里面").encode("utf-8")

    def test_log_file_survives_surrogates(self):
        """端到端：走真实的 setup_logging，往日志里塞坏字符，文件必须写成功。

        修之前的实际表现：loguru 的文件 sink 抛 UnicodeEncodeError，
        控制台刷出一大段堆栈，而且那条日志彻底丢了。
        """
        from xiaoyu import logger as xiaoyu_logger

        with tempfile.TemporaryDirectory() as tmp:
            # setup_logging 有"只生效一次"的保护，这里手动放开，
            # 保证本测试无论顺序如何都真的重新配置了日志。
            xiaoyu_logger._configured = False
            xiaoyu_logger.setup_logging(level="DEBUG", log_dir=tmp, console=False)
            try:
                log = xiaoyu_logger.get_logger("tests.encoding")
                log.info("坏字符 {}", "前\udc80后")
                log.warning("裸的 \udcba 也来一条")
                log.error("密钥也不许落盘 sk-" + "a" * 24)
            finally:
                self.logger.remove()          # enqueue=True，remove 会等队列写完

            log_files = list(Path(tmp).glob("xiaoyu_*.log"))
            self.assertTrue(log_files, "日志文件没生成")

            content = log_files[0].read_text(encoding="utf-8")
            self.assertIn("坏字符", content, "整条日志不该因为一个坏字符被丢掉")
            self.assertIn(r"\udc80", content, "坏字符应该被转义写进去")
            self.assertNotIn("a" * 24, content, "密钥必须被脱敏")
            self.assertIn("<已脱敏>", content)


if __name__ == "__main__":
    unittest.main()