"""语音转文字：sherpa-onnx + SenseVoice。

选它的理由：中文准确率高、纯 CPU 就能实时、离线、模型只有 160MB。
"""

from __future__ import annotations

import time

import numpy as np

from ..config import Settings
from ..logger import get_logger

logger = get_logger(__name__)


class SpeechRecognizer:
    """把一段波形转成文字。"""

    def __init__(self, settings: Settings) -> None:
        import sherpa_onnx

        self.settings = settings
        paths = settings.paths
        cfg = settings.asr

        models = sorted(paths.asr.glob("model*.onnx"))
        tokens = paths.asr / "tokens.txt"
        if not models:
            raise FileNotFoundError(f"识别模型不存在：{paths.asr}\\model*.onnx")
        if not tokens.exists():
            raise FileNotFoundError(f"识别模型缺少 tokens.txt：{tokens}")

        self._recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(models[0]),
            tokens=str(tokens),
            num_threads=cfg.num_threads,
            use_itn=cfg.use_itn,
            language=cfg.language,
            debug=False,
        )
        logger.info(
            "语音识别就绪 | 模型={} | 语言={} | 线程={}",
            models[0].name,
            cfg.language,
            cfg.num_threads,
        )

    def transcribe(self, audio: np.ndarray) -> str:
        """波形 -> 文字。听不清时返回空字符串。"""
        if audio.size == 0:
            logger.warning("传入的音频是空的，跳过识别")
            return ""

        started = time.perf_counter()
        stream = self._recognizer.create_stream()
        stream.accept_waveform(self.settings.audio.sample_rate, audio)
        self._recognizer.decode_stream(stream)
        text = (stream.result.text or "").strip()
        elapsed = time.perf_counter() - started

        if text:
            logger.info("识别结果：{}（耗时 {:.2f} 秒）", text, elapsed)
        else:
            logger.warning("没听清（耗时 {:.2f} 秒）", elapsed)
        return text