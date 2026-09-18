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

from .devices import resolve_device

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
        self._device, description = resolve_device(
            config.mic_device, config.mic_device_name, output=False
        )
        logger.debug(
            "录音器就绪 | 采样率={} | 声道={} | 设备={}",
            config.sample_rate,
            config.channels,
            description,
        )

    def calibrate_noise_floor(self, seconds: float = 0.3) -> float:
        """开机时采一段环境噪声，把静音阈值改成"噪声底 × 系数"（S5.5）。

        固定阈值 0.015 是笔记本上手调的值 —— N100 挂的 USB 麦噪声底完全不同，
        搬过去大概率直接失效。自适应之后，安静房间和有点底噪的房间都能用。

        返回校准后的阈值。校准失败（麦克风被占）保留配置里的默认值。
        """
        cfg = self.config
        try:
            audio = self.record_fixed(seconds)
        except Exception:
            logger.warning("噪声底校准失败，沿用配置阈值 {}", cfg.silence_threshold)
            return cfg.silence_threshold

        floor = float(np.sqrt(np.mean(audio**2)))  # RMS 比峰值更能代表"底噪"
        adapted = min(max(floor * 4.0, 0.008), 0.08)
        original = cfg.silence_threshold
        # AudioConfig 是 frozen dataclass：用 object.__setattr__ 改这一份实例
        object.__setattr__(cfg, "silence_threshold", adapted)
        logger.info(
            "噪声底校准完成 | 底噪 RMS={:.4f} -> 静音阈值 {:.4f}（原为 {:.4f}）",
            floor, adapted, original,
        )
        return adapted

    def contains_speech(self, audio: np.ndarray, model_path: str | None = None) -> bool:
        """这段录音里有没有人说话（VAD 决断，S5.5）。

        优先用 silero-vad（模型已在 models/ 下，sherpa-onnx 自带接口）；
        模型缺失/推理失败时退回老的振幅启发式 —— 那本来就是现状，不会更糟。
        """
        if audio.size == 0:
            return False
        if model_path:
            try:
                return self._silero_has_speech(audio, model_path)
            except Exception:
                logger.warning("silero-vad 推理失败，退回振幅判断", exc_info=True)
        return bool(float(np.abs(audio).max()) >= 1e-4)

    @staticmethod
    def _silero_has_speech(audio: np.ndarray, model_path: str) -> bool:
        import sherpa_onnx

        vad = sherpa_onnx.Vad(
            model=model_path,
            threshold=0.5,
            min_silence_duration=0.25,
            min_speech_duration=0.1,
            window_size=512,
        )
        samples = np.asarray(audio, dtype=np.float32)
        for start in range(0, len(samples), 512):
            vad.accept_waveform(samples[start : start + 512])
            if not vad.is_empty():
                return True
        vad.flush()
        return not vad.is_empty()

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
            device=self._device,
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
            device=self._device,
        )
        sd.wait()
        logger.info("录音完成 | 峰值={:.3f}", float(np.abs(audio).max()))
        return np.asarray(audio, dtype=np.float32).flatten()