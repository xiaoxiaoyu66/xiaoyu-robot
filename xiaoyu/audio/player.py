"""播放：把音频放出来。

用 soundfile 解码 + sounddevice 输出。

为什么不继续用 pygame：
    pygame 的混音器自己挑输出设备，**没法跟着 .env 里的设置走**。
    实测踩到的坑：当系统默认输出是显示器的 HDMI 音频时，
    TTS 的声音全进了显示器，笔记本扬声器一点动静都没有，而且非常难查。
    sounddevice 可以直接指定设备编号，和录音走同一套配置。

设备怎么挑（编号会漂移这件事）见 xiaoyu/audio/devices.py 的说明。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import sounddevice as sd
import soundfile as sf

from ..config import AudioConfig
from ..logger import get_logger
from .devices import resolve_device

logger = get_logger(__name__)

__all__ = ["Speaker", "resolve_device"]


class Speaker:
    """扬声器。"""

    def __init__(self, config: AudioConfig) -> None:
        self.config = config
        self._device, description = resolve_device(
            config.speaker_device, config.speaker_device_name, output=True
        )
        logger.info("扬声器就绪 | {}", description)
        if config.speaker_device is None and not config.speaker_device_name:
            logger.warning(
                "没有配置输出设备，用的是系统默认 —— 如果默认输出是显示器，"
                "你会完全听不到声音。跑 python scripts\\diagnose_audio.py 选一个能出声的"
            )

    @property
    def device(self) -> int | None:
        return self._device

    def play_file(self, path: str | Path, wait: bool = True) -> None:
        """播放一个音频文件。

        wait=True 时会等它放完 —— 这一步很重要：不等放完就开麦，
        麦克风会把它自己的声音录进去。
        """
        path = Path(path)
        if not path.exists():
            logger.error("音频文件不存在：{}", path)
            return

        try:
            data, samplerate = sf.read(str(path), dtype="float32", always_2d=False)
        except Exception:
            logger.exception("解码音频失败：{}", path.name)
            return

        if data.ndim > 1:                 # 多声道降成单声道
            data = data.mean(axis=1)

        logger.debug("播放 {}（{:.2f} 秒 @ {}Hz）", path.name, len(data) / samplerate, samplerate)
        self.play_array(data, samplerate, wait=wait)

    def play_array(
        self, audio: np.ndarray, samplerate: int | None = None, wait: bool = True
    ) -> None:
        """播放内存里的波形。"""
        if audio.size == 0:
            logger.warning("待播放的音频是空的")
            return

        samplerate = samplerate or self.config.sample_rate
        try:
            sd.play(audio, samplerate, device=self._device)
        except Exception:
            logger.exception(
                "播放失败，设备 {} 可能不可用。跑 python scripts\\diagnose_audio.py 找一个能出声的设备",
                self._device,
            )
            return

        if wait:
            sd.wait()

    def stop(self) -> None:
        sd.stop()
        logger.debug("已停止播放")