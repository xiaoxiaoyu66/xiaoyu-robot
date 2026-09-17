"""S0 音频自检：确认麦克风录得到、喇叭放得出。

跑法（在项目根目录下）：
    python scripts\\check_audio.py

90% 的人第一周是死在音频设备上，不是死在 AI 上。
所以先把这一步跑通，再往下做。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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
    logger.info("当前系统里的音频设备：")
    for line in list_devices().splitlines():
        logger.info(line)
    logger.info("-" * 58)

    recorder = Recorder(settings.audio)
    speaker = Speaker(settings.audio)

    logger.info("请对着麦克风随便说一句话，现在开始录 {:.0f} 秒...", RECORD_SECONDS)
    audio = recorder.record_fixed(RECORD_SECONDS)

    peak = float(abs(audio).max()) if audio.size else 0.0
    logger.info("录音峰值 = {:.5f}", peak)

    if peak < 0.001:
        logger.error("几乎没录到声音。按顺序排查：")
        logger.error("  1. 是不是麦克风被别的软件占用了（微信 / 腾讯会议 / 游戏语音）")
        logger.error("  2. 系统设置 -> 隐私和安全性 -> 麦克风，确认允许桌面应用访问")
        logger.error("  3. 上面的设备清单里如果有多个输入设备，把编号填进 .env 的 XIAOYU_MIC_DEVICE")
        logger.error("  4. 声音设置里把麦克风音量拉高，并关掉"自动增益"试试")
        return 1

    logger.info("正在播放刚才的录音...")
    speaker.play_array(audio)

    logger.info("=" * 58)
    logger.info("如果刚才清楚听到了自己的声音 —— S0 通过，可以进入 S1 了。")
    logger.info("=" * 58)
    return 0


if __name__ == "__main__":
    sys.exit(main())