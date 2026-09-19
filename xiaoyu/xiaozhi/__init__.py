"""小智设备协议适配层（A 档）：把我们的大脑接到 ESP32 板子上。

分工（对应 docs/xiaozhi拆解.md §6 的动手顺序）：

    protocol.py     纯函数：帧类型判定 / JSON 编解码 / hello 的解析与构造
    session.py      会话状态机（纯逻辑，不碰网络）
    audio_codec.py  Opus 编解码抽象 + 假实现（真实现＝装依赖那一步）
    server.py       最小服务端：能连上、能收发、能优雅收工 —— 还没写

协议原文：上游 `78/xiaozhi-esp32` 的 `docs/websocket_zh.md`；
真样本 fixture 在 `tests/fixtures/xiaozhi/`（逐字抄自那份文档，不是编的）。

这一层**不 import websockets、也不 import opuslib**：纯逻辑，测试环境不装任何
东西也能跑（和 `xiaoyu/face/protocol.py` 同一个理由）。真编解码器只从
`create_opus_codec()` 进来 —— 那是唯一的接线点。
"""

from .audio_codec import (
    BYTES_PER_SAMPLE,
    SUPPORTED_FRAME_DURATIONS,
    SUPPORTED_SAMPLE_RATES,
    CodecError,
    CodecFormatError,
    CodecParams,
    CodecUnavailableError,
    FakeOpusCodec,
    OpusCodec,
    bytes_per_frame,
    check_channels,
    check_frame_duration,
    check_sample_rate,
    create_opus_codec,
    iter_pcm_frames,
    needs_resample,
    pcm_duration_seconds,
    samples_per_frame,
)
from .protocol import (
    DEFAULT_FORMAT,
    DEFAULT_SERVER_SAMPLE_RATE,
    SUPPORTED_PROTOCOL_VERSIONS,
    TRANSPORT_WEBSOCKET,
    AudioParams,
    DeviceHello,
    FrameKind,
    ParseResult,
    ProtocolError,
    build_server_hello,
    classify_frame,
    decode_message,
    encode_message,
    parse_audio_params,
    parse_device_hello,
)
from .session import (
    HELLO_TIMEOUT_SECONDS,
    Action,
    ActionKind,
    Event,
    EventKind,
    Session,
    SessionState,
    event_from_frame,
)

__all__ = [
    "BYTES_PER_SAMPLE",
    "DEFAULT_FORMAT",
    "DEFAULT_SERVER_SAMPLE_RATE",
    "HELLO_TIMEOUT_SECONDS",
    "SUPPORTED_FRAME_DURATIONS",
    "SUPPORTED_PROTOCOL_VERSIONS",
    "SUPPORTED_SAMPLE_RATES",
    "Action",
    "ActionKind",
    "AudioParams",
    "CodecError",
    "CodecFormatError",
    "CodecParams",
    "CodecUnavailableError",
    "DeviceHello",
    "Event",
    "EventKind",
    "FakeOpusCodec",
    "FrameKind",
    "OpusCodec",
    "ParseResult",
    "ProtocolError",
    "Session",
    "SessionState",
    "build_server_hello",
    "bytes_per_frame",
    "check_channels",
    "check_frame_duration",
    "check_sample_rate",
    "classify_frame",
    "create_opus_codec",
    "decode_message",
    "encode_message",
    "event_from_frame",
    "iter_pcm_frames",
    "needs_resample",
    "parse_audio_params",
    "parse_device_hello",
    "pcm_duration_seconds",
    "samples_per_frame",
]
