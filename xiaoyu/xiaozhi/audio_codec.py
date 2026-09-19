"""Opus 编解码的接口 + 假实现（A 档）。

配套：`protocol.py`（帧类型判定 / hello 解析），对应 `docs/xiaozhi拆解.md` §2.2.1 第 4 项。

## 这一层解决的问题

设备上行发 Opus（16kHz / 单声道 / 60ms 一帧），我们下行也得发 Opus（24kHz）。
但 Opus 库（`opuslib` / `pyogg` / 调 ffmpeg）**现在一个都没装** —— 板子还没到，
装它本身是单独一步（§2.2.1 原文：「真实现等装依赖时再换」）。

所以这里跟 `xiaoyu/body/servo.py` 一个套路：

    OpusCodec            协议（Protocol）—— 行为代码只认这三个方法，不认识任何库
    FakeOpusCodec        假实现 —— 原样透传 + 记账，不装任何东西也能跑通单测
    create_opus_codec()  唯一的接线点 —— 真实现将来只在**这一个函数**里挑

换真实现只动 `create_opus_codec()`。**别在 session.py / server.py 里 import
`opuslib`** —— 那样「换一块板子」就会污染到会话逻辑，这一层就白留了。

## 两个方向的参数不一样（文档写明，别搞混）

    上行  device_hello.audio_params   opus / 16000 / 1ch / 60ms   （§1.3）
    下行  我们的 server hello          opus / 24000 / 1ch / 60ms   （§8.3、§9.2）

下行发 24000 就行：§8.3 写明「如果服务器的音频采样率与设备不一致，
会在解码后再进行重采样」，设备自己会处理。

## Opus 自己的硬约束（RFC 6716）

采样率只收 8000 / 12000 / 16000 / 24000 / 48000；
帧长只收 2.5 / 5 / 10 / 20 / 40 / 60 ms。
**表外的参数一律当场报错，不许凑合着跑** —— 22050 的 PCM 硬喂进去，
出来的是变调的怪声，那种 bug 在真板子上要听半天才听得出来。

这不是理论问题：`xiaoyu/tts/engine.py` 里 edge-tts 出 24000（能用），
但 Matcha / Piper 出 **22050**（Opus 不收）—— 走那两个引擎必须
先重采样到 24000 或 48000。`needs_resample()` 就是查这个的。

## 抛还是不抛（跟 protocol.py 一样的双重契约）

- `decode()`（**设备来的字节**）永不抛：坏包返回 `b""` 并记一次错。
  上行是网络来的，抛出去等于把会话搞死。
- `encode()` 与 `CodecParams(...)`（**我们自己的参数 / 自己的数据**）要抛：
  参数不支持、帧长切不齐都是我们自己的 bug，早炸早好。

版本 1（我们唯一支持的版本）的二进制帧就是裸 Opus，没有包头（协议文档 §3.1），
所以这里进出都是"一个 Opus 包"，不存在解包头的活。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..logger import get_logger
from .protocol import (
    DEFAULT_FORMAT,
    DEFAULT_SERVER_FRAME_DURATION,
    DEFAULT_SERVER_SAMPLE_RATE,
    AudioParams,
)

logger = get_logger(__name__)

# ---------------- Opus 的硬约束（RFC 6716）----------------

# 采样率: Opus 只收这五个。22050（Matcha / Piper）和 32000 都**不在**表里。
SUPPORTED_SAMPLE_RATES: tuple[int, ...] = (8000, 12000, 16000, 24000, 48000)

# 帧长(ms): 2.5 / 5 / 10 / 20 / 40 / 60。注意 30ms 这种"看着挺合理"的数 **不收**。
SUPPORTED_FRAME_DURATIONS: tuple[float, ...] = (2.5, 5.0, 10.0, 20.0, 40.0, 60.0)

# 我们两端都用 16 位小端 PCM（s16le）—— 这是语音链路的惯例，也是 ffmpeg 的默认
BYTES_PER_SAMPLE = 2

DEFAULT_CHANNELS = 1

# 设备 hello 里一定有 frame_duration（§1.3 样本是 60）；万一缺了按这个补
DEFAULT_FRAME_DURATION_MS = 60.0


# ---------------- 异常 ----------------

class CodecError(Exception):
    """编解码这一层的基类。"""


class CodecUnavailableError(CodecError):
    """没有可用的真实实现（依赖还没装）。"""


class CodecFormatError(CodecError):
    """参数不是 Opus 能收的（采样率 / 帧长 / 声道 / format）。"""


# ---------------- 参数校验（纯函数，单测直接打）----------------

def check_channels(channels: object) -> int:
    """我们只做单声道和双声道 —— Opus 本身能更多，但协议里没这种用法。"""
    if not isinstance(channels, int) or isinstance(channels, bool):
        raise CodecFormatError(f"channels 得是整数，给的是 {channels!r}")
    if channels not in (1, 2):
        raise CodecFormatError(f"channels 只做 1 / 2，收到 {channels!r}")
    return channels


def check_sample_rate(sample_rate: object) -> int:
    """采样率必须在 Opus 收的那五个里面（RFC 6716）。"""
    if not isinstance(sample_rate, int) or isinstance(sample_rate, bool):
        raise CodecFormatError(f"采样率得是整数，给的是 {sample_rate!r}")
    if sample_rate not in SUPPORTED_SAMPLE_RATES:
        # 22050 是 Matcha / Piper 的输出，最容易踩的就是它 —— 单独提一句
        hint = (
            "（22050 是 Matcha / Piper 的默认输出 —— 先重采样到 24000 或 48000）"
            if sample_rate == 22050
            else ""
        )
        raise CodecFormatError(
            f"采样率 {sample_rate} Opus 不收，只支持 {SUPPORTED_SAMPLE_RATES}{hint}"
        )
    return sample_rate


def check_frame_duration(frame_duration: object) -> float:
    """帧长必须在 Opus 收的那六个里面。30ms 这种"合理但不存在"的值会在这里被拦下。"""
    if isinstance(frame_duration, bool) or not isinstance(frame_duration, (int, float)):
        raise CodecFormatError(f"帧长得是数字（毫秒），给的是 {frame_duration!r}")
    value = float(frame_duration)
    if value not in SUPPORTED_FRAME_DURATIONS:
        raise CodecFormatError(
            f"帧长 {value:g}ms Opus 不收，只支持 {SUPPORTED_FRAME_DURATIONS}"
        )
    return value


def samples_per_frame(sample_rate: int, frame_duration_ms: float) -> int:
    """一帧有多少个采样点（单声道）。16000Hz / 60ms -> 960。"""
    check_sample_rate(sample_rate)
    duration = check_frame_duration(frame_duration_ms)
    samples = sample_rate * duration / 1000.0
    if samples != int(samples):
        # 合法组合算出来永远是整数（表里的数都是这么挑的），走到这里说明表被改坏了
        raise CodecFormatError(f"{sample_rate}Hz / {duration:g}ms 算出来不是整数个采样点")
    return int(samples)


def bytes_per_frame(
    sample_rate: int,
    frame_duration_ms: float,
    channels: int = DEFAULT_CHANNELS,
    bytes_per_sample: int = BYTES_PER_SAMPLE,
) -> int:
    """一帧 PCM 有多少字节。16000Hz / 60ms / 单声道 / s16le -> 1920。"""
    check_channels(channels)
    if bytes_per_sample <= 0:
        raise CodecFormatError(f"bytes_per_sample 得是正数，收到 {bytes_per_sample!r}")
    return samples_per_frame(sample_rate, frame_duration_ms) * channels * bytes_per_sample


def pcm_duration_seconds(
    byte_count: int,
    sample_rate: int,
    channels: int = DEFAULT_CHANNELS,
    bytes_per_sample: int = BYTES_PER_SAMPLE,
) -> float:
    """一段 PCM 有多少秒。用来打日志、算"这话说了多久"，别拿它做时序控制。"""
    check_sample_rate(sample_rate)
    check_channels(channels)
    if byte_count < 0:
        raise CodecFormatError(f"字节数不能是负的，收到 {byte_count!r}")
    per_sample = channels * bytes_per_sample
    return byte_count / (sample_rate * per_sample) if per_sample else 0.0


def iter_pcm_frames(
    pcm: bytes,
    sample_rate: int,
    frame_duration_ms: float,
    channels: int = DEFAULT_CHANNELS,
    *,
    pad_tail: bool = True,
) -> Iterator[bytes]:
    """把一段 PCM 切成 Opus 要的整帧。

    TTS 出来的长度几乎不会正好是 60ms 的整数倍，所以默认**最后一片补零补满**：
    `pad_tail=False` 是丢掉尾巴 —— 别用，丢掉的是一小段真人声。

    例：16000Hz / 60ms 一帧 1920 字节；喂 2000 字节 -> 两片（第二片补 880 个零）。
    """
    size = bytes_per_frame(sample_rate, frame_duration_ms, channels)
    data = bytes(pcm)
    whole = len(data) - len(data) % size
    for start in range(0, whole, size):
        yield data[start : start + size]
    tail = data[whole:]
    if tail and pad_tail:
        yield tail + b"\x00" * (size - len(tail))


def needs_resample(sample_rate: int) -> bool:
    """这个采样率要重采样吗？True 表示**不能**直接喂给 Opus。

    22050（Matcha / Piper）-> True；16000 / 24000 / 48000 -> False。
    """
    return sample_rate not in SUPPORTED_SAMPLE_RATES


# ---------------- 接口 ----------------

@runtime_checkable
class OpusCodec(Protocol):
    """行为代码认的就是这个形状（和 `body.ServoBackend` 一个道理）。

    真实现照着写这三个方法就能接上，不用改 session.py / server.py 一行。
    """

    # 参数要能读出来：驱动层得按帧长切音频，不能自己再猜一份（猜两份就会不一致）
    uplink: CodecParams
    downlink: CodecParams

    def encode(self, pcm: bytes) -> bytes:
        """PCM(s16le) -> 一个 Opus 包。长度不是整帧要抛（那是我们自己的 bug）。"""
        ...

    def decode(self, packet: bytes) -> bytes:
        """一个 Opus 包 -> PCM(s16le)。**永不抛**：坏包返回 `b""`。"""
        ...

    def close(self) -> None:
        """收工，释放编解码器。要能重复调用。"""
        ...


# ---------------- 假实现 ----------------

class FakeOpusCodec:
    """假编解码器：**原样透传 + 记账**，不依赖任何库、不碰网络。

    为什么透传而不是瞎编一段 Opus：我们要验的是「上下行帧数对不对、字节数对不对、
    收工干不干净」，不是「Opus 压得准不准」—— 后者只有真库能验，属于装依赖那一步。
    透传还有个白给的好处：写回声服务端（§2.2.1 第 5 项）时，设备发来的音频能
    一字不差地回去，端到端测试才真的测到了链路。

    `reject_decode` 是可选的判据：返回 True 的包按坏包处理 —— 单测里制造
    「设备发了垃圾包」的场景用它，不用真造一个坏 Opus。跟 session.py 的
    `clock=` 是一回事：把不好造的东西做成可注入的。

    `encoded_packets` / `decoded_packets` 留全量轨迹（和 `FakeServo.history` 一样
    是给单测看的），所以**别在长跑里挂着它** —— 那是在攒内存。
    """

    def __init__(
        self,
        uplink: CodecParams,
        downlink: CodecParams,
        *,
        reject_decode: Callable[[bytes], bool] | None = None,
        name: str = "opus",
    ) -> None:
        self.name = name
        self.uplink = uplink
        self.downlink = downlink
        self.encoded_packets: list[bytes] = []
        self.decoded_packets: list[bytes] = []
        self.decode_errors = 0
        self.closed = False
        self._reject_decode = reject_decode
        logger.warning(
            "假 Opus 编解码器 {} 就位（上行 {}Hz / {:.3g}ms，下行 {}Hz / {:.3g}ms）"
            "—— 音频是原样透传的，别接真板子",
            name,
            uplink.sample_rate,
            uplink.frame_duration,
            downlink.sample_rate,
            downlink.frame_duration,
        )

    # ---- 记账 ----

    @property
    def encoded_bytes(self) -> int:
        return sum(len(p) for p in self.encoded_packets)

    @property
    def decoded_bytes(self) -> int:
        return sum(len(p) for p in self.decoded_packets)

    @property
    def sent_seconds(self) -> float:
        """已经"说"了多久（按上行帧长算的，透传时跟下行一样长）。"""
        return pcm_duration_seconds(
            self.encoded_bytes, self.downlink.sample_rate, self.downlink.channels
        )

    # ---- OpusCodec ----

    def encode(self, pcm: bytes) -> bytes:
        if self.closed:
            raise CodecError("编解码器已经 close 了 —— 多半是漏了收工，或者拿着旧实例接着用")
        if not isinstance(pcm, (bytes, bytearray, memoryview)):
            raise CodecError(f"encode 要 bytes，给的是 {type(pcm).__name__}")
        data = bytes(pcm)
        # encode 是**下行**（我们说话给设备听），所以按下行帧长校验 —— 别拿上行那个
        size = self.downlink.frame_bytes
        if not data:
            raise CodecError("encode 收到空数据 —— 上游多半是没合成出声音，别往设备发空包")
        if len(data) % size:
            raise CodecError(
                f"下行一帧是 {size} 字节（{self.downlink.sample_rate}Hz / "
                f"{self.downlink.frame_duration:g}ms / {self.downlink.channels}ch），"
                f"收到 {len(data)} 字节 —— 先用 iter_pcm_frames() 切齐"
            )
        self.encoded_packets.append(data)
        logger.debug("假编码：{} 字节 -> 1 个包（累计 {} 个）", len(data), len(self.encoded_packets))
        return data

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
        if self._reject_decode is not None and self._reject_decode(data):
            self.decode_errors += 1
            logger.warning("假解码：判据把这个 {} 字节的包判成坏包，按协议返回空 PCM", len(data))
            return b""
        self.decoded_packets.append(data)
        return data

    def close(self) -> None:
        if not self.closed:
            logger.debug(
                "假 Opus 编解码器 {} 收工（上行 {} 个包 / 下行 {} 个包 / 坏包 {} 个）",
                self.name,
                len(self.decoded_packets),
                len(self.encoded_packets),
                self.decode_errors,
            )
        self.closed = True


# ---------------- 参数对象 ----------------

@dataclass(frozen=True)
class CodecParams:
    """编解码要用到的参数。**构造即校验** —— 不合法的参数根本建不出来。

    比 `protocol.AudioParams` 多做一件事：「帧长必须有着落」。AudioParams 允许
    `frame_duration=None`（§9.2 那个 server hello 就没这个键，得让设备端能收），
    但切帧的时候必须有个确切的数，所以 `from_audio_params()` 负责补默认值。
    """

    sample_rate: int
    channels: int = DEFAULT_CHANNELS
    frame_duration: float = DEFAULT_FRAME_DURATION_MS
    format: str = DEFAULT_FORMAT

    def __post_init__(self) -> None:
        if self.format != DEFAULT_FORMAT:
            raise CodecFormatError(f"这一层只做 Opus，收到 format={self.format!r}")
        check_sample_rate(self.sample_rate)
        check_channels(self.channels)
        # 统一成 float，省得后面 int / float 混着比较出怪事
        object.__setattr__(self, "frame_duration", check_frame_duration(self.frame_duration))

    @classmethod
    def from_audio_params(
        cls,
        params: AudioParams,
        *,
        default_frame_duration: float = DEFAULT_FRAME_DURATION_MS,
    ) -> CodecParams:
        """把协议层的 `AudioParams` 补齐成本层的完整参数。"""
        frame_duration = (
            params.frame_duration
            if params.frame_duration is not None
            else default_frame_duration
        )
        return cls(
            sample_rate=params.sample_rate,
            channels=params.channels,
            frame_duration=frame_duration,
            format=params.format,
        )

    @property
    def frame_samples(self) -> int:
        """一帧多少个采样点。16000Hz / 60ms -> 960。"""
        return samples_per_frame(self.sample_rate, self.frame_duration)

    @property
    def frame_bytes(self) -> int:
        """一帧多少字节。16000Hz / 60ms / 单声道 / s16le -> 1920。"""
        return bytes_per_frame(self.sample_rate, self.frame_duration, self.channels)


# ---------------- 接线点 ----------------

def create_opus_codec(
    uplink: AudioParams,
    downlink: AudioParams | None = None,
    *,
    allow_fake: bool = False,
) -> OpusCodec:
    """把协议里的 audio_params 变成能用的编解码器 —— **唯一的接线点**。

    `allow_fake=False`（默认）在没有真实现时**必须报错**，不许悄悄把假货交出去：
    假货是原样透传的，接上真板子只会听到一段噪音，而且很难查。
    （跟 `XIAOYU_BODY_ENABLED` 默认关同一个规矩 —— 假东西要显式开。）

    装真依赖那一步做完之后，在这里加分支即可，其它文件一行不用动。
    """
    up = CodecParams.from_audio_params(uplink)
    down = (
        CodecParams.from_audio_params(
            downlink, default_frame_duration=DEFAULT_SERVER_FRAME_DURATION
        )
        if downlink is not None
        else CodecParams(
            sample_rate=DEFAULT_SERVER_SAMPLE_RATE,
            frame_duration=DEFAULT_SERVER_FRAME_DURATION,
        )
    )
    if allow_fake:
        return FakeOpusCodec(up, down)
    raise CodecUnavailableError(
        "还没有真实的 Opus 实现：opuslib / pyogg / ffmpeg 都还没装。"
        "docs/xiaozhi拆解.md §2.2.1 第 4 项把「装真依赖」单独列成一步了 —— "
        "装完立刻补最小单测，别跳过。离线测试请显式传 allow_fake=True。"
    )
