"""唤醒词：sherpa-onnx 关键词检测（KWS）。

为什么选它：
    - 完全免费、离线、不用注册账号
    - 支持中文，唤醒词写在 config/keywords.txt 里，改词不用重新训练模型
    - 模型只有 33MB，CPU 常驻几乎不吃资源

【重要】唤醒词的写法
    这个模型的词表是"音素"级的，中文用「声母 + 带声调韵母」表示，
    不是直接写汉字。写汉字会让 sherpa-onnx 在 C++ 层报错退出，
    连 Python 异常都抓不到。

        错：  小柚子 :2.5 #0.5 @小柚子
        对：  x iǎo y ǔ :2.5 #0.5 @小柚子

    别手写拼音，用脚本生成：
        python scripts\\make_keywords.py 小柚子 --write

    格式：  音素序列 :阈值 #增强 @显示名
    阈值越大越难唤醒（越不容易误触发），一般 2.0 ~ 4.0

选型与校验的纯逻辑在 models.py，这个文件只管"跑起来"。

官方参考实现（卡住时对照）：
    sherpa-onnx 仓库 / python-api-examples / keyword-spotter-from-microphone.py
"""

from __future__ import annotations

import numpy as np

from ..audio.devices import resolve_device
from ..config import Settings
from ..logger import get_logger
from .models import load_vocab, pick_model_triple, prepare_keywords_file

logger = get_logger(__name__)

_BLOCK_SECONDS = 0.1


class WakeWordDetector:
    """常驻监听唤醒词。"""

    def __init__(self, settings: Settings) -> None:
        import sherpa_onnx

        self.settings = settings
        paths = settings.paths
        cfg = settings.wake

        encoder, decoder, joiner = pick_model_triple(paths.kws)
        tokens_file = paths.kws / "tokens.txt"
        if not tokens_file.exists():
            raise FileNotFoundError(f"唤醒模型缺少 tokens.txt：{tokens_file}")

        vocab = load_vocab(tokens_file)
        keywords_file, words = prepare_keywords_file(paths.keywords, vocab, paths.data)

        self._spotter = sherpa_onnx.KeywordSpotter(
            tokens=str(tokens_file),
            encoder=str(encoder),
            decoder=str(decoder),
            joiner=str(joiner),
            keywords_file=str(keywords_file),
            num_threads=cfg.num_threads,
            keywords_score=cfg.keywords_score,
            keywords_threshold=cfg.keywords_threshold,
            provider="cpu",
        )
        self._stream = self._spotter.create_stream()

        logger.info(
            "唤醒词检测器就绪 | 唤醒词={} | 阈值={} | 模型={}",
            "、".join(words),
            cfg.keywords_threshold,
            encoder.name,
        )

    def feed(self, samples: np.ndarray) -> str | None:
        """喂一段波形，命中唤醒词就返回它，否则返回 None。"""
        self._stream.accept_waveform(self.settings.audio.sample_rate, samples)
        while self._spotter.is_ready(self._stream):
            self._spotter.decode_stream(self._stream)
        result = self._spotter.get_result(self._stream)
        if result:
            self._spotter.reset_stream(self._stream)
            logger.info("听到唤醒词：{}", result)
            # 结构化事件行：误唤醒率统计（--wake-report）扫的就是它。
            # 格式别乱改，统计靠 "WAKE_EVENT |" 前缀 + ts 字段。
            import time as _time

            logger.info(
                "WAKE_EVENT | ts={} | keyword={}",
                _time.strftime("%Y-%m-%d %H:%M:%S"),
                result,
            )
            return result
        return None

    def listen_once(self) -> str:
        """阻塞等待，直到听到唤醒词。返回命中的关键词。"""
        import sounddevice as sd

        cfg = self.settings.audio
        block_size = int(_BLOCK_SECONDS * cfg.sample_rate)
        device, description = resolve_device(
            cfg.mic_device, cfg.mic_device_name, output=False
        )
        logger.debug("唤醒监听输入设备 | {}", description)
        logger.info("待机中，等待唤醒词...")

        with sd.InputStream(
            samplerate=cfg.sample_rate,
            channels=cfg.channels,
            dtype="float32",
            device=device,
        ) as stream:
            while True:
                chunk, _ = stream.read(block_size)
                hit = self.feed(np.asarray(chunk, dtype=np.float32).flatten())
                if hit:
                    return hit