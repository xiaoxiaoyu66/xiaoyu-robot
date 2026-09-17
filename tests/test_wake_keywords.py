"""唤醒词相关的测试。

唤醒词写错会让程序在 C++ 层直接崩掉（抓不到 Python 异常），
所以这里的校验必须可靠。这些测试都不需要真的加载模型。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from xiaoyu.wake.models import load_vocab, pick_model_triple, prepare_keywords_file

try:
    from scripts.make_keywords import to_tokens
except ImportError:
    to_tokens = None


def _make_fake_model_dir(root: Path, with_int8: bool = True) -> Path:
    """造一个和官方压缩包一样结构的假目录。"""
    d = root / "kws"
    d.mkdir(parents=True, exist_ok=True)
    for chunk in ("16", "8"):
        d.joinpath(f"encoder-epoch-13-avg-2-chunk-{chunk}-left-64.onnx").write_bytes(b"")
        d.joinpath(f"decoder-epoch-13-avg-2-chunk-{chunk}-left-64.onnx").write_bytes(b"")
        d.joinpath(f"joiner-epoch-13-avg-2-chunk-{chunk}-left-64.onnx").write_bytes(b"")
        if with_int8:
            # 故意只给 int8 的 encoder / joiner，不给 decoder —— 和官方包一模一样
            d.joinpath(f"encoder-epoch-13-avg-2-chunk-{chunk}-left-64.int8.onnx").write_bytes(b"")
            d.joinpath(f"joiner-epoch-13-avg-2-chunk-{chunk}-left-64.int8.onnx").write_bytes(b"")
    d.joinpath("tokens.txt").write_text("x 1\niǎo 2\ny 3\nǔ 4\n", encoding="utf-8")
    return d


class TestPickModelTriple(unittest.TestCase):
    def test_picks_a_complete_matching_set(self):
        """必须挑到三件齐全、后缀一致的一组，不能混搭 int8 和非 int8。"""
        with tempfile.TemporaryDirectory() as tmp:
            d = _make_fake_model_dir(Path(tmp))
            encoder, decoder, joiner = pick_model_triple(d)

            suffix = encoder.name[len("encoder") :]
            self.assertEqual(decoder.name, "decoder" + suffix)
            self.assertEqual(joiner.name, "joiner" + suffix)

    def test_prefers_int8_when_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = _make_fake_model_dir(Path(tmp), with_int8=False)
            encoder, _, _ = pick_model_triple(d)
            self.assertNotIn(".int8.", encoder.name)

    def test_raises_when_nothing_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "kws"
            d.mkdir()
            d.joinpath("encoder-only.onnx").write_bytes(b"")
            with self.assertRaises(FileNotFoundError):
                pick_model_triple(d)


class TestPrepareKeywordsFile(unittest.TestCase):
    def setUp(self):
        self.vocab = {"x", "iǎo", "y", "ǔ"}

    def test_accepts_pinyin_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "keywords.txt"
            src.write_text("x iǎo y ǔ :2.5 #0.5 @小宇\n", encoding="utf-8")
            cleaned, names = prepare_keywords_file(src, self.vocab, Path(tmp) / "out")
            self.assertEqual(names, ["小宇"])
            self.assertTrue(cleaned.exists())
            self.assertIn("x iǎo y ǔ", cleaned.read_text(encoding="utf-8"))

    def test_rejects_chinese_characters_with_clear_error(self):
        """写汉字必须给出人能看懂的报错，而不是让 C++ 层崩掉。"""
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "keywords.txt"
            src.write_text("小宇 :2.5 #0.5 @小宇\n", encoding="utf-8")
            with self.assertRaises(ValueError) as ctx:
                prepare_keywords_file(src, self.vocab, Path(tmp) / "out")
            message = str(ctx.exception)
            self.assertIn("小宇", message)
            self.assertIn("make_keywords.py", message)

    def test_skips_blank_and_comment_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "keywords.txt"
            src.write_text(
                "# 这是注释\n\nx iǎo y ǔ :2.5 #0.5 @小宇\n\n", encoding="utf-8"
            )
            cleaned, names = prepare_keywords_file(src, self.vocab, Path(tmp) / "out")
            self.assertEqual(names, ["小宇"])
            self.assertEqual(len(cleaned.read_text(encoding="utf-8").strip().splitlines()), 1)

    def test_missing_file_is_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                prepare_keywords_file(Path(tmp) / "nope.txt", self.vocab, Path(tmp) / "out")

    def test_load_vocab(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "tokens.txt"
            f.write_text("<blk> 0\nx 179\niǎo 124\n", encoding="utf-8")
            self.assertEqual(load_vocab(f), {"<blk>", "x", "iǎo"})


@unittest.skipIf(to_tokens is None, "未安装 pypinyin，跳过")
class TestPinyinConversion(unittest.TestCase):
    def test_xiaoyu(self):
        self.assertEqual(to_tokens("小宇"), ["x", "iǎo", "y", "ǔ"])

    def test_erwa(self):
        self.assertEqual(to_tokens("二娃"), ["èr", "w", "á"])

    def test_real_model_vocab_covers_default_wake_word(self):
        """默认唤醒词的每个音素都必须真的出现在模型词表里。"""
        vocab_file = Path(__file__).resolve().parent.parent / "models" / "kws" / "tokens.txt"
        if not vocab_file.exists():
            self.skipTest("还没下载唤醒模型")
        vocab = load_vocab(vocab_file)
        for token in to_tokens("小宇"):
            self.assertIn(token, vocab, f"音素 {token} 不在模型词表里")


if __name__ == "__main__":
    unittest.main()