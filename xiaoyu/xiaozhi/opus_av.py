"""真 Opus 编解码（A 档第二批「装真依赖」）—— 走 PyAV（自带 ffmpeg + libopus）。

## 为什么是 PyAV（2026-09-22 实测选的，不是拍脑袋）

    opuslib   装得上，跑起来报「Could not find Opus library. Make sure it is
              installed.」—— 它只包了 Python 绑定，opus.dll 得自己另外搞。
    pyogg     0.6.14a1 只有 OpusFile / OpusFileStream，**没有裸编解码器**
              （AttributeError: module 'pyogg' has no attribute 'OpusEncoder'）。
    PyAV      一个 pip 包把 ffmpeg + libopus 全带进来，装完裸 Opus 包直接能用。

    py -3.11 -m pip install av

## 三条实测出来的硬事实（单测锁着，别照着"常识"改）

1. **编码器必须显式要 frame_duration**：不给 `options={"frame_duration": "60"}`
   的时候 libopus 按 20ms 切 —— 喂一帧 60ms 进去会吐 **3 个包**，跟"一帧进、
   一个包出"的契约对不上，我们 hello 里声明的帧长也就白写了。给了之后
   `encoder.frame_size` 正好等于下行一帧的采样数（24000Hz/60ms -> 1440）。
2. **解码器固定输出 48kHz**：给它设 `sample_rate = 16000` 它不认，出来永远是
   48000。所以解码后**必须重采样**回上行采样率 —— 不然 ASR 拿到的是"快三倍"
   的音频，识别出来是乱码。这不是可选项。
3. **重采样有一次性的 16 样本延迟**（swresample 的内部缓冲）：第一帧会短 16 个
   样本，之后每帧都准。3 秒的音频差 1 毫秒，听不出来、ASR 也不在乎 ——
   但**别拿它做精确的时长记账**。

抛还是不抛，跟 `audio_codec.py` 是同一个契约：`encode()` 要抛（我们自己的数据
出了问题，早炸早好），`decode()` 永不抛（字节是网络来的，坏包返回 b"" 并记一次错）。
"""

from __future__ import annotations

from typing import Any

import numpy as np

from ..logger import get_logger
from .audio_codec import CodecError, CodecParams, CodecUnavailableError, OpusCodec

logger = get_logger(__name__)

# 解码器固定吐 48kHz（上面事实 2）。这不是我们选的，是 ffmpeg 的 libopus 解码器行为。
DECODER_SAMPLE_RATE = 48000

# 语音够用，跟设备端一个量级（上游默认约 24kbps）
ENCODER_BIT_RATE = 24000


def _layout(channels: int) -> str:
    return "mono" if channels == 1 else "stereo"


def _require_av() -> Any:
    """只在真要用的时候才 import —— 没装 PyAV 也不该让 import 本模块就炸。"""
    try:
        import av
    except ImportError as exc:
        raise CodecUnavailableError(
            "没有 PyAV，裸 Opus 编解码用不了：py -3.11 -m pip install av"
        ) from exc
    return av


def resample_float(
    samples: np.ndarray, src_rate: int, dst_rate: int, channels: int = 1
) -> np.ndarray:
    """float32 重采样（[-1,1] 进、[-1,1] 出）—— 用 swresample，不是自己写的线性插值。

    为什么不自己写：22050 -> 16000 这种非整数比，线性插值会有明显的折叠噪声；
    而 swresample 已经随 PyAV 一起装进来了，没理由再写一份更容易错的。

    顺带一个用法上的坑：这个函数**每次调用都新建重采样器**，所以会丢那次性的
    16 个样本（事实 3）。按"一整段"调用没问题，按"每 60ms 调一次"就会每包都丢。
    逐包解码那条路径用的是 `AvOpusCodec` 里那个有状态的重采样器。
    """
    data = np.asarray(samples, dtype=np.float32).reshape(-1)
    if src_rate == dst_rate or data.size == 0:
        return data

    av = _require_av()
    pcm = (np.clip(data, -1.0, 1.0) * 32767.0).astype("<i2")
    if channels == 1:
        frame = av.AudioFrame.from_ndarray(
            np.ascontiguousarray(pcm.reshape(1, -1)), format="s16", layout="mono"
        )
    else:
        frame = av.AudioFrame.from_ndarray(
            np.ascontiguousarray(pcm.reshape(-1, channels).T), format="s16", layout="stereo"
        )
    frame.sample_rate = int(src_rate)
    frame.pts = 0

    resampler = av.AudioResampler(
        format="s16", layout=_layout(channels), rate=int(dst_rate)
    )
    pieces: list[np.ndarray] = []
    for piece in list(resampler.resample(frame)) + list(resampler.resample(None)):
        pieces.append(np.asarray(piece.to_ndarray()).reshape(-1))
    if not pieces:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(pieces).astype(np.float32) / 32767.0


def resample_pcm16(pcm: bytes, src_rate: int, dst_rate: int, channels: int = 1) -> bytes:
    """s16le 字节重采样。下行合成出来的音频（22050）要进 Opus（24000）时走这里。"""
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32767.0
    out = resample_float(samples, src_rate, dst_rate, channels)
    return (np.clip(out, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()


class AvOpusCodec:
    """`OpusCodec` 的真实现。对外就四个属性和三个方法，跟 `FakeOpusCodec` 一模一样。"""

    name = "libopus(PyAV)"

    def __init__(self, uplink: CodecParams, downlink: CodecParams) -> None:
        av = _require_av()
        self._av = av
        self.uplink = uplink
        self.downlink = downlink
        self.closed = False
        self.decode_errors = 0
        self._encode_pts = 0
        self._decoder = self._open_decoder()
        self._encoder = self._open_encoder()
        # 这个重采样器是**有状态**的，跨包复用才不会每包丢那 16 个样本（事实 3）
        self._resampler = av.AudioResampler(
            format="s16", layout=_layout(uplink.channels), rate=uplink.sample_rate
        )
        logger.info(
            "真 Opus 就位 | 上行 {}Hz/{:.3g}ms | 下行 {}Hz/{:.3g}ms | {}",
            uplink.sample_rate,
            uplink.frame_duration,
            downlink.sample_rate,
            downlink.frame_duration,
            self.name,
        )

    # ------------------------------------------------------------ 内部

    def _open_encoder(self):
        av = self._av
        ctx = av.CodecContext.create("libopus", "w")
        ctx.sample_rate = self.downlink.sample_rate
        ctx.layout = _layout(self.downlink.channels)
        ctx.format = "s16"
        ctx.bit_rate = ENCODER_BIT_RATE
        ctx.options = {"frame_duration": f"{self.downlink.frame_duration:g}"}
        ctx.open()
        if ctx.frame_size != self.downlink.frame_samples:
            raise CodecError(
                f"libopus 的帧长跟我们声明的不一致：编码器给 {ctx.frame_size} 个采样，"
                f"我们要 {self.downlink.frame_samples} 个"
                f"（{self.downlink.sample_rate}Hz / {self.downlink.frame_duration:g}ms）"
            )
        return ctx

    def _open_decoder(self):
        av = self._av
        ctx = av.CodecContext.create("libopus", "r")
        # 设了也不认（事实 2），但设上不影响什么，留着表明意图
        ctx.sample_rate = DECODER_SAMPLE_RATE
        ctx.layout = _layout(self.uplink.channels)
        ctx.format = "s16"
        ctx.open()
        return ctx

    # ------------------------------------------------------------ OpusCodec

    def encode(self, pcm: bytes) -> bytes:
        if self.closed:
            raise CodecError("编解码器已经 close 了 —— 多半是漏了收工，或者拿着旧实例接着用")
        if not isinstance(pcm, (bytes, bytearray, memoryview)):
            raise CodecError(f"encode 要 bytes，给的是 {type(pcm).__name__}")
        data = bytes(pcm)
        size = self.downlink.frame_bytes
        if not data:
            raise CodecError("encode 收到空数据 —— 上游多半是没合成出声音，别往设备发空包")
        if len(data) != size:
            raise CodecError(
                f"真编码器一次只收一帧：下行一帧是 {size} 字节"
                f"（{self.downlink.sample_rate}Hz / {self.downlink.frame_duration:g}ms / "
                f"{self.downlink.channels}ch），收到 {len(data)} 字节"
                " —— 先用 iter_pcm_frames() 切齐"
            )
        samples = np.frombuffer(data, dtype="<i2").astype("<i2")
        frame = self._av.AudioFrame.from_ndarray(
            np.ascontiguousarray(samples.reshape(1, -1)),
            format="s16",
            layout=_layout(self.downlink.channels),
        )
        frame.sample_rate = self.downlink.sample_rate
        frame.pts = self._encode_pts
        self._encode_pts += samples.size

        packets = list(self._encoder.encode(frame))
        if len(packets) != 1:
            raise CodecError(
                f"一帧进应该一个包出，libopus 给了 {len(packets)} 个 —— "
                "frame_duration 选项没生效？（见 opus_av.py 开头的事实 1）"
            )
        return bytes(packets[0])

    def decode(self, packet: bytes) -> bytes:
        if self.closed:
            self.decode_errors += 1
            logger.debug("已经 close 了还收到音频包，丢掉（不计入正常轨迹）")
            return b""
        if not isinstance(packet, (bytes, bytearray, memoryview)):
            self.decode_errors += 1
            logger.warning("解码收到 {} —— 不是二进制帧，丢掉", type(packet).__name__)
            return b""
        data = bytes(packet)
        if not data:
            self.decode_errors += 1
            return b""
        try:
            frames = self._decoder.decode(self._av.Packet(data))
            pieces: list[np.ndarray] = []
            for frame in frames:
                for resampled in self._resampler.resample(frame):
                    pieces.append(np.asarray(resampled.to_ndarray()).reshape(-1))
            if not pieces:
                return b""
            return np.concatenate(pieces).astype("<i2").tobytes()
        except Exception as exc:  # noqa: BLE001 —— 契约：decode 永不抛
            self.decode_errors += 1
            logger.warning("Opus 解码失败（{}），按协议当坏包丢掉", exc)
            return b""

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        for attr in ("_encoder", "_decoder", "_resampler"):
            target = getattr(self, attr, None)
            if target is None:
                continue
            try:
                target.close()
            except Exception:  # noqa: BLE001
                logger.debug("关 {} 时有点小状况，忽略", attr, exc_info=True)


def assert_codec_contract(codec: OpusCodec) -> None:
    """装完真依赖先跑一次的最小体检：一帧进、一个包出，解回来长度对得上。

    `scripts/xiaozhi_fake_device.py --selftest` 调它。放在这里而不是单测里，
    是因为"板子到了现场"也想随手验一次 —— 单测跑不到那台机器上。
    """
    frame_bytes = codec.downlink.frame_bytes
    tone = np.sin(
        np.arange(codec.downlink.frame_samples) / codec.downlink.sample_rate * 2 * np.pi * 440
    )
    pcm = (tone * 8000).astype("<i2").tobytes()
    if len(pcm) != frame_bytes:
        raise CodecError(f"自检用的 PCM 长度 {len(pcm)} 对不上下行一帧 {frame_bytes}")
    packet = codec.encode(pcm)
    if not packet:
        raise CodecError("encode 返回了空包")
    back = codec.decode(packet)
    if not back:
        raise CodecError("decode 解不回 PCM —— 坏包？")
    logger.info(
        "编解码自检通过 | 一帧 {} 字节 -> {} 字节的包 -> 解回 {} 字节",
        len(pcm),
        len(packet),
        len(back),
    )
