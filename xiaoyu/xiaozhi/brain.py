"""设备说的一句话 -> 我们的大脑 -> 要下发给设备的 PCM（A 档第二批）。

这一层**不 import 任何具体实现**：识别 / 对话 / 合成 / 重采样全部从构造参数进来。
和 `server.py`（Transport）、`audio_codec.py`（OpusCodec）一个套路 ——
单测里塞假货就能验"接线对不对"，而"接线对不对"恰恰是最容易错、最难查的地方
（HANDOFF §9 那条教训：冒烟必须走线上同一段代码，别自己另接一遍）。

## 这一版刻意简化的（别以为已经有了）

**整轮跑完才开始下发**：`respond()` 一次把 ASR -> LLM -> TTS 干完，拿到全部 PCM
再逐段 yield。所以"边想边说"（第一句 1 秒内就出声）还没有 —— 那得把 LLM 的流式
输出和下发做成管道，属于下一步。先把链路跑通：**能听懂、能回话**比"回得快"重要。

异步那边只有一句 `asyncio.to_thread`：ASR / LLM / TTS 全是阻塞的，直接在这里调用
会把整个 WebSocket 服务卡住（心跳、打断、别的设备全停）。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass

import numpy as np

from ..logger import get_logger
from .server import TurnRequest

logger = get_logger(__name__)

# 我们的 ASR（sherpa-onnx sense-voice）只吃 16k 单声道 float32
ASR_SAMPLE_RATE = 16000

# 比这还短的一轮就当没听见：设备端 VAD 偶尔会甩过来一个几十毫秒的碎片，
# 喂给 ASR 只会得到一句幻觉（"嗯"、"谢谢观看"那种）。
MIN_UTTERANCE_SECONDS = 0.3

# 一轮回话最多下发这么久。防的是"模型疯了写作文"把设备端缓冲灌爆。
MAX_REPLY_SECONDS = 30.0


@dataclass(frozen=True)
class BrainParts:
    """四个接线点。名字故意起得直白 —— 接错哪个都是"它会答但没声音"这类难查的毛病。"""

    transcribe: Callable[[np.ndarray], str]
    """float32 单声道 @16k -> 文字。返回空串表示没听清。"""

    reply: Callable[..., Iterator[str]]
    """文字 -> 一句句回话。第二个位置参数是情绪回调（实现方可以忽略它）。"""

    synthesize: Callable[[str], "tuple[np.ndarray, int]"]
    """一句话 -> (float32 采样, 采样率)。"""

    resample: Callable[..., np.ndarray]
    """(采样, 源采样率, 目标采样率) -> 采样。上行拉 16k、下行拉 24k 都走它。"""


def to_pcm16(samples: np.ndarray, channels: int = 1) -> bytes:
    """float32（[-1,1]）-> s16le 字节。多声道就按声道复制，设备端只认交织的。"""
    data = np.clip(np.asarray(samples, dtype=np.float32).reshape(-1), -1.0, 1.0)
    pcm = (data * 32767.0).astype("<i2")
    if channels > 1:
        pcm = np.repeat(pcm[:, None], channels, axis=1).reshape(-1)
    return pcm.tobytes()


class BrainResponder:
    """`Responder` 的真实现：设备的每一轮都交给我们的 ASR / LLM / TTS。"""

    def __init__(
        self,
        parts: BrainParts,
        *,
        asr_sample_rate: int = ASR_SAMPLE_RATE,
        on_caption: Callable[[str], None] | None = None,
        on_emotion: Callable[[str, float], None] | None = None,
        max_reply_seconds: float = MAX_REPLY_SECONDS,
    ) -> None:
        self._parts = parts
        self._asr_rate = int(asr_sample_rate)
        self._on_caption = on_caption
        self._on_emotion = on_emotion
        self._max_reply_seconds = float(max_reply_seconds)
        self.turns = 0
        self.dropped = 0

    # ------------------------------------------------------------ Responder

    async def respond(self, request: TurnRequest):
        """设备的这一轮 -> 一串下行 PCM（采样率按 `codec.downlink`）。

        **不抛**：这一轮出什么岔子都不该把会话搞死。异常记一笔、当轮跳过，
        设备那边最多是"这次它没理我"，下一轮照旧。
        """
        if not request.audio:
            # 设备偶尔会发一个空轮（唤醒但没说话）。不答是对的 ——
            # 答了就是"你还没说话它就自己嘀咕"。
            self.dropped += 1
            logger.info("小智：这一轮设备没送音频过来，不答")
            return
        try:
            chunks = await asyncio.to_thread(
                self._think, request.audio, request.codec.uplink, request.codec.downlink
            )
        except Exception:  # noqa: BLE001
            logger.exception("小智：这一轮没答上来（已吞掉，会话继续）")
            return
        for chunk in chunks:
            yield chunk

    # ------------------------------------------------------------ 干活（阻塞，跑在线程池里）

    def _think(self, pcm: bytes, uplink, downlink) -> list[bytes]:
        started = time.perf_counter()
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32767.0
        if samples.size == 0:
            self.dropped += 1
            return []
        if uplink.channels > 1:
            # 设备一般发单声道；真有双声道就取第一路，别把左右声道串着喂 ASR
            samples = samples.reshape(-1, uplink.channels)[:, 0]

        seconds = samples.size / uplink.sample_rate
        if seconds < MIN_UTTERANCE_SECONDS:
            self.dropped += 1
            logger.info("小智：这一轮只有 {:.2f} 秒（<{:.1f} 秒），当没听见", seconds, MIN_UTTERANCE_SECONDS)
            return []

        if uplink.sample_rate != self._asr_rate:
            samples = self._parts.resample(samples, uplink.sample_rate, self._asr_rate)

        text = (self._parts.transcribe(samples) or "").strip()
        if not text:
            self.dropped += 1
            logger.info("小智：这一轮 {:.1f} 秒音频没识别出字，不答", seconds)
            return []
        self.turns += 1

        chunks: list[bytes] = []
        total_seconds = 0.0
        for sentence in self._parts.reply(text, self._on_emotion):
            sentence = (sentence or "").strip()
            if not sentence:
                continue
            if self._on_caption is not None:
                try:
                    self._on_caption(sentence)
                except Exception:  # noqa: BLE001
                    logger.exception("小智：字幕回调失败（不影响这轮说话）")
            audio, rate = self._parts.synthesize(sentence)
            audio = np.asarray(audio, dtype=np.float32).reshape(-1)
            if audio.size == 0:
                logger.warning("小智：这句话没合成出声音，跳过：{}", sentence[:20])
                continue
            if int(rate) != downlink.sample_rate:
                audio = self._parts.resample(audio, int(rate), downlink.sample_rate)
            total_seconds += audio.size / downlink.sample_rate
            if total_seconds > self._max_reply_seconds:
                logger.warning(
                    "小智：回话超过 {:.0f} 秒，剩下的不说了", self._max_reply_seconds
                )
                break
            chunks.append(to_pcm16(audio, downlink.channels))

        logger.info(
            "小智：这一轮答完 | 上行 {:.1f} 秒音频 -> {} 段、共 {:.1f} 秒回话 | 用时 {:.2f} 秒",
            seconds,
            len(chunks),
            total_seconds,
            time.perf_counter() - started,
        )
        return chunks
