"""找出"哪个设备能出声、哪个能录音"。

为什么需要它：
    系统默认输出设备不一定是喇叭。实测遇到过默认输出是**显示器的 HDMI 音频**，
    声音全进了显示器，笔记本扬声器一点动静都没有 —— 而且日志看起来一切正常。

用法（在项目根目录下跑）：
    python scripts\\diagnose_audio.py                 # 挨个试输出设备，你确认哪个响了
    python scripts\\diagnose_audio.py --device 4      # 只试编号 4
    python scripts\\diagnose_audio.py --all           # 连各种驱动（WASAPI 等）一起试
    python scripts\\diagnose_audio.py --record        # 顺便测麦克风

确认好了就把编号写进 .env：
    XIAOYU_SPK_DEVICE=4
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import sounddevice as sd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from xiaoyu.audio.player import resolve_device
from xiaoyu.config import Settings
from xiaoyu.logger import get_logger

logger = get_logger("scripts.diagnose_audio")


def make_beep(samplerate: int, duration: float = 0.7) -> np.ndarray:
    """生成一段"嘀—嘟"提示音，带淡入淡出（不然会有爆音）。"""
    samples = int(samplerate * duration)
    t = np.linspace(0, duration, samples, endpoint=False)
    half = samples // 2
    wave = np.zeros(samples, dtype=np.float64)
    wave[:half] = np.sin(2 * np.pi * 880 * t[:half])
    wave[half:] = np.sin(2 * np.pi * 660 * t[half:])

    fade = max(1, int(0.02 * samplerate))
    wave[:fade] *= np.linspace(0, 1, fade)
    wave[-fade:] *= np.linspace(1, 0, fade)
    return (wave * 0.3).astype(np.float32)


def play_beep(device: int | None) -> bool:
    """在一个设备上放提示音。返回是否成功。"""
    beep = make_beep(44100)
    for samplerate, wave in ((44100, beep), (16000, make_beep(16000))):
        try:
            sd.play(wave, samplerate, device=device)
            sd.wait()
            return True
        except Exception as exc:
            logger.debug("设备 {} 用 {}Hz 播放失败：{}", device, samplerate, exc)
    return False


def output_candidates(all_apis: bool) -> list[tuple[int, str, bool]]:
    """列出值得试的输出设备，返回 [(编号, 名字, 是否系统默认)]。"""
    devices = sd.query_devices()
    apis = sd.query_hostapis()
    _, default_out = sd.default.device

    mme = next((i for i, a in enumerate(apis) if a["name"] == "MME"), None)
    result: list[tuple[int, str, bool]] = []

    for index, dev in enumerate(devices):
        if dev["max_output_channels"] <= 0:
            continue
        if not all_apis:
            if dev["hostapi"] != mme:
                continue
            if "声音映射器" in dev["name"] or "Sound Mapper" in dev["name"]:
                continue          # 这是系统的设备别名，不是真实设备
        result.append((index, dev["name"], index == default_out))
    return result


def ask(prompt: str) -> str:
    try:
        return input(prompt).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return "q"


def test_record(settings: Settings, play_device: int | None) -> None:
    print("\n" + "=" * 62)
    print("录音测试")
    print("=" * 62)

    index, name = resolve_device(settings.audio.mic_device, output=False)
    print(f"将使用输入设备：{name}")

    seconds = 3.0
    print(f"对着麦克风说一句话，录 {seconds:.0f} 秒...")
    try:
        audio = sd.rec(
            int(seconds * settings.audio.sample_rate),
            samplerate=settings.audio.sample_rate,
            channels=1,
            dtype="float32",
            device=index,
        )
        sd.wait()
    except Exception as exc:
        logger.error("录音失败：{}", exc)
        return

    peak = float(np.abs(audio).max())
    rms = float(np.sqrt(np.mean(audio**2)))
    print(f"\n峰值 = {peak:.4f}   均方根 = {rms:.5f}")

    if peak < 0.001:
        print("\n几乎没有声音。按顺序排查：")
        print("  1. Windows 设置 -> 隐私和安全性 -> 麦克风：允许桌面应用访问")
        print("  2. 声音设置 -> 输入 -> 选中麦克风 -> 音量拉高、关闭自动增益")
        print("  3. 麦克风被别的软件占用（微信 / 腾讯会议 / 游戏语音）")
        print("  4. 换个输入设备编号再试（用 --all 能看到全部）")
        return

    print("\n正在回放刚才的录音...")
    if play_beep(play_device):        # 先响一声，确认输出设备是对的
        sd.play(audio, settings.audio.sample_rate, device=play_device)
        sd.wait()
        print("听到自己的声音了吗？听到就说明录音和播放都通了。")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="音频设备诊断")
    parser.add_argument("--device", type=int, default=None, help="只测这一个设备编号")
    parser.add_argument("--all", action="store_true", help="连 WASAPI/DirectSound 等一起试")
    parser.add_argument("--record", action="store_true", help="顺便测录音")
    args = parser.parse_args(argv)

    settings = Settings.load()
    _, default_in = sd.default.device
    in_index, in_name = resolve_device(settings.audio.mic_device, output=False)
    out_index, out_name = resolve_device(settings.audio.speaker_device, output=True)

    print("=" * 62)
    print("音频设备诊断")
    print("=" * 62)
    print(f"当前输入：{in_name}")
    print(f"当前输出：{out_name}")
    print()

    if args.device is not None:
        print(f"在 [{args.device}] 上播放提示音...")
        if play_beep(args.device):
            print("已播放。听到了吗？")
        else:
            print("播放失败，这个编号可能不能用。")
        if args.record:
            test_record(settings, args.device)
        return 0

    candidates = output_candidates(args.all)
    print(f"接下来会在 {len(candidates)} 个输出设备上依次播放「嘀—嘟」提示音。")
    print("每放一个，请回答 y（听到了）/ n（没听到）/ q（退出）。")
    print("注意：只有真实喇叭才会响，显示器、未接音箱的 HDMI 都不会出声。\n")

    found: int | None = None
    for index, name, is_default in candidates:
        mark = "   ← 当前系统默认" if is_default else ""
        print(f"[{index:2}] {name}{mark}")
        if not play_beep(index):
            print("     播放失败，跳过\n")
            continue

        answer = ask("     听到了吗？(y/n/q) ")
        if answer == "q":
            break
        if answer == "y":
            found = index
            print(f"     -> 记下了：{index}\n")
            break
        print()

    print("=" * 62)
    if found is not None:
        print("找到能出声的设备了。把这一行写进项目根目录的 .env：")
        print()
        print(f"    XIAOYU_SPK_DEVICE={found}")
        print()
        print("写完重跑一次确认： python scripts\\check_audio.py")
    else:
        print("没有找到能出声的设备。检查：")
        print("  1. 笔记本扬声器有没有静音（看任务栏音量图标）")
        print("  2. 声音设置 -> 输出，确认选中的是「扬声器」而不是显示器")
        print("  3. 用 --all 把所有驱动类型都试一遍")
    print("=" * 62)

    if args.record:
        test_record(settings, found if found is not None else out_index)

    return 0 if found is not None else 1


if __name__ == "__main__":
    sys.exit(main())