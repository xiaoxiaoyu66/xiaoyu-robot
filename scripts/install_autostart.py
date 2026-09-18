"""小柚子「常驻」生命周期工具（S5.5）。

用法（项目根目录，用系统 Python 3.11）：
    py -3.11 scripts\\install_autostart.py            # 安装：注册计划任务 + 电源设置
    py -3.11 scripts\\install_autostart.py --start    # 现在就起，不用等重新登录
    py -3.11 scripts\\install_autostart.py --status   # 活着没、跑了多久、端口通不通
    py -3.11 scripts\\install_autostart.py --stop     # 停（验收期间别用，一周计时会归零）
    py -3.11 scripts\\install_autostart.py --remove   # 卸载自启

装完做三件事：
    1. 注册计划任务 XiaoYuRobot：登录时启动 scripts\\guardian.py（本体挂了 5 秒自爬）
    2. 电源设置：接电永不睡眠 / 永不休眠 / 合盖不睡
    3. 打印 Windows 更新「使用时段」的手动设置提醒（强制重启后靠自启爬回来）

任务 XML 里埋了三个「跑不满一周」的坑，都是 2026-09-18 实测踩出来的：
    * DisallowStartIfOnBatteries / StopIfGoingOnBatteries
      —— schtasks 默认给 true。拔一下电源线任务当场被停，「常驻」当场结束。
    * ExecutionTimeLimit
      —— XML 里不写，默认就是 PT72H：第 4 天系统亲手把任务掐了。
        必须显式写 PT0S（= 不限时）。
    * 启动方式
      —— 偷懒直接启动 .cmd 会弹黑窗口，用户顺手一关，守护和本体一起死。
        改成 pythonw.exe 跑 guardian.py：进程天生没有控制台窗口，关无可关。

只支持 Windows；搬到 N100 装 Linux 后用 systemd，别跑这个脚本。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from xml.sax.saxutils import escape

TASK_NAME = "XiaoYuRobot"
ROOT = Path(__file__).resolve().parent.parent
GUARDIAN = ROOT / "scripts" / "guardian.py"

_TASK_XML = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>小柚子常驻守护（S5.5）：登录自启，本体崩溃 5 秒自爬，全程无窗口。</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{sid}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>3</Count>
    </RestartOnFailure>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{command}</Command>
      <Arguments>{arguments}</Arguments>
      <WorkingDirectory>{workdir}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""

_STATUS_PS = r"""
$ErrorActionPreference = 'SilentlyContinue'
$body = '*-m' + ' xiaoyu*'
$agents = @(Get-CimInstance Win32_Process | Where-Object {
        $_.Name -like 'python*' -and $_.CommandLine -like $body
    } | ForEach-Object {
        [pscustomobject]@{ pid = $_.ProcessId;
                           minutes = [int]((Get-Date) - $_.CreationDate).TotalMinutes }
    })
$guardians = @(Get-CimInstance Win32_Process | Where-Object {
        $_.Name -like 'python*' -and $_.CommandLine -like '*guardian.py*'
    } | Select-Object -ExpandProperty ProcessId)
$listening = @(Get-NetTCPConnection -LocalPort 8765 -State Listen |
    Select-Object -ExpandProperty OwningProcess)
[pscustomobject]@{
    xiaoyu = $agents
    guardian = $guardians
    listening = $listening
    task = (schtasks /query /tn XiaoYuRobot /fo LIST | Out-String)
} | ConvertTo-Json -Depth 5
"""


def run(cmd: list[str]) -> None:
    print("+", " ".join(cmd))
    subprocess.run(cmd, check=True)


def _pythonw() -> Path:
    """pythonw.exe：同样是 Python，但不带控制台窗口。找不到就退回 python.exe。"""
    exe = Path(sys.executable)
    console = exe.with_name("python.exe") if exe.name.lower() == "pythonw.exe" else exe
    windowless = console.with_name("pythonw.exe")
    return windowless if windowless.exists() else console


def _current_sid() -> str:
    """任务 XML 的 UserId 写 SID 最稳：换用户名、换域都对得上。"""
    try:
        done = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value"],
            capture_output=True, text=True, timeout=20, check=True,
        )
        sid = done.stdout.strip()
        if sid.startswith("S-1-"):
            return sid
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"（取 SID 失败，退回用户名写法：{exc}）")
    return f"{os.environ.get('USERDOMAIN', '.')}\\{os.environ.get('USERNAME', '')}"


def _as_list(value: object) -> list:
    """ConvertTo-Json 会把单元素数组拆成对象，这里统一成列表。"""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _power_settings() -> None:
    """接电档：永不睡眠、永不休眠、合盖不睡。电池档不动（拔电本来就是应急场景）。"""
    run(["powercfg", "/change", "standby-timeout-ac", "0"])
    run(["powercfg", "/change", "hibernate-timeout-ac", "0"])
    run(["powercfg", "/setacvalueindex", "SCHEME_CURRENT", "SUB_BUTTONS", "LIDACTION", "0"])
    run(["powercfg", "/setactive", "SCHEME_CURRENT"])


def install() -> None:
    if not GUARDIAN.exists():
        raise SystemExit(f"找不到守护脚本：{GUARDIAN}")

    xml = _TASK_XML.format(
        sid=escape(_current_sid()),
        command=escape(str(_pythonw())),
        arguments=escape(f'"{GUARDIAN}"'),
        workdir=escape(str(ROOT)),
    )

    # schtasks /xml 只认 UTF-16 带 BOM（XML 头里也这么声明了）
    with tempfile.TemporaryDirectory() as tmp:
        xml_path = Path(tmp) / f"{TASK_NAME}.xml"
        xml_path.write_bytes(xml.encode("utf-16"))
        run(["schtasks", "/create", "/f", "/tn", TASK_NAME, "/xml", str(xml_path)])

    _power_settings()
    start()

    print()
    print("装好了：登录 Windows 后小柚子自动起，全程没有窗口可关，崩溃 5 秒自爬，")
    print("        插电永不睡眠，拔电源 / 切电池也不会被杀，任务不限时。")
    print()
    print("还剩一步只能手点（Windows 更新会强制重启，重启后靠上面的自启爬回来）：")
    print("  设置 -> Windows 更新 -> 高级选项 -> 使用时段")
    print("  设成你平时在家的 8~10 小时，别让它大半夜自动重启。")
    print()
    print(f"验收：一周里随时跑 --status，xiaoyu 那行 pid 不该断；")
    print(f"      logs\\guardian.log 里 starting 应该一直只有 1 行（多出来就是它挂过）。")


def start() -> None:
    done = subprocess.run(["schtasks", "/run", "/tn", TASK_NAME], check=False)
    print("已请求启动。" if done.returncode == 0 else f"启动请求返回 {done.returncode}，用 --status 看看。")


def stop() -> None:
    """停：/end 会连整棵子进程树一起带走，剩的再补一刀。"""
    subprocess.run(["schtasks", "/end", "/tn", TASK_NAME], check=False)

    body = '*-m' + ' xiaoyu*'
    ps = (
        "Get-CimInstance Win32_Process | Where-Object { "
        f"$_.Name -like 'python*' -and ($_.CommandLine -like '{body}' "
        "-or $_.CommandLine -like '*guardian.py*') } | "
        "Select-Object -ExpandProperty ProcessId"
    )
    done = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
        capture_output=True, text=True, check=False,
    )
    leftovers = [line.strip() for line in done.stdout.splitlines() if line.strip().isdigit()]
    for pid in leftovers:
        # 上一步多半已经把它带走了，这里再失败也正常，别让 --stop 抛异常
        subprocess.run(
            ["taskkill", "/PID", pid, "/T", "/F"],
            capture_output=True, text=True, check=False,
        )
    print(f"停了（补刀 {len(leftovers)} 个残留进程）。想再起：--start。")


def status() -> None:
    done = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", _STATUS_PS],
        capture_output=True, text=True, check=False,
    )
    try:
        info = json.loads(done.stdout or "{}")
    except json.JSONDecodeError:
        print("读状态失败，PowerShell 原话：")
        print(done.stdout)
        print(done.stderr)
        return

    agents = _as_list(info.get("xiaoyu"))
    guardians = _as_list(info.get("guardian"))
    listening = _as_list(info.get("listening"))

    if agents:
        for item in agents:
            hours, minutes = divmod(int(item.get("minutes") or 0), 60)
            days, hours = divmod(hours, 24)
            print(f"本体：活着 | pid={item.get('pid')} | 已连续运行 {days} 天 {hours} 小时 {minutes} 分")
    else:
        print("本体：没在跑（该跑 --start，或者查 logs\\error.log）")

    print(f"守护：{'在，pid=' + ', '.join(str(g) for g in guardians) if guardians else '没在跑'}")
    print(f"脸页端口 8765：{'在听' if listening else '没在听'}")

    log = ROOT / "logs" / "guardian.log"
    if log.exists():
        starts = 0
        crashes = 0
        with log.open("r", encoding="utf-8", errors="backslashreplace") as fh:
            for line in fh:
                if "guardian: starting" in line:
                    starts += 1
                elif "guardian: exited" in line:
                    crashes += 1
        # 「启动次数」不是异常信号：每次开机 / 重登录都会 +1。
        # 真正要盯的是本体中途崩了几次 —— 那才说明有东西坏了。
        print(f"守护启动次数：{starts}（每次开机 / 重登录 +1，正常）")
        print(
            f"本体中途崩溃次数：{crashes}"
            + ("（正常）" if crashes == 0 else "  <- 异常，去看 logs\\error.log")
        )

    print()
    print(str(info.get("task") or "").strip())


def remove() -> None:
    stop()
    run(["schtasks", "/delete", "/f", "/tn", TASK_NAME])
    print("已卸载自启（电源设置保留着，想恢复默认去 设置->系统->电源 里改）。")


def main(argv: list[str]) -> int:
    if "--remove" in argv:
        remove()
    elif "--start" in argv:
        start()
    elif "--stop" in argv:
        stop()
    elif "--status" in argv:
        status()
    else:
        install()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
