"""开机自启 + 电源设置安装器（S5.5）。

用法（在项目根目录）：
    python scripts\\install_autostart.py            # 安装
    python scripts\\install_autostart.py --remove  # 卸载

做三件事：
    1. 用 schtasks 注册"登录时启动 run_forever.cmd"（守护循环，崩溃 5 秒自爬）
    2. 电源设置：接通电源永不睡眠、不休眠（否则半夜它悄悄睡了）
    3. 打印 Windows 更新"使用时段"的手动设置提醒（自动更新重启后要能自己爬起来，
       这步系统设置不给命令行全权限，照着印出来的步骤点两下就行）

只支持 Windows；别的平台（比如以后 N100 装 Linux）用 systemd，别跑这个脚本。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

TASK_NAME = "XiaoYuRobot"
ROOT = Path(__file__).resolve().parent.parent
GUARDIAN = ROOT / "scripts" / "run_forever.cmd"


def run(cmd: list[str]) -> None:
    print("+", " ".join(cmd))
    subprocess.run(cmd, check=True)


def install() -> None:
    if not GUARDIAN.exists():
        raise SystemExit(f"找不到守护脚本：{GUARDIAN}")

    # 1. 登录时自启（/f 覆盖旧的；/rl limited 不需要管理员）
    run([
        "schtasks", "/create", "/f", "/tn", TASK_NAME,
        "/tr", f'"{GUARDIAN}"',
        "/sc", "onlogon", "/rl", "limited",
    ])

    # 2. 电源：插电时显示器可以关，但机器永不睡眠/休眠
    #    （笔记本合盖默认睡眠，对常驻是致命的；这两条只改"接通电源"档，电池档不动）
    run(["powercfg", "/change", "standby-timeout-ac", "0"])
    run(["powercfg", "/change", "hibernate-timeout-ac", "0"])
    run(["powercfg", "/setacvalueindex", "SCHEME_CURRENT", "SUB_BUTTONS", "LIDACTION", "0"])
    run(["powercfg", "/setactive", "SCHEME_CURRENT"])

    print()
    print("装好了：登录 Windows 后小柚子会自动起，崩溃 5 秒自爬，插电永不睡眠。")
    print()
    print("还剩一步只能手点（Windows 更新会强制重启，重启后靠上面的自启爬回来）：")
    print("  设置 -> Windows 更新 -> 高级选项 -> 使用时段")
    print("  设成你平时在家的 8~10 小时，别让它大半夜自动重启。")
    print()
    print("验证：注销重登录，任务管理器里应该能看到 cmd(py -3.11 -m xiaoyu)。")


def remove() -> None:
    run(["schtasks", "/delete", "/f", "/tn", TASK_NAME])
    print("已卸载开机自启（电源设置保留着，想恢复默认去 设置->系统->电源 里改）。")


if __name__ == "__main__":
    if "--remove" in sys.argv:
        remove()
    else:
        install()
