"""OTA 配置应答层（A 档）—— 设备开机后问的第一个问题。

配套：上游 `78/xiaozhi-esp32`，本机快照在 `.scratch/xiaozhi/upstream/`
（`ota.cc` / `ota.h` / `application.cc`，2026-09-20 实读）。协议原文是
`docs/websocket_zh.md`，但 **OTA 这一步不在那份文档里** —— 它属于 HTTP，
所以这一层的依据是上游源码本身。

## 为什么设备要先问我们一次

设备**不会**直接去连一个写死的 WebSocket 地址。它的开机流程是：

    读「服务端地址」（闪存里的 wifi/ota_url，空则用固件默认）
      -> 向它发一个 HTTP 请求（`Ota::CheckVersion()`）
      -> 从回答里取出 websocket 段，**逐个键写进闪存**
      -> 这才去连那个 WebSocket -> 发 hello -> 开始对话

所以我们的服务端要开两个口子，而且**可以同一个端口**（照 `xiaoyu/face/server.py`
那套 `process_request`）：HTTP 答这一个请求，WebSocket 管之后的正事。

## 上游原文核对到的硬约束（2026-09-20 实读，不是猜的）

请求侧（`main/ota.cc` 的 `SetupHttp` / `CheckVersion`）：

    POST（因为 board.GetSystemInfoJson() 非空；空才降级成 GET）
    请求头：Device-Id（MAC）/ Client-Id（UUID）/ Activation-Version /
            Serial-Number（有才带）/ User-Agent / Accept-Language /
            Content-Type: application/json
    状态码**必须 200**，否则设备直接判失败（`*status_code != 200`）

应答侧：五个**全部可选**的段 —— `activation` / `mqtt` / `websocket` /
`server_time` / `firmware`。其中三条要特别当心：

    1. **必须有 websocket 段。** 没有它，`application.cc:543` 会打一行
       「No protocol specified in the OTA config, using MQTT」然后**退回 MQTT** ——
       设备就永远不来连我们的 WebSocket 了。症状是"板子没反应"，很难查。
    2. **绝不能有 activation 段。** 只要里面有 `code` 或 `challenge`，
       `application.cc:502-527` 就进激活循环（10 次重试、每次之间还 sleep），
       板子卡在激活界面等用户输入。**我们不做激活**，所以这个键一个字都不许出现。
       这一条之前几份文档都没写，是这次读源码才钉住的。
    3. `websocket` 里的键**会被逐个写进设备的闪存**，所以只放我们要它认的
       `url` / `token` / `version` 三个，别夹带别的东西。

## 设计

和 `protocol.py` / `face/static.py` 一样：**纯逻辑，不 import 网络库、不打日志**。
给「一条 HTTP 请求」返回「一条 HTTP 应答」，网络留给 `server.py` 那一步接。

两个刻意的选择：

- **`url` 从请求的 `Host` 头推出来，零配置。** 设备请求的是
  `http://<我们IP>:8766/xiaozhi/ota/`，它的 Host 头天然就是 `<我们IP>:8766`。
  和 `face/static.py` 让页面读 `location.hostname` 是同一个思路 ——
  换电脑、换 IP、换个路由器，都不用改任何配置。
- **构造会抛、处理不抛**（沿用 `protocol.py` 那条界线）：`build_config()` 是我们
  自己的代码在调用，Host 不合法就抛 `ValueError`（早失败）；而 `handle_request()` 面对的是
  外部输入，一律翻成状态码，不把异常丢给调用方。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

# ---------------- 常量 ----------------

OTA_PATH = "/xiaozhi/ota/"
DEFAULT_WEBSOCKET_PATH = "/xiaozhi/v1/"

# 设备认的配置版本。上游默认就是 1，我们也只支持 1（`protocol.py` 里那条注释同款）。
CONFIG_VERSION = 1

CONTENT_TYPE_JSON = "application/json; charset=utf-8"
CONTENT_TYPE_TEXT = "text/plain; charset=utf-8"

STATUS_OK = 200
STATUS_BAD_REQUEST = 400
STATUS_NOT_FOUND = 404

# 请求头名统一小写存（见 normalize_headers）
HEADER_HOST = "host"
HEADER_DEVICE_ID = "device-id"
HEADER_CLIENT_ID = "client-id"
HEADER_SERIAL_NUMBER = "serial-number"
HEADER_USER_AGENT = "user-agent"


# ---------------- 数据结构 ----------------


@dataclass(frozen=True)
class OtaConfig:
    """我们要发给设备的配置。"""

    token: str = ""
    version: int = CONFIG_VERSION
    websocket_path: str = DEFAULT_WEBSOCKET_PATH

    # WebSocket 在哪个口上。None = 跟 OTA 同一个 Host（老行为）。
    # 现在**必须**分开：上游发的是 POST + body，而 websockets 的 HTTP 层不收 body
    # （2026-09-22 实测，见 ws.py 开头「坑 4」）—— 所以 OTA 走标准库的 HTTP 口，
    # 对话走 websockets 的另一个口，这里负责把正确的口告诉设备。
    websocket_port: int | None = None

    # 默认**关**：第一版接板子时，变量越少越好 —— 先只给它"往哪连"这一件事。
    # 开了之后设备会把系统时间对齐到我们的时间（见 server_time()）。
    include_server_time: bool = False


@dataclass(frozen=True)
class OtaResponse:
    """一条 HTTP 应答。`reason` 是给人看的，调用方拿去写日志。"""

    status: int
    body: bytes
    content_type: str = CONTENT_TYPE_JSON
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK


# ---------------- 请求侧：把设备发来的东西看懂 ----------------


def _text(value: Any) -> str:
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", "replace")
    if isinstance(value, str):
        return value
    return str(value)


def _as_lines(raw: Any) -> list[str]:
    if isinstance(raw, (str, bytes, bytearray)):
        return _text(raw).splitlines()
    try:
        return [_text(item) for item in raw]
    except TypeError:
        return []


def normalize_headers(raw: Any) -> dict[str, str]:
    """请求头归一化成 `{"小写名": "值"}`。认不出来就返回 {}，绝不抛。

    接受四种形状：

      1. dict / `Mapping`（`websockets` 给的是这种）
      2. `email.message.Message` —— **标准库 `http.server` 的 `self.headers` 就是它**。
         2026-09-22 实测：它长得像 Mapping，但**不是** `collections.abc.Mapping`，
         于是掉进"按行拆"那条路：迭代一个 Message 拿到的是**值**，`Host` 会被拆成
         一堆碎片直接丢掉 —— 表现是 OTA 回 400「Host 头不合法」，而客户端明明发了 Host。
         分口那天就是被它咬的，别删这个分支。
      3. 一串 `"Name: value"` 行
      4. 单行字符串

    **同名取最后一个**（HTTP 的规矩），畸形行直接跳过 —— 一个坏请求头不该让
    "板子为什么连不上"变得更难查。
    """
    if raw is None:
        return {}
    if isinstance(raw, Mapping):
        return {_text(name).strip().lower(): _text(value).strip() for name, value in raw.items()}

    items = getattr(raw, "items", None)
    if callable(items):
        try:
            return {
                _text(name).strip().lower(): _text(value).strip()
                for name, value in raw.items()
            }
        except Exception:  # noqa: BLE001 —— 认不出来就当没有头，别把请求打回去
            return {}

    out: dict[str, str] = {}
    for line in _as_lines(raw):
        name, sep, value = line.partition(":")
        if not sep:
            continue
        name = name.strip().lower()
        if name:
            out[name] = value.strip()
    return out


def device_id(headers: Mapping[str, str]) -> str:
    """设备标识：`Device-Id`（MAC）优先，退到 `Client-Id`（UUID），再退序列号。

    只用来写日志 —— 板子第一次连不上时，日志里至少得看出**是哪个设备在问**。
    """
    for key in (HEADER_DEVICE_ID, HEADER_CLIENT_ID, HEADER_SERIAL_NUMBER):
        value = (headers.get(key) or "").strip()
        if value:
            return value
    return ""


def parse_device_info(body: Any) -> dict[str, Any]:
    """设备的自述（上游 `board.GetSystemInfoJson()`）—— 解析不出来就返回 `{}`。

    里面有固件版本、板卡型号、MAC。**只看不用**：板子到货那天拿它核对
    "到的是不是我们要的那块"，以及固件版本和 `.scratch/firmware/` 里那份对不对得上。
    """
    if body is None:
        return {}
    if isinstance(body, (bytes, bytearray)):
        text = bytes(body).decode("utf-8", "replace")
    elif isinstance(body, str):
        text = body
    else:
        return {}
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def is_ota_path(path: Any) -> bool:
    """这一条 HTTP 请求是不是在问 OTA 配置。

    带不带结尾斜杠都认：设备连的是用户手填进配网页的那串地址，
    多一个少一个斜杠是手误，不该让板子连不上。查询串同理（直接丢掉）。
    """
    target = urlparse(_text(path)).path or "/"
    return target.rstrip("/") == OTA_PATH.rstrip("/")


# ---------------- 应答侧：构造我们要发回去的东西 ----------------


def host_with_port(host: str, port: int) -> str:
    """把 Host 头里的端口换成另一个：`10.0.0.5:8766` -> `10.0.0.5:8767`。

    IPv6 的 `[fe80::1]:8766` 也要认 —— 方括号里的冒号不是分隔符。
    裸 IPv6（没有方括号）原样返回：那种情况下拼端口只会拼出一个坏地址，
    让上层自己去报错比这里猜强。
    """
    host = (host or "").strip()
    if not host:
        return host
    if host.startswith("["):
        end = host.find("]")
        name = host[: end + 1] if end != -1 else host
    elif host.count(":") == 1:
        name = host.rsplit(":", 1)[0]
    elif host.count(":") > 1:
        return host
    else:
        name = host
    return f"{name}:{int(port)}"


def websocket_url(
    host: str, path: str = DEFAULT_WEBSOCKET_PATH, *, port: int | None = None
) -> str:
    """`Host` 头 -> 我们要告诉设备的 WebSocket 地址。Host 不合法返回 ""。

    只用 `ws://` 不用 `wss://`：家里局域网，没有证书这回事
    （v3 §3.1 的安全边界本来就不映射公网）。

    挡掉空白和 `/ ? #` 是为了防注入 —— Host 是外部输入，直接拼进 URL 里，
    不挡的话 `Host: evil/x` 就能把设备指到别的地方去。
    """
    host = (host or "").strip()
    if not host or any(char.isspace() for char in host):
        return ""
    if any(char in host for char in "/?#"):
        return ""
    if port is not None:
        # 对话不在 OTA 那个口上 —— 把正确的口写进要下发给设备的地址里
        host = host_with_port(host, port)
    if not path.startswith("/"):
        path = "/" + path
    return f"ws://{host}{path}"


def server_time(now: datetime | None = None) -> dict[str, int]:
    """设备要的「服务器时间」（读法见 `main/ota.cc:194-216`）。

    单位是 **UTC 毫秒 + 时区偏移（分钟）**：设备拿到后做的是
    `settimeofday(utc_ms + offset_min * 60000)`。也就是说，想让它显示**当地时间**，
    就得把偏移一起写进去 —— 北京是 `+480`。

    默认不发给设备（`OtaConfig.include_server_time = False`）。
    """
    moment = now or datetime.now().astimezone()
    offset = moment.utcoffset()
    minutes = int(offset.total_seconds() // 60) if offset is not None else 0
    return {"timestamp": int(moment.timestamp() * 1000), "timezone_offset": minutes}


def build_config(
    host: str,
    config: OtaConfig | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """构造要回给设备的 JSON。`host` 不合法**抛 ValueError**（我们自己调用时早失败）。

    ⚠️ 这里只允许出现两个顶层键：`websocket` 和 `server_time`。
    `activation` / `mqtt` / `firmware` 一个都不能有 —— 理由见模块 docstring
    的第 2 条（有 activation 设备就卡在激活界面不往下走了）。
    改这个函数之前，先回去把那段读完。
    """
    cfg = config or OtaConfig()
    url = websocket_url(host, cfg.websocket_path, port=cfg.websocket_port)
    if not url:
        raise ValueError(f"Host 头不合法，拼不出 websocket 地址: {host!r}")

    payload: dict[str, Any] = {
        # 这三个键会被设备逐个写进闪存（main/ota.cc:172-191），别夹带别的
        "websocket": {
            "url": url,
            "token": cfg.token,
            "version": cfg.version,
        }
    }
    if cfg.include_server_time:
        payload["server_time"] = server_time(now)
    return payload


def encode_config(payload: Mapping[str, Any]) -> bytes:
    """JSON -> UTF-8 字节。不转义中文（设备那边是 cJSON，认得）。"""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def handle_request(
    path: Any,
    headers: Any = None,
    body: Any = None,
    config: OtaConfig | None = None,
    *,
    now: datetime | None = None,
) -> OtaResponse:
    """一条 HTTP 请求 -> 一条 HTTP 应答。**坏输入翻成状态码，不抛异常。**

    `server.py` 那一步的接线就一行：路径不是 OTA 就交给别人（404），
    是就把 `response.body` 原样发回去。`body`（设备的自述）在这里不看，
    只由 `parse_device_info()` 单独给调用方拿去写日志。
    """
    if not is_ota_path(path):
        return OtaResponse(
            STATUS_NOT_FOUND,
            b"not found",
            CONTENT_TYPE_TEXT,
            f"{_text(path)!r} 不是 OTA 端点",
        )

    parsed = normalize_headers(headers)
    try:
        payload = build_config(parsed.get(HEADER_HOST, ""), config, now=now)
    except ValueError as exc:
        return OtaResponse(
            STATUS_BAD_REQUEST,
            str(exc).encode("utf-8"),
            CONTENT_TYPE_TEXT,
            str(exc),
        )

    return OtaResponse(STATUS_OK, encode_config(payload), CONTENT_TYPE_JSON, "配置已下发")