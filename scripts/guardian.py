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
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG_PATH = ROOT / "logs" / "guardian.log"
RESTART_DELAY_SECONDS = 5.0

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
        _log(
            f"exited | code={code} | 活了一轮 {alive:.0f} 秒｜"
            f"{RESTART_DELAY_SECONDS:.0f} 秒后重启"
        )
        time.sleep(RESTART_DELAY_SECONDS)


if __name__ == "__main__":
    sys.exit(main())
