"""文字转语音：edge-tts。

为什么不一开始用 Piper：Piper 的中文音色偏机械，而 edge-tts 免费、
中文很自然、一行就能装好。代价是第一次合成要联网。
以后想彻底离线，再换 Piper；想更像人，上 GPT-SoVITS 克隆音色。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable
from pathlib import Path

from ..audio.player import Speaker
from ..config import Settings
from ..logger import get_logger

logger = get_logger(__name__)

_CACHE_KEEP = 20  # 最多保留多少个合成缓存文件


class Synthesizer:
    """合成并播放。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._cfg = settings.tts
        self._cache_dir = settings.paths.data / "tts_cache"
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._speaker = Speaker(settings.audio)
        logger.info(
            "语音合成就绪 | 音色={} | 语速={}", self._cfg.voice, self._cfg.rate
        )

    async def _synthesize(self, text: str, path: Path) -> None:
        import edge_tts

        communicate = edge_tts.Communicate(
            text, self._cfg.voice, rate=self._cfg.rate, volume=self._cfg.volume
        )
        await communicate.save(str(path))

    @property
    def speaker(self) -> Speaker:
        """给外面播提示音用。

        故意复用同一份 Speaker：另建一个会重新解析一次音频设备，
        日志里会出现两次"扬声器就绪"，排查问题时很迷惑。
        """
        return self._speaker

    def speak(self, text: str) -> None:
        """合成一句并播放，等它放完才返回（播放期间必须保持闭麦）。"""
        text = text.strip()
        if not text:
            return

        path = self._cache_dir / f"reply_{int(time.time() * 1000)}.mp3"
        started = time.perf_counter()
        try:
            asyncio.run(self._synthesize(text, path))
        except Exception:
            logger.exception("语音合成失败，跳过这句：{}", text[:30])
            return

        logger.debug("合成完成 {:.2f} 秒 -> {}", time.perf_counter() - started, path.name)
        self._speaker.play_file(path, wait=True)
        self._cleanup()

    def speak_stream(self, sentences: Iterable[str]) -> None:
        """一句一句地念 —— 这是"首句 2.5 秒内出声"的关键。
        不要等整段回答生成完再合成。"""
        count = 0
        for sentence in sentences:
            if not sentence.strip():
                continue
            count += 1
            if count == 1:
                logger.info("开始说话（首句）：{}", sentence.strip()[:40])
            self.speak(sentence)
        if count == 0:
            logger.warning("没有可播放的内容")

    def _cleanup(self) -> None:
        files = sorted(self._cache_dir.glob("reply_*.mp3"), key=lambda p: p.stat().st_mtime)
        for old in files[:-_CACHE_KEEP]:
            try:
                old.unlink()
            except OSError:
                pass