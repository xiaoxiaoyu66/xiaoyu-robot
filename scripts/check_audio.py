"""S0 音频自检：确认麦克风录得到、喇叭放得出。

跑法（在项目根目录下）：
    python scripts\\check_audio.py

录不到声音或放不出声音时，先跑这个找设备：
    python scripts\\diagnose_audio.py

90% 的人第一周是死在音频设备上，不是死在 AI 上，
所以先把这一步跑通再往下做。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from xiaoyu.audio.devices import resolve_device
from xiaoyu.audio.player import Speaker
from xiaoyu.audio.recorder import Recorder, list_devices
from xiaoyu.config import Settings
from xiaoyu.logger import get_logger

logger = get_logger("scripts.check_audio")

RECORD_SECONDS = 3.0


def main() -> int:
    settings = Settings.load()

    logger.info("=" * 58)
    logger.info("S0 · 音频自检")
    logger.info("=" * 58)

    _, in_name = resolve_device(
        settings.audio.mic_device, settings.audio.mic_device_name, output=False
    )
    _, out_name = resolve_device(
        settings.audio.speaker_device, settings.audio.speaker_device_name, output=True
    )
    logger.info("本次使用的输入设备：{}", in_name)
    logger.info("本次使用的输出设备：{}", out_name)
    if settings.audio.speaker_device is None and not settings.audio.speaker_device_name:
        logger.warning(
            "没有指定输出设备，用的是系统默认。如果默认输出是显示器，"
            "你会听不到任何声音 —— 跑 python scripts\\diagnose_audio.py 找一个能出声的"
        )

    logger.info("当前系统里的音频设备：")
    for line in list_devices().splitlines():
        logger.info(line)
    logger.info("-" * 58)

    recorder = Recorder(settings.audio)
    speaker = Speaker(settings.audio)

    logger.info("请对着麦克风随便说一句话，现在开始录 {:.0f} 秒...", RECORD_SECONDS)
    audio = recorder.record_fixed(RECORD_SECONDS)

    peak = float(np.abs(audio).max()) if audio.size else 0.0
    logger.info("录音峰值 = {:.5f}", peak)

    if peak < 0.001:
        logger.error("几乎没录到声音。按顺序排查：")
        logger.error("  1. Windows 设置 -> 隐私和安全性 -> 麦克风：允许桌面应用访问")
        logger.error("  2. 声音设置 -> 输入 -> 选中麦克风 -> 音量拉高、关闭自动增益")
        logger.error("  3. 麦克风被别的软件占用（微信 / 腾讯会议 / 游戏语音）")
        logger.error("  4. \u628a\u4e0a\u9762\u6e05\u5355\u91cc\u7684\u8f93\u5165\u8bbe\u5907\u540d\u5b57\u586b\u8fdb .env \u7684 XIAOYU_MIC_DEVICE_NAME")
        logger.error("  5. 还是不行就跑 python scripts\\diagnose_audio.py")
        return 1

    logger.info("录音正常。正在回放...")
    speaker.play_array(audio)

    logger.info("=" * 58)
    logger.info("听到自己的声音 = S0 通过，可以进入 S1 了。")
    logger.info("没听到 = 输出设备选错了，跑 python scripts\\diagnose_audio.py")
    logger.info("=" * 58)
    return 0


if __name__ == "__main__":
    sys.exit(main())