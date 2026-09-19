"""小智设备协议（A 档）—— 设备和我们的服务端之间唯一的契约。

配套：上游 `78/xiaozhi-esp32` 的 `docs/websocket_zh.md`（下称"协议文档"）；
真样本在 `tests/fixtures/xiaozhi/`（逐字抄自那份文档，含溯源表）。

这一层**只做纯函数**：不 import websockets、不碰硬件、不联网、不打日志 ——
测试环境什么都没装也能跑。网络在后面的 `server.py`，会话流程在 `session.py`。

本文件负责 4 件事（§2.2.1 第 2 项）：

    1. 帧类型判定 —— 文本帧 = JSON，二进制帧 = Opus 音频（协议文档 §1.5 / §3.1）
    2. 文本帧 JSON 的编解码
    3. 解析设备端 hello（§1.3 / §4.1.1）
    4. 构造服务端 hello（§1.4）

## 为什么不用 `face/protocol.py` 那种"失败返回 None"

`xiaoyu/face/protocol.py` 解析失败一律返回 None，因为脸是纯消费者、命令只有一条。
小智这边不一样：**设备连不上时它只会说"无法连接到服务"**，具体原因全在我们这侧。
所以这里解析失败返回 `ParseResult(error=ProtocolError(code, detail))`，把
"哪儿不对"（不是 JSON / 缺字段 / 版本不符 / transport 不是 websocket）带回来 ——
不然板子第一次连不上，就只能靠猜。

**解析永不抛异常**：任何输入（None、数字、坏字节、半截 JSON）都返回 ParseResult。
反过来，**构造是我们自己的代码在调用，参数错了就抛**（早失败）—— 见 `encode_message`
和 `build_server_hello`。这两条不矛盾：一个面对外部输入，一个面对自己的 bug。

## 版本号（文档里有两处，别被绕进去）

协议文档有两处 "version"：一是 hello 消息体内的 `version` 字段（§2 说它和
`Protocol-Version` 请求头一致），二是 §3 里"配置中的 version 字段"决定二进制协议
版本（1/2/3）。文档没说清是不是同一个数，**第一版按同一处理解**：只接受 `1`
（§3.1：直接发裸 Opus 数据、无额外元数据），2/3 明确报 `unsupported_version`。
板子到了抓一次真机 hello 对一遍，不一致以真机为准（同 fixture README 里那条）。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

# ---------------- 协议常量 ----------------

TRANSPORT_WEBSOCKET = "websocket"

# 第一版只认协议版本 1（裸 Opus）。理由见模块 docstring「版本号」那节。
SUPPORTED_PROTOCOL_VERSIONS = (1,)

DEFAULT_FORMAT = "opus"

# 下行采样率用 24000（协议文档 §8.3：「为了获得更好的音乐播放效果，服务器下行音频
# 可能使用 24000 采样率」）；设备上行是 16000，两边不一样是文档写明的。
DEFAULT_SERVER_SAMPLE_RATE = 24000

DEFAULT_SERVER_FRAME_DURATION = 60

# ---------------- 错误码 ----------------
# 放进 ProtocolError.code。调用方按这个分支，**别去匹配 detail 的文案** ——
# 文案是给人看的，随时可能改得更清楚。

ERR_BAD_INPUT = "bad_input"                  # 给进来的根本不是 str / bytes
ERR_NOT_JSON = "not_json"                    # 不是合法 JSON（含 bytes 解不成 utf-8）
ERR_NOT_OBJECT = "not_object"                # 是合法 JSON，但顶层不是对象
ERR_MISSING_TYPE = "missing_type"            # 缺 type，或 type 不是非空字符串（§8.6）
ERR_UNEXPECTED_TYPE = "unexpected_type"      # type 不是这一步要的那种
ERR_MISSING_FIELD = "missing_field"          # 必填字段不在
ERR_BAD_FIELD = "bad_field"                  # 字段在，但类型 / 取值不合法
ERR_BAD_TRANSPORT = "bad_transport"          # transport 不是 websocket
ERR_UNSUPPORTED_VERSION = "unsupported_version"


class FrameKind(Enum):
    """一条 WebSocket 消息是文本帧还是二进制帧（协议文档 §1.5）。"""

    TEXT = "text"        # 文本帧 = JSON（聊天 / TTS / STT / MCP 事件）
    BINARY = "binary"    # 二进制帧 = Opus 音频
    UNKNOWN = "unknown"  # 既不是 str 也不是 bytes —— 不该出现在协议里


@dataclass(frozen=True)
class ProtocolError:
    """解析失败的原因。code 给代码分支，detail 给人看。"""

    code: str
    detail: str

    def __str__(self) -> str:
        # 日志里直接 {} 它就能看，不用手拼
        return f"[{self.code}] {self.detail}"


@dataclass(frozen=True)
class ParseResult:
    """解析结果：成功给 value，失败给 error，**二选一**。

    这一层所有解析函数都返回它，永远不抛 —— 见模块 docstring。
    """

    value: Any = None
    error: ProtocolError | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def __bool__(self) -> bool:
        # 允许 `if result:` 这种写法；`if result.ok:` 等价，随调用方喜好
        return self.ok


@dataclass(frozen=True)
class AudioParams:
    """音频参数（协议文档 §1.3 / §1.4）。

    设备上行：opus / 16000 / 单声道 / 60ms 一帧；
    服务端下行：opus / 24000（§8.3）。

    `channels` / `frame_duration` 带默认值，是因为协议文档 §9.2 那个服务端 hello
    就只有 `format` + `sample_rate` 两个键 —— 说明这俩可以省。解析时不能因为它们
    缺席就报错（fixture `server/server_hello_16000.json` 就是这个形状）。
    """

    format: str
    sample_rate: int
    channels: int = 1
    frame_duration: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """回成线格式。`frame_duration` 没值就**不写这个键**（不是写 null）——
        设备端拿到 null 未必能受，干脆别给。"""
        out: dict[str, Any] = {
            "format": self.format,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
        }
        if self.frame_duration is not None:
            out["frame_duration"] = self.frame_duration
        return out


@dataclass(frozen=True)
class DeviceHello:
    """设备端握手消息（协议文档 §1.3 / §4.1.1）。

    `features` / `text_font` 是可选扩展（§1.3：features 按编译配置自动生成）。
    原样留着、不做解释 —— 这一层的职责是"别把设备说的话弄丢"，用不用是上层的事。
    """

    version: int
    transport: str
    audio_params: AudioParams
    features: Mapping[str, Any] = field(default_factory=dict)
    text_font: Mapping[str, Any] | None = None


def classify_frame(data: object) -> FrameKind:
    """判断这是文本帧还是二进制帧（协议文档 §1.5）。

    依据是 **WebSocket 的帧类型本身**：`str` = 文本 = JSON，`bytes` = 二进制 = Opus。
    不去猜内容 —— 拿 bytes 去 try 一下 json.loads 那种"聪明"做法会让一条恰好
    是合法 JSON 的音频帧被当成文本消息（反向更糟：把 JSON 当 Opus 喂给解码器）。

    空 bytes 仍然是**二进制帧**（它确实是二进制帧，只是里面没内容）；
    "这帧内容合不合法"不归这里管，那是 `audio_codec.py` 的事。
    """
    if isinstance(data, str):
        return FrameKind.TEXT
    if isinstance(data, (bytes, bytearray, memoryview)):
        return FrameKind.BINARY
    return FrameKind.UNKNOWN


def _as_text(raw: object) -> tuple[str | None, ProtocolError | None]:
    """把 str / bytes 统一成 str；不行就给错误（不抛）。"""
    if isinstance(raw, str):
        return raw, None
    if isinstance(raw, (bytes, bytearray, memoryview)):
        try:
            return bytes(raw).decode("utf-8"), None
        except UnicodeDecodeError as exc:
            return None, ProtocolError(
                ERR_NOT_JSON, f"bytes 解不成 utf-8，更不可能是 JSON：{exc}"
            )
    return None, ProtocolError(
        ERR_BAD_INPUT, f"要 str 或 bytes，拿到 {type(raw).__name__}"
    )


def decode_message(raw: object) -> ParseResult:
    """文本帧 -> 消息 dict。**任何输入都返回 ParseResult，不抛。**

    要过三关：

    1. 是 str / bytes，且 bytes 能按 utf-8 解开；
    2. 是合法 JSON，且顶层是个**对象**（数组 / 数字 / 字符串都挡掉 —— 协议文档
       §4 说得很清楚，文本帧传的是带 `type` 的 JSON 对象）；
    3. 有**非空 `type`** —— §8.6：缺 `type` 时设备端只记一条错误日志、不执行业务。
       我们这边同理：没有 type 的消息没有意义，早点挡掉比在业务里到处判空强。

    这里**不检查** `type` 是不是我们认识的值（hello / listen / abort / mcp …）——
    那是各调用方的事（比如 `parse_device_hello` 只认 hello）。这一层只管
    "这是不是一条格式合法的协议消息"。
    """
    text, err = _as_text(raw)
    if err is not None:
        return ParseResult(error=err)

    try:
        message = json.loads(text)
    except (ValueError, TypeError) as exc:
        return ParseResult(error=ProtocolError(ERR_NOT_JSON, f"不是合法 JSON：{exc}"))

    if not isinstance(message, dict):
        return ParseResult(
            error=ProtocolError(
                ERR_NOT_OBJECT, f"JSON 顶层要是对象，拿到 {type(message).__name__}"
            )
        )

    message_type = message.get("type")
    if not isinstance(message_type, str) or not message_type:
        return ParseResult(
            error=ProtocolError(
                ERR_MISSING_TYPE, "消息缺非空 type 字段（协议文档 §8.6）"
            )
        )

    return ParseResult(value=message)


def encode_message(message: Mapping[str, Any]) -> str:
    """消息 dict -> 文本帧内容（JSON 字符串）。

    **和解析不同，这里参数错了就抛**：调用方是我们自己的代码，不是外部输入，
    越早炸越好 —— 构造出一条设备看不懂的消息，比当场抛异常难查得多。
    所以要求 `message` 是 Mapping 且带非空 `type`（同 §8.6）；嵌套值不可序列化
    也会抛出 TypeError（`json` 自己抛的，不吞）。

    `ensure_ascii=False`：协议文档里的样本就是中文原样（"你好小明"、"自定义内容"），
    我们也照这个来 —— 中文不转成 \\uXXXX，日志和抓包都看得懂。
    """
    if not isinstance(message, Mapping):
        raise TypeError(f"message 要是 Mapping，拿到 {type(message).__name__}")
    message_type = message.get("type")
    if not isinstance(message_type, str) or not message_type:
        raise ValueError("message 缺非空 type 字段（协议文档 §8.6）")
    return json.dumps(dict(message), ensure_ascii=False)


def _require(obj: Mapping[str, Any], key: str, where: str) -> tuple[Any, ProtocolError | None]:
    """取必填字段。不在就给 missing_field（而不是 KeyError）。"""
    if key not in obj:
        return None, ProtocolError(ERR_MISSING_FIELD, f"{where} 缺 {key} 字段")
    return obj[key], None


def _as_positive_int(value: Any, key: str) -> tuple[int | None, ProtocolError | None]:
    """字段必须是正整数。"""
    # bool 是 int 的子类，得先挡掉 —— 否则 True 会被当成 1、False 当成 0 混进去
    if isinstance(value, bool) or not isinstance(value, int):
        return None, ProtocolError(
            ERR_BAD_FIELD, f"{key} 要是整数，拿到 {type(value).__name__}: {value!r}"
        )
    if value <= 0:
        return None, ProtocolError(ERR_BAD_FIELD, f"{key} 要是正数，拿到 {value!r}")
    return value, None


def parse_audio_params(raw: Any) -> ParseResult:
    """解析 `audio_params` -> `AudioParams`。

    必填：`format`（非空字符串）、`sample_rate`（正整数）。
    可省：`channels`（默认 1，文档写明单声道）、`frame_duration`（默认 None，
    文档 §9.2 的服务端 hello 就没这个键）。

    这里**不校验**采样率是不是 16000/24000 —— 那是设备告诉我们的事实，不是要我们
    批准的请求；写死校验只会让换固件时白白连不上。
    """
    if not isinstance(raw, Mapping):
        return ParseResult(
            error=ProtocolError(
                ERR_BAD_FIELD, f"audio_params 要是对象，拿到 {type(raw).__name__}"
            )
        )

    fmt, err = _require(raw, "format", "audio_params")
    if err is not None:
        return ParseResult(error=err)
    if not isinstance(fmt, str) or not fmt:
        return ParseResult(
            error=ProtocolError(
                ERR_BAD_FIELD, f"audio_params.format 要是非空字符串，拿到 {fmt!r}"
            )
        )

    rate, err = _require(raw, "sample_rate", "audio_params")
    if err is not None:
        return ParseResult(error=err)
    rate, err = _as_positive_int(rate, "audio_params.sample_rate")
    if err is not None:
        return ParseResult(error=err)

    channels, err = _as_positive_int(raw.get("channels", 1), "audio_params.channels")
    if err is not None:
        return ParseResult(error=err)

    frame_raw = raw.get("frame_duration")
    frame_duration: int | None = None
    if frame_raw is not None:
        frame_duration, err = _as_positive_int(frame_raw, "audio_params.frame_duration")
        if err is not None:
            return ParseResult(error=err)

    return ParseResult(
        value=AudioParams(
            format=fmt,
            sample_rate=rate,
            channels=channels,
            frame_duration=frame_duration,
        )
    )


def parse_device_hello(raw: object) -> ParseResult:
    """解析设备端 hello（协议文档 §1.3 / §4.1.1）-> `DeviceHello`。永不抛。

    顺序：解成消息 -> `type` 必须是 hello -> `transport` 必须是 websocket
    （第一版只做 WebSocket，见 docs/xiaozhi拆解.md §3）-> `version` 必须是我们
    支持的 -> `audio_params` 必须能解析。`features` / `text_font` 可选。

    **注意服务端 hello 过不了这里**：它同样 `type=hello`，但没有 `version` 字段
    （协议文档 §1.4 的样本就没有），会以 `missing_field` 被挡掉。两个 hello 方向不同、
    字段不同，别指望一个函数两边都能用。
    """
    decoded = decode_message(raw)
    if not decoded.ok:
        return decoded

    message = decoded.value
    if message.get("type") != "hello":
        return ParseResult(
            error=ProtocolError(
                ERR_UNEXPECTED_TYPE,
                f"这里是设备端 hello，拿到 type={message.get('type')!r}",
            )
        )

    transport, err = _require(message, "transport", "hello")
    if err is not None:
        return ParseResult(error=err)
    if transport != TRANSPORT_WEBSOCKET:
        return ParseResult(
            error=ProtocolError(
                ERR_BAD_TRANSPORT,
                f"第一版只接 websocket（docs/xiaozhi拆解.md §3），拿到 {transport!r}",
            )
        )

    version, err = _require(message, "version", "hello")
    if err is not None:
        return ParseResult(error=err)
    version, err = _as_positive_int(version, "hello.version")
    if err is not None:
        return ParseResult(error=err)
    if version not in SUPPORTED_PROTOCOL_VERSIONS:
        return ParseResult(
            error=ProtocolError(
                ERR_UNSUPPORTED_VERSION,
                f"只支持协议版本 {list(SUPPORTED_PROTOCOL_VERSIONS)}"
                f"（§3.1 裸 Opus），拿到 {version}",
            )
        )

    audio_raw, err = _require(message, "audio_params", "hello")
    if err is not None:
        return ParseResult(error=err)
    audio = parse_audio_params(audio_raw)
    if not audio.ok:
        return audio

    # features 缺席或为 null 都当"没有" —— 它是可选扩展，null 不携带任何信息
    features = message.get("features") or {}
    if not isinstance(features, Mapping):
        return ParseResult(
            error=ProtocolError(
                ERR_BAD_FIELD, f"features 要是对象，拿到 {type(features).__name__}"
            )
        )

    text_font = message.get("text_font")
    if text_font is not None and not isinstance(text_font, Mapping):
        return ParseResult(
            error=ProtocolError(
                ERR_BAD_FIELD, f"text_font 要是对象，拿到 {type(text_font).__name__}"
            )
        )

    return ParseResult(
        value=DeviceHello(
            version=version,
            transport=transport,
            audio_params=audio.value,
            features=dict(features),
            text_font=dict(text_font) if text_font is not None else None,
        )
    )


def build_server_hello(
    session_id: str,
    audio_params: AudioParams | None = None,
) -> dict[str, Any]:
    """构造服务端 hello（协议文档 §1.4）。

    §1.4 要求：必须带 `type` + `transport: websocket`；`session_id` 是可选的，但设备
    收到会自动记下来（§8.2 会话控制），所以这里**总是**给。下行音频默认
    opus / 24000 / 单声道 / 60ms（§8.3 + 与设备帧长对齐）。

    `session_id` 为空就抛 ValueError：它是我们自己生成的（uuid），空了纯属 bug ——
    这跟"解析外部输入不许炸"是两回事（同上，一个对外一个对内）。
    """
    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError("session_id 不能为空")

    params = audio_params or AudioParams(
        format=DEFAULT_FORMAT,
        sample_rate=DEFAULT_SERVER_SAMPLE_RATE,
        channels=1,
        frame_duration=DEFAULT_SERVER_FRAME_DURATION,
    )
    return {
        "type": "hello",
        "transport": TRANSPORT_WEBSOCKET,
        "session_id": session_id,
        "audio_params": params.to_dict(),
    }
