"""常驻生命周期（S5.5）：守护日志的定性。

判错的代价很实在：2026-09-19 搬家断电，仪表盘把"系统关机"报成「崩溃 2 次」，
白吓一跳。这里的样本就是那天的真实日志行（原样抄的）。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from xiaoyu.lifecycle import (
    DLL_INIT_FAILED_CODE,
    VERDICT_CLEAN,
    VERDICT_CRASH,
    VERDICT_SYSTEM,
    GuardianLog,
    judge_exit,
    parse_guardian_log,
    read_guardian_log,
)

# 2026-09-19 搬家断电那天的真日志（老格式，没有「判定=」）：
# 16:19:51 系统关机把本体终结，16:19:56 守护去重启、机器已经在关机流程里起不来，
# 16:41:21 开机自启。两条 exited 全是关机的正常现象。
REAL_OUTAGE_LOG = """\
[2026-09-18 20:21:42] guardian: starting | 守护 pid=10536 | 本体 = python.exe -m xiaoyu
[2026-09-19 16:19:51] guardian: exited | code=1073807364 | 活了一轮 71892 秒｜5 秒后重启
[2026-09-19 16:19:56] guardian: exited | code=3221225794 | 活了一轮 0 秒｜5 秒后重启
[2026-09-19 16:41:21] guardian: starting | 守护 pid=10516 | 本体 = python.exe -m xiaoyu
"""


class JudgeExitTest(unittest.TestCase):
    def test_system_shutdown_kill_is_not_a_crash_and_no_restart(self):
        """0x40010004 = 系统关机时把进程终结掉 —— 不是崩，重启也是白起。"""
        result = judge_exit(0x40010004)
        self.assertEqual(result.verdict, VERDICT_SYSTEM)
        self.assertFalse(result.crash)
        self.assertFalse(result.restart)

    def test_ctrl_c_is_not_a_crash_but_still_restarts(self):
        """有人按了 Ctrl+C：不算崩，但常驻设备必须爬起来接着干。"""
        result = judge_exit(0xC000013A)
        self.assertEqual(result.verdict, VERDICT_SYSTEM)
        self.assertFalse(result.crash)
        self.assertTrue(result.restart)

    def test_shutting_down_flag_wins_over_the_code(self):
        """系统正在关机：哪怕退出码是 0，也别再重启（起了也是白起）。"""
        result = judge_exit(0, shutting_down=True)
        self.assertFalse(result.crash)
        self.assertFalse(result.restart)

    def test_real_crash_is_counted_and_restarted(self):
        result = judge_exit(1)
        self.assertEqual(result.verdict, VERDICT_CRASH)
        self.assertTrue(result.crash)
        self.assertTrue(result.restart)

    def test_clean_exit_is_restarted_but_not_a_crash(self):
        result = judge_exit(0)
        self.assertEqual(result.verdict, VERDICT_CLEAN)
        self.assertFalse(result.crash)
        self.assertTrue(result.restart)


class ParseGuardianLogTest(unittest.TestCase):
    def test_outage_log_counts_as_system_stop_not_crash(self):
        """2026-09-19 搬家断电：两条 exited 都不算崩溃。"""
        summary = parse_guardian_log(REAL_OUTAGE_LOG)
        self.assertEqual(summary.starts, 2)
        self.assertEqual(summary.crashes, 0)
        self.assertEqual(summary.system_stops, 2)

    def test_new_format_verdict_is_trusted(self):
        """新格式里有「判定=」，直接信它，不再按码倒推。"""
        summary = parse_guardian_log(
            "[2026-09-19 18:00:00] guardian: exited | code=1073807364 | "
            "活了一轮 10 秒｜判定=随系统退出｜系统要关机了，守护一起收工\n"
            "[2026-09-19 19:00:00] guardian: exited | code=1 | "
            "活了一轮 3 秒｜判定=崩溃｜5 秒后重启\n"
        )
        self.assertEqual(summary.crashes, 1)
        self.assertEqual(summary.system_stops, 1)

    def test_lone_dll_init_failure_is_a_crash(self):
        """0xC0000142 前面几分钟都好好的 -> 真起不来了，得报。"""
        summary = parse_guardian_log(
            "[2026-09-19 10:00:00] guardian: exited | code=3221225794 | "
            "活了一轮 0 秒｜5 秒后重启\n"
        )
        self.assertEqual(summary.crashes, 1)
        self.assertEqual(summary.system_stops, 0)

    def test_dll_init_failure_long_after_shutdown_is_a_crash(self):
        """关机退出之后隔了 10 分钟才 DLL 失败，跟关机没关系，算崩。"""
        summary = parse_guardian_log(
            "[2026-09-19 16:19:51] guardian: exited | code=1073807364 | 活了一轮 71892 秒\n"
            "[2026-09-19 16:29:51] guardian: exited | code=3221225794 | 活了一轮 0 秒\n"
        )
        self.assertEqual(summary.crashes, 1)
        self.assertEqual(summary.system_stops, 1)

    def test_dll_init_failure_within_grace_of_shutdown_is_system(self):
        summary = parse_guardian_log(
            "[2026-09-19 16:19:51] guardian: exited | code=1073807364 | 活了一轮 71892 秒\n"
            "[2026-09-19 16:19:56] guardian: exited | code=3221225794 | 活了一轮 0 秒\n"
        )
        self.assertEqual(summary.crashes, 0)
        self.assertEqual(summary.system_stops, 2)

    def test_starts_alone_are_not_crashes(self):
        summary = parse_guardian_log(
            "[2026-09-19 16:41:21] guardian: starting | 守护 pid=10516\n"
        )
        self.assertEqual(summary.starts, 1)
        self.assertEqual(summary.crashes, 0)
        self.assertEqual(summary.system_stops, 0)

    def test_empty_log_is_all_zeros(self):
        self.assertEqual(parse_guardian_log(""), GuardianLog(0, 0, 0))

    def test_broken_timestamp_does_not_crash_the_parser(self):
        summary = parse_guardian_log("guardian: exited | code=1 | 活了一轮 3 秒\n")
        self.assertEqual(summary.crashes, 1)


class ReadGuardianLogTest(unittest.TestCase):
    def test_missing_file_is_all_zeros(self):
        with tempfile.TemporaryDirectory() as tmp:
            summary = read_guardian_log(Path(tmp) / "guardian.log")
        self.assertEqual(summary, GuardianLog(0, 0, 0))

    def test_reads_a_real_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "guardian.log"
            path.write_text(REAL_OUTAGE_LOG, encoding="utf-8")
            summary = read_guardian_log(path)
        self.assertEqual(summary.starts, 2)
        self.assertEqual(summary.crashes, 0)
        self.assertEqual(summary.system_stops, 2)


if __name__ == "__main__":
    unittest.main()