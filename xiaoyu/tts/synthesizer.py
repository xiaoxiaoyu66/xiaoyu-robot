"""文字转语音：把大模型吐出来的句子，一句一句变成声音放出来。

两个关键设计：

1. **引擎可换**（细节见 engine.py）
   默认用本地 sherpa-onnx。因为 edge-tts 每句话都要连一次微软的服务器，
   实测首块音频 0.88~11.76 秒；本地只要 0.1~0.3 秒。

2. **合成和播放重叠**（这是本类存在的主要理由）
   旧版是"合成 -> 播放 -> 合成 -> 播放"串行的，每句话的合成时间
   都白白加到总时长上。
   现在合成在主线程、播放在另一个线程，中间用一个容量 2 的队列连着：
   放第一句的时候，第二句已经在合成了。
   容量故意只有 2 —— 攒太多的话，将来做打断功能时，
   用户一插话还要等一堆没用的音频放完。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterable
from queue import Queue

from ..audio.player import Speaker
from ..config import Settings
from ..logger import get_logger
from .engine import Speech, build_engine

logger = get_logger(__name__)

# 最多"提前合成"几句。2 = 正在放的那句 + 已经备好的一句。
_QUEUE_SIZE = 2


class Synthesizer:
    """合成并播放。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._cfg = settings.tts
        self._speaker = Speaker(settings.audio)
        self._engine = build_engine(settings)
        logger.info(
            "语音合成就绪 | 引擎={} | 采样率={}Hz",
            self._engine.name, getattr(self._engine, "samplerate", "?"),
        )
        # 先把模型跑热。不做这一步，用户听到的第一句会莫名其妙慢好几秒 ——
        # 而那恰好是他最在意的一次。
        self._engine.warmup()

    @property
    def speaker(self) -> Speaker:
        """给外面播提示音用。

        故意复用同一份 Speaker：另建一个会重新解析一次音频设备，
        日志里会出现两次"扬声器就绪"，排查问题时很迷惑。
        """
        return self._speaker

    @property
    def engine_name(self) -> str:
        return self._engine.name

    def speak(self, text: str) -> None:
        """合成一句话并放完（阻塞）。主要给调试和单句场景用。"""
        text = text.strip()
        if not text:
            return
        try:
            speech = self._engine.synthesize(text)
        except Exception:
            logger.exception("合成失败，跳过这句：{}", text[:30])
            return
        self._speaker.play_array(speech.samples, speech.samplerate)

    def speak_stream(self, sentences: Iterable[str]) -> None:
        """一边收句子一边说：**播放和合成是重叠的**。

        sentences 通常是大模型流式回复切出来的句子生成器，
        所以这个循环会边等模型吐字、边合成、边播放。
        """
        started = time.perf_counter()
        queue: Queue[Speech | None] = Queue(maxsize=_QUEUE_SIZE)
        first_audio_logged = False

        def player() -> None:
            nonlocal first_audio_logged
            while True:
                speech = queue.get()
                if speech is None:
                    return
                if not first_audio_logged:
                    first_audio_logged = True
                    logger.info(
                        "首句出声 | 从收到回复算起 {:.2f} 秒（这就是用户实际等的时间）",
                        time.perf_counter() - started,
                    )
                try:
                    self._speaker.play_array(speech.samples, speech.samplerate)
                except Exception:
                    logger.exception("播放失败，跳过这句")

        player_thread = threading.Thread(
            target=player, name="xiaoyu-tts-player", daemon=True
        )
        player_thread.start()

        count = 0
        try:
            for sentence in sentences:
                text = sentence.strip()
                if not text:
                    continue
                count += 1
                if count == 1:
                    logger.info("开始说（首句）：{}", text[:40])
                try:
                    speech = self._engine.synthesize(text)
                except Exception:
                    # 一句合成失败不该把整轮回复废掉，跳过继续
                    logger.exception("合成失败，跳过这句：{}", text[:30])
                    continue
                queue.put(speech)          # 队列满时会在这里等，天然限流
        finally:
            queue.put(None)
            player_thread.join()

        if count == 0:
            logger.warning("没有可播放的内容")
        else:
            logger.debug(
                "本轮说了 {} 句，总耗时 {:.2f} 秒", count, time.perf_counter() - started
            )