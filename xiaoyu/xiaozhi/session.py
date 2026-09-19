"""小智会话状态机（A 档）—— 纯逻辑，不碰网络、不碰硬件。

配套：`protocol.py`（帧类型判定 / 编解码 / hello）与上游协议文档
（`78/xiaozhi-esp32` 的 `docs/websocket_zh.md`，下称「协议文档」）。

## 这一层管什么

把「设备发来的一帧一帧」翻译成「我们这边该做什么」，并且保证**任何输入都不会把
状态搞死** —— 这是 `docs/xiaozhi拆解.md` §2.2.1 对这一步的验收。真正的网络收发、
ASR、LLM、TTS 全在调用方（`server.py`），这里只做决定，所以能全离线单测。

## 状态（5 个，名字沿用 §2.2.1）

    CONNECTING   传输已建立，等设备 hello（10 秒不来就收工）
    HANDSHAKING  hello 已互换，等设备开一轮
    LISTENING    设备在收音（用户这一轮）
    SPEAKING     轮到我们（跑 ASR/LLM/TTS + 往设备下发音频）
    CLOSED       终态；到了这里就再也不动了

## 迁移（只认协议文档里写过的信号）

    CONNECTING  --device_hello(合法)---------> HANDSHAKING  发我们的 hello
    CONNECTING  --device_hello(不合法)-------> CLOSED       收工
    CONNECTING  --超时 10 秒-----------------> CLOSED       收工
    HANDSHAKING --listen(state=start|detect)-> LISTENING
    LISTENING   --listen(state=stop)---------> SPEAKING     轮到我们，发 RUN_TURN
    SPEAKING    --listen(state=start)--------> LISTENING    设备自动重新收音（§6 自动模式）
    SPEAKING    --speak_end------------------> LISTENING    我们这一轮说完了
    SPEAKING    --abort / listen(detect)-----> LISTENING    唤醒词打断，发 STOP_SPEAKING
    任意状态    --closed---------------------> CLOSED       收工

    其余一律**忽略 + 记一笔**（`ignored_count`）—— 乱序、重复、将来新增的消息类型
    都不该把会话搞死。§8.6 也是这个精神：设备端收到缺 `type` 的消息只记日志、不执行。

## 几个刻意的判断（都有依据，别当随手写的）

- **超时只做「等 hello 的 10 秒」**（协议文档 §1.4；上游源码
  `main/protocols/websocket_protocol.cc` 里就是 `pdMS_TO_TICKS(10000)`）。
  别的超时值都是编的 —— 比如给「说话」也加个超时，模型一慢就会把好好的会话掐掉。
  真要防卡死，交给调用方（它才知道一轮跑到哪了）。
- **收到音频帧不算错**：§4.1.4 说设备在发 `listen/detect` 之前**会先发唤醒词的
  Opus 数据**，所以 HANDSHAKING 阶段收到音频是正常的，收下就好。
- **`listen(state=start)` 在 SPEAKING 时不当打断**：那是我们说完了、设备自动重新收音
  （协议文档 §6 自动模式）。真正的打断信号是 `abort` 和 `listen(detect)`。
- **`speak_end` 是我们自己的事件**：只有我们知道自己什么时候下发完 TTS。没有它，
  手动模式下设备不会自动重新收音，会话就永远卡在 SPEAKING —— 正是验收里
  说的「状态不许卡死」。

时钟可以注入（`clock=`），所以超时能被单测精确驱动，不需要真的等 10 秒。
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

from ..logger import get_logger
from . import protocol

logger = get_logger(__name__)

# 等设备 hello 的上限。协议文档 §1.4 说「默认 10 秒」，上游源码
# websocket_protocol.cc 里是 pdMS_TO_TICKS(10000) —— 两边对得上，所以照抄。
HELLO_TIMEOUT_SECONDS = 10.0

# 设备端 listen 消息里 state 的取值（协议文档 §4.1.2 / §4.1.4）
LISTEN_START = "start"
LISTEN_STOP = "stop"
LISTEN_DETECT = "detect"


class SessionState(Enum):
    """会话状态。取值字符串和 §2.2.1 里写的那串名字一致，日志里直接可读。"""

    CONNECTING = "connecting"
    HANDSHAKING = "handshaking"
    LISTENING = "listening"
    SPEAKING = "speaking"
    CLOSED = "closed"


class EventKind(Enum):
    """喂给状态机的事件。

    前四个是 §2.2.1 点名要的（设备侧信号）；后两个是补充：
    `SPEAK_END` 只有我们自己知道，`UNKNOWN` 用来消化认不出来的帧。
    """

    DEVICE_HELLO = "device_hello"    # 设备端 hello（原始文本 / 字节，或已解析的 DeviceHello）
    AUDIO_FRAME = "audio_frame"      # 二进制帧 = Opus 音频
    TEXT_MESSAGE = "text_message"    # 文本帧 = JSON（listen / abort / mcp / ...）
    CLOSED = "closed"                # socket 断了 / 出错 / 对方关掉
    SPEAK_END = "speak_end"          # 我们这一轮 TTS 下发完毕（我们自己发的）
    UNKNOWN = "unknown"              # 既不是 str 也不是 bytes 的帧


class ActionKind(Enum):
    """状态机让调用方去做的事。"""

    SEND_MESSAGE = "send_message"      # payload: 消息 dict，交给 protocol.encode_message
    RUN_TURN = "run_turn"              # 轮到我们了：跑 ASR -> LLM -> TTS
    STOP_SPEAKING = "stop_speaking"    # 立刻停止往设备下发音频（被打断）
    CLOSE = "close"                    # 收工：关连接、停掉这一轮的任务；payload: 原因


@dataclass(frozen=True)
class Event:
    kind: EventKind
    payload: Any = None


@dataclass(frozen=True)
class Action:
    kind: ActionKind
    payload: Any = None

    def __str__(self) -> str:
        # 日志里直接 {} 它就能看
        return self.kind.value if self.payload is None else f"{self.kind.value}({self.payload!r})"


def _is_device_hello(data: object) -> bool:
    """这一帧文本是不是设备 hello（协议文档 §1.3）。

    解不开就当**不是** —— 坏消息有它自己的处理路径（认出来也只是被忽略），
    别让一个坏 JSON 在这里改变事件的种类。
    """
    decoded = protocol.decode_message(data)
    return decoded.ok and decoded.value.get("type") == "hello"


def event_from_frame(data: object) -> Event:
    """WebSocket 收到的一帧 -> 事件（用 `protocol.classify_frame` 判类型）。

    认不出来的帧也返回事件（`UNKNOWN`），交给状态机忽略 + 记一笔 —— 这一层不抛。
    """
    kind = protocol.classify_frame(data)
    if kind is protocol.FrameKind.TEXT:
        # 设备 hello 也是文本帧，但对状态机是**独立事件**（握手只认它）。
        # 不在这里分出来的话：hello 被当成普通文本消息忽略，会话一直卡在
        # CONNECTING 直到 10 秒超时 —— 只有把真实帧喂进来才会发现的坑。
        # payload 留**原始文本**，由 `_on_device_hello` 自己解析（它两种都收）。
        if _is_device_hello(data):
            return Event(EventKind.DEVICE_HELLO, data)
        return Event(EventKind.TEXT_MESSAGE, data)
    if kind is protocol.FrameKind.BINARY:
        return Event(EventKind.AUDIO_FRAME, data)
    return Event(EventKind.UNKNOWN, data)


class Session:
    """一台设备一条会话。**所有方法都不抛异常**（除了构造参数写错）。

    `handle()` 收事件、回动作；`poll()` 只管超时。两句都得调用方主动调 ——
    这里没有线程、没有定时器，纯逻辑。
    """

    def __init__(
        self,
        session_id: str | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        hello_timeout: float = HELLO_TIMEOUT_SECONDS,
    ) -> None:
        self.session_id = session_id or uuid.uuid4().hex
        self.state = SessionState.CONNECTING
        # 设备端 hello 解析出来的东西（握手成功前是 None）
        self.device_hello: protocol.DeviceHello | None = None
        # 最近一次「收到的东西不对」的原因（坏 hello / 解不开的文本帧）
        self.last_error: protocol.ProtocolError | None = None
        # 收工时为什么收工（超时 / 协议错 / 对方断开）
        self.closed_reason: str | None = None
        # 忽略了多少个不该来的事件（乱序 / 重复 / 认不得）—— 排查时第一个看这个
        self.ignored_count = 0

        self._clock = clock
        self._hello_timeout = float(hello_timeout)
        self._deadline = self._clock() + self._hello_timeout
        self._handlers = {
            EventKind.DEVICE_HELLO: self._on_device_hello,
            EventKind.AUDIO_FRAME: self._on_audio_frame,
            EventKind.TEXT_MESSAGE: self._on_text_message,
            EventKind.CLOSED: self._on_closed,
            EventKind.SPEAK_END: self._on_speak_end,
        }

    # ------------------------------------------------------------------ 外面用的

    def handle(self, event: Event) -> list[Action]:
        """喂一个事件，拿回一串要做的动作。永不抛。"""
        if self.state is SessionState.CLOSED:
            # 终态：之后来的什么都不理（免得已收工的会话被迟到的事件拉活）
            self._ignore(event, "会话已收工")
            return []
        handler = self._handlers.get(event.kind)
        if handler is None:
            self._ignore(event, "认不得的事件")
            return []
        return handler(event.payload)

    def poll(self) -> list[Action]:
        """超时检查。调用方在「每个事件之后」和「定时醒来时」各调一次。

        只检查一件事：CONNECTING 等 hello 等过头了没有（见模块 docstring）。
        时间只从注入的 `clock` 取 —— 再开一个 `now=` 参数是重复的，还会被用错
        （拿真实 monotonic 的 deadline 去比一个自己造的秒数，永远比不平）。
        """
        if self.state is not SessionState.CONNECTING:
            return []
        if self._clock() >= self._deadline:
            return self._close(f"等设备 hello 超过 {self._hello_timeout:g} 秒")
        return []

    def __repr__(self) -> str:
        return f"<Session {self.session_id} {self.state.value}>"

    # ------------------------------------------------------------------ 事件处理

    def _on_device_hello(self, payload: Any) -> list[Action]:
        if self.state is not SessionState.CONNECTING:
            self._ignore(Event(EventKind.DEVICE_HELLO), "已经握过手了")
            return []

        if isinstance(payload, protocol.DeviceHello):
            hello = payload
        else:
            result = protocol.parse_device_hello(payload)
            if not result.ok:
                self.last_error = result.error
                return self._close(f"设备 hello 不合法：{result.error}")
            hello = result.value

        self.device_hello = hello
        self._set_state(SessionState.HANDSHAKING)
        # 我们的 hello 回给设备；session_id 由我们生成（协议文档 §1.4 / §8.2）
        return [Action(
            ActionKind.SEND_MESSAGE,
            protocol.build_server_hello(self.session_id),
        )]

    def _on_audio_frame(self, payload: Any) -> list[Action]:
        # HANDSHAKING 也可能来音频：§4.1.4 说唤醒词的 Opus 数据会先于 listen/detect
        # 发过来。收下（payload 由调用方取走喂 ASR），不改状态、也不算错。
        if self.state in (SessionState.HANDSHAKING, SessionState.LISTENING):
            return []
        self._ignore(Event(EventKind.AUDIO_FRAME), f"{self.state.value} 状态下不该有音频帧")
        return []

    def _on_text_message(self, payload: Any) -> list[Action]:
        if isinstance(payload, Mapping):
            message = payload
        else:
            decoded = protocol.decode_message(payload)
            if not decoded.ok:
                # 坏消息不该把会话搞死（§8.6：设备端也只记一条日志、不执行）
                self.last_error = decoded.error
                self._ignore(Event(EventKind.TEXT_MESSAGE), f"解不开：{decoded.error}")
                return []
            message = decoded.value

        message_type = message.get("type")
        if message_type == "listen":
            return self._on_listen(message)
        if message_type == "abort":
            return self._on_abort(message)
        # 别的（mcp / system / custom / 将来新增的）不影响会话状态：收下就好，
        # 调用方想处理自己去读事件。**故意不按「认不得」计数** —— 它们是合法的。
        return []

    def _on_closed(self, payload: Any) -> list[Action]:
        # 对方断开是运行期的常态（关设备、走远、Wi-Fi 抖），记 INFO 就好；
        # WARNING 留给"本来不该发生"的那些（超时、协议错）——
        # 不然日志里全是正常断开，真出问题反而看不见（S5.5 那次的教训）。
        reason = str(payload) if payload else "设备或网络断开"
        return self._close(reason, worrying=False)

    def _on_speak_end(self, payload: Any) -> list[Action]:
        if self.state is not SessionState.SPEAKING:
            self._ignore(Event(EventKind.SPEAK_END), "没在说话")
            return []
        self._set_state(SessionState.LISTENING)
        return []

    # ------------------------------------------------------------------ listen / abort

    def _on_listen(self, message: Mapping[str, Any]) -> list[Action]:
        listen_state = message.get("state")

        if listen_state == LISTEN_DETECT and self.state is SessionState.SPEAKING:
            # 唤醒词命中 = 用户要打断我们（§4.1.4）
            return self._interrupt()

        if listen_state in (LISTEN_START, LISTEN_DETECT):
            if self.state is SessionState.HANDSHAKING:
                self._set_state(SessionState.LISTENING)
                return []
            if self.state is SessionState.SPEAKING:
                # 我们说完了、设备自动重新收音（协议文档 §6 自动模式）。
                # **这不是打断** —— 打断信号是 abort / detect。
                self._set_state(SessionState.LISTENING)
                return []
            self._ignore(Event(EventKind.TEXT_MESSAGE), f"重复或乱序的 listen({listen_state})")
            return []

        if listen_state == LISTEN_STOP:
            if self.state is SessionState.LISTENING:
                # 用户说完了 —— 轮到我们
                self._set_state(SessionState.SPEAKING)
                return [Action(ActionKind.RUN_TURN)]
            self._ignore(Event(EventKind.TEXT_MESSAGE), f"{self.state.value} 状态下收到 listen(stop)")
            return []

        self._ignore(Event(EventKind.TEXT_MESSAGE), f"认不得的 listen(state={listen_state!r})")
        return []

    def _on_abort(self, payload_message: Any = None) -> list[Action]:
        if self.state is SessionState.SPEAKING:
            return self._interrupt()
        # 没在说话，没什么可停的（协议文档 §4.1.3 的 abort 是终止 TTS 播放/语音通道）
        self._ignore(Event(EventKind.TEXT_MESSAGE), "没在说话，abort 无事可做")
        return []

    def _interrupt(self) -> list[Action]:
        """被打断：先让调用方闭麦，然后回到「设备在收音」。"""
        self._set_state(SessionState.LISTENING)
        return [Action(ActionKind.STOP_SPEAKING)]

    # ------------------------------------------------------------------ 内部小工具

    def _set_state(self, new_state: SessionState) -> None:
        if new_state is self.state:
            return
        logger.debug(
            "小智会话 {} {} -> {}", self.session_id, self.state.value, new_state.value
        )
        self.state = new_state

    def _ignore(self, event: Event, why: str) -> None:
        self.ignored_count += 1
        logger.debug(
            "小智会话 {} 忽略 {}（{}）：{}",
            self.session_id, event.kind.value, self.state.value, why,
        )

    def _close(self, reason: str, *, worrying: bool = True) -> list[Action]:
        """收工。`worrying=True` 表示这是"本来不该发生"的收工（超时 / 协议错）。"""
        self.closed_reason = reason
        self._set_state(SessionState.CLOSED)
        level = logger.warning if worrying else logger.info
        level("小智会话 {} 收工：{}", self.session_id, reason)
        return [Action(ActionKind.CLOSE, reason)]
