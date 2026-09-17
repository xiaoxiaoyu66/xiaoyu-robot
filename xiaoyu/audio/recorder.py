"""录音 + VAD 断句。

VAD = Voice Activity Detection，判断"你说完了没有"。
这里用的是最朴素的能量检测版：连续静音超过 silence_seconds 就算说完。
够用，且没有额外依赖；以后想升级可以换成 silero-vad。
"""

from __future__ import annotations

import numpy as np
import sounddevice as sd

from ..config import AudioConfig
from ..logger import get_logger

logger = get_logger(__name__)


def list_devices() -> str:
    """返回可读的音频设备清单，排查问题时用。"""
    devices = sd.query_devices()
    lines = []
    for index, dev in enumerate(devices):
        kind = []
        if dev["max_input_channels"] > 0:
            kind.append("输入")
        if dev["max_output_channels"] > 0:
            kind.append("输出")
        lines.append(
            f"  [{index:>2}] {dev['name']}  ({'/'.join(kind) or '无'}, "
            f"默认采样率 {int(dev['default_samplerate'])})"
        )
    return "\n".join(lines)


class Recorder:
    """麦克风录音。"""

    def __init__(self, config: AudioConfig) -> None:
        self.config = config
        logger.debug(
            "录音器就绪 | 采样率={} | 声道={} | 设备={}",
            config.sample_rate,
            config.channels,
            config.mic_device if config.mic_device is not None else "系统默认",
        )

    def record_until_silence(self) -> np.ndarray:
        """录到你说完为止，返回 float32 单声道波形。"""
        cfg = self.config
        block = 0.1                       # 每次读 100 毫秒
        block_size = int(block * cfg.sample_rate)
        frames: list[np.ndarray] = []
        silent_for = 0.0
        total = 0.0
        heard_speech = False

        logger.info("开始聆听...")
        with sd.InputStream(
            samplerate=cfg.sample_rate,
            channels=cfg.channels,
            dtype="float32",
            device=cfg.mic_device,
        ) as stream:
            while total < cfg.max_record_seconds:
                chunk, _ = stream.read(block_size)
                frames.append(chunk.copy())
                total += block

                level = float(np.abs(chunk).mean())
                if level < cfg.silence_threshold:
                    silent_for += block
                    if heard_speech and silent_for >= cfg.silence_seconds:
                        break
                else:
                    if not heard_speech:
                        logger.debug("检测到人声，开始录音 | 音量={:.4f}", level)
                    heard_speech = True
                    silent_for = 0.0

        audio = (
            np.concatenate(frames).flatten().astype(np.float32)
            if frames
            else np.zeros(0, dtype=np.float32)
        )

        if not heard_speech:
            logger.warning("没有检测到人声（可能是麦克风太远或静音阈值太高）")
        else:
            logger.info("录音结束 | 时长={:.2f} 秒 | 峰值={:.3f}", total, float(np.abs(audio).max()))
        return audio

    def record_fixed(self, seconds: float = 3.0) -> np.ndarray:
        """录固定时长，S0 自检用。"""
        frames = int(seconds * self.config.sample_rate)
        logger.info("录音 {:.1f} 秒...", seconds)
        audio = sd.rec(
            frames,
            samplerate=self.config.sample_rate,
            channels=self.config.channels,
            dtype="float32",
            device=self.config.mic_device,
        )
        sd.wait()
        logger.info("录音完成 | 峰值={:.3f}", float(np.abs(audio).max()))
        return np.asarray(audio, dtype=np.float32).flatten()