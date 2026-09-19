"""小柚子进程守护（S5.5）：本体退出/崩溃 5 秒后自己爬起来。

为什么不是 .cmd：
    任务计划启动 .cmd 必然弹一个黑窗口，而"看得见的窗口"迟早被顺手关掉 ——
    关窗口 = 守护 + 本体一起死，一周常驻直接归零（2026-09-18 真人实测踩到）。
    pythonw.exe 起的进程天生没有控制台窗口，关无可关，这才叫"关不掉"。
    本体自己也有自愈：单轮异常不退出，连续失败 5 次才交棒（见 xiaoyu/app.py）。

停它：
    任务管理器结束 pythonw.exe，或者 python scripts\\install_autostart.py --stop。

日志：
    这里只记"起来了 / 挂了 / 重启中"三类行；本体详细日志在 logs\\xiaoyu_YYYY-MM-DD.log。
    退出行带「判定=」——"什么才算崩了"只写在 xiaoyu/lifecycle.py 一份，仪表盘读同一份。

关机时：
    系统关机 / 重启会把本体终结掉，这不算崩。2026-09-19 搬家断电实测：
    当时守护还傻乎乎地 5 秒后重启，可机器已经在关机流程里，白刷一行 0xC0000142，
    仪表盘就报了「本体中途崩溃次数：2」。现在先确认系统真在关机，是就安静收工。
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 计划任务里是 pythonw scripts\\guardian.py 这么起的，sys.path[0] 是 scripts/ 自己，
# 主包不在路径上 —— 手动补一下才 import 得到业务模块。
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from xiaoyu.lifecycle import judge_exit  # noqa: E402  （要等 ROOT 进 sys.path）

LOG_PATH = ROOT / "logs" / "guardian.log"
RESTART_DELAY_SECONDS = 5.0

# 系统真在关机：GetSystemMetrics(SM_SHUTTINGDOWN) 非 0 就是。
_SM_SHUTTINGDOWN = 0x2000
# 关机是可能被取消的（有程序拦着不让关、用户点了取消），所以先等一等再下结论：
# 等到 30 秒还说要关，才认它真在关机。
_SHUTDOWN_GRACE_SECONDS = 30.0
_SHUTDOWN_POLL_SECONDS = 3.0

# Windows：让子进程不弹控制台窗口。别的平台没这个常量，给 0 就是默认行为。
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _python_exe() -> str:
    """守护自己多半跑在 pythonw.exe 下，本体还是用带控制台的 python.exe 更稳。"""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        console_exe = exe.with_name("python.exe")
        if console_exe.exists():
            return str(console_exe)
    return str(exe)


def _log(line: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8", errors="backslashreplace") as fh:
        fh.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] guardian: {line}\n")


def _system_is_shutting_down() -> bool:
    """Windows 正在关机 / 重启 / 注销？

    别的平台没这个概念，一律当没关机（守护照常重启）。
    问不出来也别拿它做判断 —— 宁可多重启一次，也不要该起来的时候躺平。
    """
    if os.name != "nt":
        return False
    try:
        return bool(ctypes.windll.user32.GetSystemMetrics(_SM_SHUTTINGDOWN))
    except Exception:
        return False


def _shutdown_confirmed() -> bool:
    """确认系统是真的在关机，而不是"刚说要关、又被取消了"。

    真被取消了我还得爬起来干活，所以先等一下：关机标记消失了 = 照常重启；
    一直不消失 = 确实在关机，安静收工（起了也是白起，还会污染崩溃计数）。
    """
    deadline = time.monotonic() + _SHUTDOWN_GRACE_SECONDS
    while True:
        if not _system_is_shutting_down():
            return False
        if time.monotonic() >= deadline:
            return True
        time.sleep(_SHUTDOWN_POLL_SECONDS)


def main() -> int:
    python = _python_exe()

    env = dict(os.environ)
    # 输出重定向进文件，别让 Windows 拿本地代码页去猜（老 .cmd 的 >> 写出来中文全是乱码）
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["PYTHONUNBUFFERED"] = "1"

    _log(f"starting | 守护 pid={os.getpid()} | 本体 = {python} -m xiaoyu")

    while True:
        started = time.monotonic()
        with LOG_PATH.open("a", encoding="utf-8", errors="backslashreplace") as sink:
            try:
                proc = subprocess.Popen(
                    [python, "-m", "xiaoyu"],
                    cwd=str(ROOT),
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=sink,
                    stderr=subprocess.STDOUT,
                    creationflags=_NO_WINDOW,
                )
            except OSError as exc:
                _log(f"本体没起来：{exc}｜{RESTART_DELAY_SECONDS:.0f} 秒后重试")
                time.sleep(RESTART_DELAY_SECONDS)
                continue
            code = proc.wait()

        alive = time.monotonic() - started
        # 顺序要紧：先问一句"系统是不是真在关机"，再定性。
        result = judge_exit(code, shutting_down=_shutdown_confirmed())
        tail = (
            f"{RESTART_DELAY_SECONDS:.0f} 秒后重启"
            if result.restart
            else "系统要关机了，守护一起收工"
        )
        _log(f"exited | code={code} | 活了一轮 {alive:.0f} 秒｜判定={result.verdict}｜{tail}")

        if not result.restart:
            return 0
        time.sleep(RESTART_DELAY_SECONDS)


if __name__ == "__main__":
    sys.exit(main())
