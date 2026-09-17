"""播放：把 mp3 / 波形放出来。

用 pygame 播放 mp3（edge-tts 的输出是 mp3），用 sounddevice 播放内存波形。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import sounddevice as sd

from ..config import AudioConfig
from ..logger import get_logger

logger = get_logger(__name__)


class Speaker:
    """扬声器。"""

    def __init__(self, config: AudioConfig) -> None:
        self.config = config
        self._mixer_ready = False
        logger.debug(
            "扬声器就绪 | 设备={}",
            config.speaker_device if config.speaker_device is not None else "系统默认",
        )

    def _ensure_mixer(self) -> None:
        if self._mixer_ready:
            return
        import pygame

        pygame.mixer.init()
        self._mixer_ready = True
        logger.debug("pygame 混音器初始化完成")

    def play_file(self, path: str | Path, wait: bool = True) -> None:
        """播放一个音频文件。wait=True 时会等它放完 —— 这一步很重要，
        不等放完就开麦，麦克风会把自己的声音录进去。"""
        import pygame

        path = Path(path)
        if not path.exists():
            logger.error("音频文件不存在：{}", path)
            return

        self._ensure_mixer()
        logger.debug("播放 {}", path.name)
        pygame.mixer.music.load(str(path))
        pygame.mixer.music.play()
        if wait:
            while pygame.mixer.music.get_busy():
                pygame.time.wait(50)
            logger.debug("播放结束 {}", path.name)

    def stop(self) -> None:
        if not self._mixer_ready:
            return
        import pygame

        pygame.mixer.music.stop()
        logger.debug("已停止播放")

    def play_array(self, audio: np.ndarray) -> None:
        """播放内存里的波形（S0 自检用）。"""
        if audio.size == 0:
            logger.warning("待播放的音频为空")
            return
        sd.play(audio, self.config.sample_rate, device=self.config.speaker_device)
        sd.wait()
        logger.debug("波形播放结束")