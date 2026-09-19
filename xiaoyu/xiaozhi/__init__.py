"""小智设备协议适配层（A 档）：把我们的大脑接到 ESP32 板子上。

分工（对应 docs/xiaozhi拆解.md §6 的动手顺序）：

    protocol.py     纯函数：帧类型判定 / JSON 编解码 / hello 的解析与构造
    session.py      会话状态机（纯逻辑，不碰网络）        —— 还没写
    audio_codec.py  Opus 编解码抽象 + 假实现             —— 还没写
    server.py       最小服务端：能连上、能收发、能优雅收工 —— 还没写

协议原文：上游 `78/xiaozhi-esp32` 的 `docs/websocket_zh.md`；
真样本 fixture 在 `tests/fixtures/xiaozhi/`（逐字抄自那份文档，不是编的）。

这一层**不 import websockets**：纯逻辑，测试环境不装任何东西也能跑
（和 `xiaoyu/face/protocol.py` 同一个理由）。
"""

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

__all__ = [
    "DEFAULT_FORMAT",
    "DEFAULT_SERVER_SAMPLE_RATE",
    "SUPPORTED_PROTOCOL_VERSIONS",
    "TRANSPORT_WEBSOCKET",
    "AudioParams",
    "DeviceHello",
    "FrameKind",
    "ParseResult",
    "ProtocolError",
    "build_server_hello",
    "classify_frame",
    "decode_message",
    "encode_message",
    "parse_audio_params",
    "parse_device_hello",
]
