"""播放：把音频放出来。

用 soundfile 解码 + sounddevice 输出。

为什么不继续用 pygame：
    pygame 的混音器自己挑输出设备，**没法跟着 .env 里的设置走**。
    实测踩到的坑：当系统默认输出是显示器的 HDMI 音频时，
    TTS 的声音全进了显示器，笔记本扬声器一点动静都没有，而且非常难查。
    sounddevice 可以直接指定设备编号，和录音走同一套配置。

设备怎么挑（编号会漂移这件事）见 xiaoyu/audio/devices.py 的说明。

S5 改造（2026-09-18）：sd.play 换成 OutputStream 分块写。两个目的：
1. 每 50ms 一块算 RMS 推给表情脸当口型（mouth.level）；
2. 块与块之间都能响应 stop() —— 触屏打断要能立刻停，
   不能等整句放完（旧版 sd.play 中途停不下来）。
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path

import numpy as np
import sounddevice as sd
import soundfile as sf

from ..config import AudioConfig
from ..logger import get_logger
from . import sfx
from .devices import resolve_device

logger = get_logger(__name__)

__all__ = ["Speaker", "resolve_device", "mouth_level", "MOUTH_FRAME_SECONDS"]

# 口型帧长：50ms 一块，正好对应表情脸 mouth.level 的一帧（v3 §3.1）
MOUTH_FRAME_SECONDS = 0.05


def mouth_level(chunk: np.ndarray) -> float:
    """把一块音频折成一个 0~1 的口型开度。

    用 RMS 音量再开平方压缩动态：正常说话的 RMS 大约 0.05~0.25，
    直接当开度太小声看不见动；平方根一下，小声也张得开嘴。

    纯函数，方便单测：空块 / NaN 一律返回 0，不会出现负数或非法值。
    """
    if chunk.size == 0:
        return 0.0
    rms = float(np.sqrt(np.mean(np.square(chunk, dtype=np.float64))))
    if not np.isfinite(rms) or rms <= 0:
        return 0.0
    return float(np.clip(np.sqrt(rms) * 2.2, 0.0, 1.0))


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
        self._stop_event = threading.Event()
        self._playing = False
        self._level_listeners: list[Callable[[float], None]] = []

    @property
    def device(self) -> int | None:
        return self._device

    def on_level(self, callback: Callable[[float], None]) -> None:
        """注册音量包络监听器（表情脸接嘴型用）。

        每写出去一块音频回调一次（约 50ms 一帧）。
        回调必须又快又不抛异常 —— 它在播放热路径上，卡一下就是声音卡顿。
        """
        self._level_listeners.append(callback)

    def is_playing(self) -> bool:
        """当前是否在放声音。"""
        return self._playing

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
        """播放内存里的波形。

        内部按 50ms 一块写进 OutputStream，块与块之间检查 stop()。
        wait=False 时放到守护线程里播（几乎用不到，保留是为了兼容旧调用）。
        """
        if audio.size == 0:
            logger.warning("待播放的音频是空的")
            return

        if not wait:
            threading.Thread(
                target=self._play_blocking,
                args=(audio, samplerate),
                name="xiaoyu-audio-bg",
                daemon=True,
            ).start()
            return
        self._play_blocking(audio, samplerate)

    def _play_blocking(self, audio: np.ndarray, samplerate: int | None) -> None:
        samplerate = samplerate or self.config.sample_rate
        blocksize = max(1, int(samplerate * MOUTH_FRAME_SECONDS))
        self._stop_event.clear()
        self._playing = True
        stopped_early = False
        try:
            with sd.OutputStream(
                samplerate=samplerate,
                device=self._device,
                channels=1,
                dtype="float32",
                blocksize=blocksize,
            ) as stream:
                for start in range(0, len(audio), blocksize):
                    if self._stop_event.is_set():
                        stopped_early = True
                        logger.info("播放被 stop() 打断")
                        break
                    chunk = audio[start : start + blocksize]
                    stream.write(chunk)
                    self._emit_level(mouth_level(chunk))
        except Exception:
            logger.exception(
                "播放失败，设备 {} 可能不可用。跑 python scripts\\diagnose_audio.py 找一个能出声的设备",
                self._device,
            )
        finally:
            # 关流会立刻停掉声卡缓冲里的余音；OutputStream 退出即停
            self._playing = False
            self._emit_level(0.0)
        if stopped_early:
            logger.debug("本次播放被中断，剩余音频已丢弃")

    def _emit_level(self, level: float) -> None:
        for callback in self._level_listeners:
            try:
                callback(level)
            except Exception:
                logger.exception("音量监听器执行失败")

    def play_cue(self, kind: str = sfx.ACK) -> None:
        """播一声提示音（"听到了" / "结束了" / "出错了"）。

        唤醒命中后要**立刻**播一声 —— 那一刻人还听不到任何"它在思考"的信号，
        而后面还要等识别和大模型好几秒。一声短音就是最省事的"我听见了"。

        提示音只是锦上添花，出了任何问题都不该影响主流程，所以整段吞掉异常。
        """
        try:
            self.play_array(sfx.make_cue(kind, self.config.sample_rate))
        except Exception:
            logger.debug("提示音没播出来，不影响主流程", exc_info=True)

    def stop(self) -> None:
        """立刻停掉当前播放（触屏打断的入口）。

        已经没在放的时候调用无副作用；下一次 play_array 会自行清掉标记。
        """
        self._stop_event.set()
        sd.stop()
        logger.debug("已请求停止播放")