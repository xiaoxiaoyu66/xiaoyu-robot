"""唤醒词：sherpa-onnx 关键词检测（KWS）。

为什么用它：
    - 完全免费、离线、不用注册账号
    - 支持中文，且唤醒词直接写在 config/keywords.txt 里，改词不用重新训练
    - 模型只有 33MB，CPU 常驻几乎不吃资源

keywords.txt 格式：
    关键词 :阈值 #增强 @显示名
    小宇 :2.5 #0.5 @小宇

官方参考实现（卡住时对照这个）：
    sherpa-onnx 仓库 / python-api-examples / keyword-spotter-from-microphone.py
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from ..config import Settings
from ..logger import get_logger

logger = get_logger(__name__)

_BLOCK_SECONDS = 0.1


class WakeWordDetector:
    """常驻监听唤醒词。"""

    def __init__(self, settings: Settings) -> None:
        import sherpa_onnx

        self.settings = settings
        paths = settings.paths
        cfg = settings.wake

        keywords_file = paths.keywords
        if not keywords_file.exists():
            raise FileNotFoundError(f"唤醒词文件不存在：{keywords_file}")

        def pick(pattern: str, what: str) -> str:
            found = sorted(paths.kws.glob(pattern))
            if not found:
                raise FileNotFoundError(f"唤醒模型缺少 {what}（{paths.kws}\\{pattern}）")
            return str(found[0])

        self._spotter = sherpa_onnx.KeywordSpotter(
            tokens=pick("tokens.txt", "tokens.txt"),
            encoder=pick("encoder*.onnx", "encoder"),
            decoder=pick("decoder*.onnx", "decoder"),
            joiner=pick("joiner*.onnx", "joiner"),
            keywords_file=str(keywords_file),
            num_threads=cfg.num_threads,
            keywords_score=cfg.keywords_score,
            keywords_threshold=cfg.keywords_threshold,
            provider="cpu",
        )
        self._stream = self._spotter.create_stream()

        words = [
            line.split("@")[-1].strip()
            for line in keywords_file.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#") and "@" in line
        ]
        logger.info(
            "唤醒词检测器就绪 | 唤醒词={} | 阈值={}",
            "、".join(words) or "未配置",
            cfg.keywords_threshold,
        )

    def listen_once(self) -> str:
        """阻塞等待，直到听到唤醒词。返回命中的关键词。"""
        import sounddevice as sd

        cfg = self.settings.audio
        block_size = int(_BLOCK_SECONDS * cfg.sample_rate)
        logger.info("待机中，等待唤醒词...")

        with sd.InputStream(
            samplerate=cfg.sample_rate,
            channels=cfg.channels,
            dtype="float32",
            device=cfg.mic_device,
        ) as stream:
            while True:
                chunk, _ = stream.read(block_size)
                samples = np.asarray(chunk, dtype=np.float32).flatten()

                self._stream.accept_waveform(cfg.sample_rate, samples)
                while self._spotter.is_ready(self._stream):
                    self._spotter.decode_stream(self._stream)
                result = self._spotter.get_result(self._stream)
                if result:
                    self._spotter.reset_stream(self._stream)
                    logger.info("听到唤醒词：{}", result)
                    return result