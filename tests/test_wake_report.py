"""--wake-report 的统计逻辑（S5.5）：按天 + 凌晨分开数。

跑法：python -m unittest discover tests
只测纯函数 count_wake_events（读日志 + 计数），不碰配置、不碰真实 logs/。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from xiaoyu.app import count_wake_events

# 一份真格式的日志样本：两行"听到唤醒词"是噪声，四行 WAKE_EVENT 才是要数的。
LOG = """\
2026-09-18 14:03:11.220 | INFO | xiaoyu.wake.kws | 听到唤醒词：小柚子
2026-09-18 14:03:11.240 | INFO | xiaoyu.wake.kws | WAKE_EVENT | ts=2026-09-18 14:03:11 | keyword=小柚子
2026-09-18 14:09:02.100 | INFO | xiaoyu.wake.kws | WAKE_EVENT | ts=2026-09-18 14:09:02 | keyword=小柚子
2026-09-18 14:09:02.120 | INFO | xiaoyu.wake.kws | 听到唤醒词：小柚子
2026-09-19 03:41:55.900 | INFO | xiaoyu.wake.kws | WAKE_EVENT | ts=2026-09-19 03:41:55 | keyword=小柚子
2026-09-19 23:12:00.000 | INFO | xiaoyu.wake.kws | WAKE_EVENT | ts=2026-09-19 23:12:00 | keyword=小柚子
"""


class CountWakeEventsTest(unittest.TestCase):
    def _count(self, text):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "xiaoyu_2026-09-18.log").write_text(text, encoding="utf-8")
            return count_wake_events(Path(tmp))

    def test_counts_per_day_and_night(self):
        per_day, night = self._count(LOG)
        self.assertEqual(per_day["2026-09-18"], 2)
        self.assertEqual(per_day["2026-09-19"], 2)
        self.assertEqual(night["2026-09-18"], 0)
        self.assertEqual(night["2026-09-19"], 1)   # 只有 03:41 那次算凌晨

    def test_bare_wake_line_is_not_double_counted(self):
        """只认 WAKE_EVENT 行；"听到唤醒词"那行是同一次唤醒的第二条日志。"""
        per_day, night = self._count(
            "2026-09-18 14:03:11.220 | INFO | xiaoyu.wake.kws | 听到唤醒词：小柚子\n"
        )
        self.assertEqual(sum(per_day.values()), 0)
        self.assertEqual(sum(night.values()), 0)

    def test_midnight_boundary(self):
        per_day, night = self._count(
            "WAKE_EVENT | ts=2026-09-18 05:59:59 | keyword=小柚子\n"
            "WAKE_EVENT | ts=2026-09-18 06:00:00 | keyword=小柚子\n"
        )
        self.assertEqual(per_day["2026-09-18"], 2)
        self.assertEqual(night["2026-09-18"], 1)   # 06:00 已经不算凌晨

    def test_missing_directory_is_empty_not_a_crash(self):
        per_day, night = count_wake_events(
            Path(tempfile.gettempdir()) / "xiaoyu-no-such-dir-xyz"
        )
        self.assertEqual(sum(per_day.values()), 0)
        self.assertEqual(sum(night.values()), 0)


if __name__ == "__main__":
    unittest.main()
