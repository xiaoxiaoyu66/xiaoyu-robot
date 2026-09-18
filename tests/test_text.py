"""分句逻辑 + 时间感的测试。

这一层不依赖任何第三方库，所以可以直接跑：
    python -m unittest discover tests -v
"""

from __future__ import annotations

import unittest
from datetime import datetime

from xiaoyu.text import describe_last_seen, describe_now, parse_timestamp, split_sentences


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


class TestParseTimestamp(unittest.TestCase):
    def test_parses_the_stores_format(self):
        self.assertEqual(
            parse_timestamp("2026-09-17 21:30:00"),
            datetime(2026, 9, 17, 21, 30, 0),
        )

    def test_accepts_shorter_forms(self):
        self.assertEqual(parse_timestamp("2026-09-17 21:30"), datetime(2026, 9, 17, 21, 30))
        self.assertEqual(parse_timestamp("2026-09-17"), datetime(2026, 9, 17))

    def test_garbage_returns_none_instead_of_raising(self):
        """库里的时间戳坏了，只能少一句时间感，不能让整轮对话崩掉。"""
        for bad in (None, "", "   ", "昨天", "2026/09/17", "2026-13-45 99:99:99"):
            self.assertIsNone(parse_timestamp(bad), f"{bad!r} 应该解析失败")


class TestDescribeNow(unittest.TestCase):
    def test_says_date_weekday_and_clock(self):
        self.assertEqual(
            describe_now(datetime(2026, 9, 18, 14, 19)),
            "2026-09-18 周五 14:19",
        )


class TestDescribeLastSeen(unittest.TestCase):
    """机器人要说出"上次你不是说在忙作业吗"，全靠这几个字符串。"""

    NOW = datetime(2026, 9, 18, 14, 19)

    def test_yesterday(self):
        self.assertEqual(describe_last_seen("2026-09-17 21:30:00", self.NOW), "昨天 21:30")

    def test_days_ago(self):
        self.assertEqual(describe_last_seen("2026-09-15 09:00:00", self.NOW), "3 天前")

    def test_weeks_ago(self):
        self.assertEqual(describe_last_seen("2026-09-01 09:00:00", self.NOW), "大概 2 周前")

    def test_long_ago_falls_back_to_a_date(self):
        self.assertEqual(describe_last_seen("2026-05-01 09:00:00", self.NOW), "2026-05-01")

    def test_today_gets_the_clock(self):
        self.assertEqual(describe_last_seen("2026-09-18 09:05:00", self.NOW), "今天 09:05")

    def test_very_recent_is_not_reported_as_a_clock(self):
        """"今天 14:18" 这种说法很奇怪，差几分钟就直说。"""
        self.assertEqual(describe_last_seen("2026-09-18 14:18:00", self.NOW), "刚才")
        self.assertEqual(describe_last_seen("2026-09-18 13:50:00", self.NOW), "29 分钟前")

    def test_future_timestamp_is_not_guessed(self):
        """系统时钟被改过时，不猜"负几天"，原样说。"""
        self.assertEqual(
            describe_last_seen("2026-09-19 08:00:00", self.NOW), "2026-09-19 08:00"
        )

    def test_bad_input_gives_empty_string(self):
        for bad in (None, "", "不是时间"):
            self.assertEqual(describe_last_seen(bad, self.NOW), "")


if __name__ == "__main__":
    unittest.main()