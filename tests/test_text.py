"""分句逻辑的测试。

这一层不依赖任何第三方库，所以可以直接跑：
    python -m unittest discover tests -v
"""

from __future__ import annotations

import unittest

from xiaoyu.text import split_sentences


class TestSplitSentences(unittest.TestCase):
    def test_single_sentence(self):
        sentences, rest = split_sentences("你好。")
        self.assertEqual(sentences, ["你好。"])
        self.assertEqual(rest, "")

    def test_multiple_sentences(self):
        sentences, rest = split_sentences("你好。今天天气不错！你呢？")
        self.assertEqual(sentences, ["你好。", "今天天气不错！", "你呢？"])
        self.assertEqual(rest, "")

    def test_incomplete_tail_is_kept(self):
        sentences, rest = split_sentences("你好。我还没说完")
        self.assertEqual(sentences, ["你好。"])
        self.assertEqual(rest, "我还没说完")

    def test_no_punctuation_stays_in_buffer(self):
        sentences, rest = split_sentences("完全没标点")
        self.assertEqual(sentences, [])
        self.assertEqual(rest, "完全没标点")

    def test_empty(self):
        self.assertEqual(split_sentences(""), ([], ""))

    def test_newline_is_a_boundary(self):
        sentences, rest = split_sentences("第一行\n第二行\n")
        self.assertEqual(sentences, ["第一行", "第二行"])
        self.assertEqual(rest, "")

    def test_splits_at_soft_break_when_too_long(self):
        """太长时应该在逗号处断开，而不是一路攒着。"""
        text = "一二三四五六七八九十" * 2 + "，尾巴"
        sentences, rest = split_sentences(text, max_buffer=20)
        self.assertEqual(len(sentences), 1)
        self.assertTrue(sentences[0].endswith("，"), f"应该在逗号处切开：{sentences[0]!r}")
        self.assertEqual(rest, "尾巴")

    def test_force_cut_when_no_break_at_all(self):
        """一个标点都没有时也必须切，不能让缓冲无限累积（延迟兜底）。"""
        text = "无标点" * 40          # 120 个字，没有任何标点
        sentences, rest = split_sentences(text, max_buffer=20)
        self.assertTrue(sentences, "长文本必须切出至少一句，否则首句永远出不来")
        self.assertLessEqual(len(rest), 30, f"缓冲区不该继续膨胀，当前 {len(rest)} 字")

    def test_buffer_never_exceeds_limit(self):
        """模拟流式逐字输入：无论吐什么都不该让缓冲无限长。"""
        buffer = ""
        produced = 0
        for _ in range(300):
            buffer += "字"
            sentences, buffer = split_sentences(buffer, max_buffer=20)
            produced += len(sentences)
        self.assertGreater(produced, 0)
        self.assertLessEqual(len(buffer), 30)


if __name__ == "__main__":
    unittest.main()