"""常驻生命周期（S5.5）：守护日志怎么记、怎么读，以及"什么才算崩了"。

为什么单独一个模块：
    守护（scripts/guardian.py）负责写，仪表盘（scripts/install_autostart.py --status）
    负责读，"崩溃"只能有一个答案，所以判定只写在这里一份，两边共用。

踩过的坑（2026-09-19 搬家断电实测）：
    断电后开机自启一切正常，仪表盘却报「本体中途崩溃次数：2」。
    真相是系统关机时把本体终结了（0x40010004），守护 5 秒后又去重启，
    而那个瞬间机器已经在关机流程里，python 连 DLL 都初始化不了（0xC0000142）。
    两条都是"关机"的正常现象，一条都不是崩溃 —— 判断要按退出码来；
    更要在守护那一侧就拦住：关机时安静收工，别再制造假的"崩溃"。

只依赖标准库：scripts/ 是直接跑的脚本，不该为了读一行日志把整个业务包拉起来。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

# 系统在关机 / 注销 / 重启时终结进程，用的就是这两个码。
#   0x40010004 DBG_TERMINATE_PROCESS —— 系统要走了，顺手把进程终结掉
#   0xC000013A STATUS_CONTROL_C_EXIT —— Ctrl+C / 窗口被关（关机、注销也会发）
SHUTDOWN_CODES = frozenset({0x40010004, 0xC000013A})

# 关机流程里"5 秒后重启"必然失败：机器已经在关了。所以它只有紧跟在一次系统
# 退出之后才算系统退出；孤零零出现（前面好几分钟都好好的）才是真崩。
DLL_INIT_FAILED_CODE = 0xC0000142
DLL_INIT_GRACE = timedelta(seconds=120)

VERDICT_CRASH = "崩溃"
VERDICT_SYSTEM = "随系统退出"
VERDICT_CLEAN = "正常退出"

_START_MARK = "guardian: starting"
_EXIT_MARK = "guardian: exited"

_CODE_RE = re.compile(r"code=(-?\d+)")
_VERDICT_RE = re.compile(r"判定=([^｜\s]+)")
_TIMESTAMP_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]")


@dataclass(frozen=True)
class ExitVerdict:
    """一次退出怎么定性，顺带决定守护要不要把本体拉起来。"""

    verdict: str
    crash: bool
    restart: bool


@dataclass(frozen=True)
class GuardianLog:
    """守护日志里的三个数。"""

    starts: int
    crashes: int
    system_stops: int


def judge_exit(code: int, shutting_down: bool = False) -> ExitVerdict:
    """退出码 + 系统是否正在关机 -> 怎么定性。

    三种情形分清楚，别混：
        系统退出  机器自己要关机 / 注销，我拦不住也不该对着干（起了也是白起）
        正常退出  本体自己想走的（code 0），不记崩溃，但常驻设备得爬起来
        崩溃      真出事了，要记、要修
    """
    if shutting_down or code == 0x40010004:
        return ExitVerdict(VERDICT_SYSTEM, crash=False, restart=False)
    if code in SHUTDOWN_CODES:
        # 有人让本体停的（Ctrl+C、窗口被关）：不是崩，而且常驻设备必须爬起来
        return ExitVerdict(VERDICT_SYSTEM, crash=False, restart=True)
    if code == 0:
        return ExitVerdict(VERDICT_CLEAN, crash=False, restart=True)
    return ExitVerdict(VERDICT_CRASH, crash=True, restart=True)


def parse_guardian_log(text: str) -> GuardianLog:
    """数一下：守护起来过几次、本体崩过几次、随系统关机退出几次。

    「守护启动次数」不是异常信号（每次开机 / 重登录 +1），
    真正要盯的是「中途崩溃次数」——那才说明有东西坏了。
    """
    starts = crashes = system_stops = 0
    last_system_exit: datetime | None = None

    for line in text.splitlines():
        if _START_MARK in line:
            starts += 1
            continue
        if _EXIT_MARK not in line:
            continue

        stamp = _timestamp_in(line)
        code = _code_in(line)
        verdict = _verdict_in(line)
        if verdict is None:
            verdict = _legacy_verdict(code, stamp, last_system_exit)

        if verdict == VERDICT_SYSTEM:
            system_stops += 1
            if stamp is not None:
                last_system_exit = stamp
        elif verdict == VERDICT_CRASH:
            crashes += 1

    return GuardianLog(starts=starts, crashes=crashes, system_stops=system_stops)


def read_guardian_log(path: Path) -> GuardianLog:
    """读守护日志；文件不存在就当什么都没发生过（刚装好就是这个状态）。"""
    try:
        text = path.read_text(encoding="utf-8", errors="backslashreplace")
    except OSError:
        return GuardianLog(starts=0, crashes=0, system_stops=0)
    return parse_guardian_log(text)


def _legacy_verdict(
    code: int | None, stamp: datetime | None, last_system_exit: datetime | None
) -> str:
    """老格式的日志行（还没有「判定=」）只能按退出码倒推。"""
    if code in SHUTDOWN_CODES:
        return VERDICT_SYSTEM
    if (
        code == DLL_INIT_FAILED_CODE
        and stamp is not None
        and last_system_exit is not None
        and stamp - last_system_exit <= DLL_INIT_GRACE
    ):
        return VERDICT_SYSTEM
    if code == 0:
        return VERDICT_CLEAN
    return VERDICT_CRASH


def _code_in(line: str) -> int | None:
    match = _CODE_RE.search(line)
    return int(match.group(1)) if match else None


def _verdict_in(line: str) -> str | None:
    match = _VERDICT_RE.search(line)
    return match.group(1) if match else None


def _timestamp_in(line: str) -> datetime | None:
    match = _TIMESTAMP_RE.match(line)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None