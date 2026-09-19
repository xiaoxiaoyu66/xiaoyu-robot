"""小智最小服务端（A 档，`docs/xiaozhi拆解.md` §2.2.1 第 5 项）。

只做三件事：**能连上、能收发、能优雅收工**。先不接 LLM —— 应答是固定的。

## 为什么这一步也全是离线的

真网络层（`websockets`）**这里不写**：板子还没到，写了也验不了，只会多出
一份"看着写完、其实没人跑过"的代码。所以这一步定的是**传输之上那一层**：

    Transport(Protocol)   一条连接的最小形状：recv / send / close
    FakeTransport         假客户端那一端（真样本喂进来，发出去的记下来）
    Connection            驱动器：帧 -> 事件 -> 状态机 -> 动作 -> 帧
    FixedResponder        固定应答（骨架占位，真应答等第二批）

板子到了之后，真实现只要满足 `Transport` 就能接上，`Connection` 一行不用改。
跟 `body/servo.py`（假舵机）、`audio_codec.py`（假编解码器）是同一个套路。

## 收工的规矩（§2.2.1 第 5 项的验收：「断开时不留悬挂任务」）

`run()` 无论怎么退出（正常收工 / 对端断开 / hello 超时 / 被取消 / 应答里抛异常），
都必须走到同一个 `finally`：关掉 transport、打一条收工日志。跑完之后会话停在
CLOSED，没有活着的任务留着。收工路径只有一条 —— 这是故意的。

## 这一版**故意不做**的（别以为已经有了）

- **说话期间不收帧**：`RUN_TURN` 是顺序执行的。一边下发音频、一边听有没有打断，
  必须并发，那是第二批「能打断」的事，也要等板子。直接后果：`STOP_SPEAKING`
  这个动作**现在走不到** —— `_stop_speaking()` 只是把接线点先留出来。
- **不接 LLM / 记忆 / TTS**：`FixedResponder` 回固定的一段静音。它证明的是
  「切帧 -> 编码 -> 发出去」这条链路通，不是「它会说话了」。
- **不认 `listen(stop)` 之外的说话触发**：没有手动按键模式、没有 MCP 调用。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from ..logger import get_logger
from . import protocol
from .audio_codec import OpusCodec, iter_pcm_frames
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

logger = get_logger(__name__)

# `_receive()` 的两个"没有帧"的结果。用哨兵而不是 None，是因为 None 已经被
# 「对端断开」占用了 —— 混在一起会把"暂时没话说"当成"连接断了"。
_TIMED_OUT = object()


@runtime_checkable
class Transport(Protocol):
    """一条连接的最小形状。真实现（websockets）照着写这三个方法就能接上。

    帧的约定跟协议文档 §1.5 一致：文本帧是 JSON（`str`），二进制帧是 Opus（`bytes`）。
    """

    async def recv(self) -> object | None:
        """等下一帧。**对端收工返回 `None`**（不是抛异常）。"""
        ...

    async def send(self, frame: bytes | str) -> None:
        """发一帧。"""
        ...

    async def close(self) -> None:
        """收工。要能重复调用。"""
        ...


_PEER_CLOSED = object()


class FakeTransport:
    """假连接 —— 板子的那一端。跟 `FakeServo` 一个道理：没有硬件也能把上层跑通、能断言。

    `sent` 留全量发出的帧，`close_calls` 看收工干不干净。喂进去的帧应该来自
    `tests/fixtures/xiaozhi/`（逐字抄自协议文档的真样本），不是自己编的 JSON。
    """

    def __init__(self, incoming: Iterable[object] = ()) -> None:
        self._inbox: asyncio.Queue = asyncio.Queue()
        for frame in incoming:
            self._inbox.put_nowait(frame)
        self.sent: list[bytes | str] = []
        self.closed = False
        self.close_calls = 0

    # ---------------------------------------------------------- 测试用的手

    def feed(self, frame: object) -> None:
        """设备又发了一帧。"""
        self._inbox.put_nowait(frame)

    def disconnect(self) -> None:
        """设备把连接关了（走远 / 关机 / Wi-Fi 抖）。"""
        self._inbox.put_nowait(_PEER_CLOSED)

    # ---------------------------------------------------------- Transport

    async def recv(self) -> object | None:
        # 队列空就**等着**（跟真 socket 一样）。这里不返回 None：
        # 返回 None 只表示对端断开，不能让"暂时没话说"变成"连接断了"。
        item = await self._inbox.get()
        if item is _PEER_CLOSED:
            return None
        return item

    async def send(self, frame: bytes | str) -> None:
        if self.closed:
            raise RuntimeError("连接已经关了还在 send —— 收工路径漏了一处")
        self.sent.append(frame)

    async def close(self) -> None:
        self.close_calls += 1
        self.closed = True

    # ---------------------------------------------------------- 断言用

    @property
    def text_frames(self) -> list[str]:
        return [frame for frame in self.sent if isinstance(frame, str)]

    @property
    def binary_frames(self) -> list[bytes]:
        return [frame for frame in self.sent if isinstance(frame, bytes)]


@dataclass(frozen=True)
class TurnRequest:
    """轮到我们说话时交给 responder 的上下文。"""

    session: Session       # 设备说了什么、会话 id 是多少，读它
    codec: OpusCodec       # 下行采样率 / 帧长在 codec.downlink 里


@runtime_checkable
class Responder(Protocol):
    """轮到我们说话时，产出要下发的**下行 PCM**（按 `codec.downlink` 的采样率）。

    切帧和编码由 `Connection` 干 —— responder 只管「说什么」。这样换 responder
    （固定应答 / 真 TTS）不用碰切帧那套容易写错、错了又不报错的算术。
    """

    def respond(self, request: TurnRequest) -> AsyncIterator[bytes]:
        ...


class FixedResponder:
    """固定应答：不管设备说了什么，都回同样一段下行音频。

    骨架的占位 —— 真应答（ASR -> LLM -> 记忆 -> TTS）是第二批的事，要等板子。
    默认回**一帧静音**，故意不用"假声音"：静音不会让人误以为 TTS 已经通了，
    但它真的走完了「切帧 -> 编码 -> 发出去」这条链路。
    """

    def __init__(self, *, frames: int = 1, name: str = "fixed") -> None:
        if frames < 1:
            raise ValueError("frames 至少 1 帧 —— 一帧都不发就等于没有应答")
        self.frames = frames
        self.name = name

    async def respond(self, request: TurnRequest) -> AsyncIterator[bytes]:
        size = request.codec.downlink.frame_bytes
        logger.debug("固定应答 {}：{:d} 帧静音（每帧 {} 字节）", self.name, self.frames, size)
        yield b"\x00" * (size * self.frames)


class Connection:
    """一条连接的驱动器：帧 -> 事件 -> 状态机 -> 动作 -> 帧。

    一个实例只服务**一条**连接（一台设备）。用完就丢，别跨连接复用 ——
    会话状态是有记忆的（`Session` 里存着设备 hello、忽略计数那些）。
    """

    def __init__(
        self,
        transport: Transport,
        *,
        codec: OpusCodec,
        session: Session | None = None,
        responder: Responder | None = None,
        session_id: str | None = None,
        hello_timeout: float = HELLO_TIMEOUT_SECONDS,
    ) -> None:
        self.transport = transport
        self.codec = codec
        self.responder: Responder = responder if responder is not None else FixedResponder()
        self.session = (
            session
            if session is not None
            else Session(session_id, hello_timeout=hello_timeout)
        )
        # 如果你自己传了 session，请把 hello_timeout 也给成同一个值：
        # 状态机那份管"判定"，这份管"recv 等多久"，两处不一致会互相打架。
        self.hello_timeout = float(hello_timeout)
        self.sent_frames = 0
        self.sent_audio_frames = 0

    # ------------------------------------------------------------ 外面用的

    @property
    def closed(self) -> bool:
        return self.session.state is SessionState.CLOSED

    async def run(self) -> Session:
        """跑到收工为止。**无论怎么退出都不留悬挂任务** —— 见模块 docstring。"""
        logger.info("小智：设备连上了（会话 {}）", self.session.session_id)
        try:
            await self._loop()
        finally:
            # 唯一的收工路径：正常收工 / 对端断开 / 超时 / 被取消，都走这里
            await self._shutdown()
        return self.session

    # ------------------------------------------------------------ 主循环

    async def _loop(self) -> None:
        while not self.closed:
            frame = await self._receive()
            await self._execute(self._absorb(frame))

    async def _receive(self) -> object:
        """等一帧。等超时返回 `_TIMED_OUT`，对端断开返回 `None`。"""
        try:
            return await asyncio.wait_for(self.transport.recv(), self._recv_timeout())
        except TimeoutError:
            return _TIMED_OUT

    def _recv_timeout(self) -> float | None:
        """只有「等 hello」才有截止时间，握完手就慢慢等。

        跟 session.py 一个立场：不给正常对话编超时 ——
        模型慢一点、用户想一会儿，都不该被我们掐掉。
        """
        if self.session.state is SessionState.CONNECTING:
            return self.hello_timeout
        return None

    def _absorb(self, frame: object) -> list[Action]:
        """一帧（或者"等超时了"）-> 一串要做的动作。"""
        if frame is _TIMED_OUT:
            # 超时是我们自己的事，不往状态机里塞假事件 —— 它自己会判
            return self.session.poll()
        if frame is None:
            return self.session.handle(Event(EventKind.CLOSED, "对端断开"))
        # 每个事件之后都查一次超时（Session.poll 的约定）
        return self.session.handle(event_from_frame(frame)) + self.session.poll()

    # ------------------------------------------------------------ 执行动作

    async def _execute(self, actions: Sequence[Action]) -> None:
        for action in actions:
            if action.kind is ActionKind.SEND_MESSAGE:
                await self._send_message(action.payload)
            elif action.kind is ActionKind.RUN_TURN:
                await self._run_turn()
            elif action.kind is ActionKind.STOP_SPEAKING:
                self._stop_speaking()
            elif action.kind is ActionKind.CLOSE:
                logger.info(
                    "小智：会话 {} 收工（{}）", self.session.session_id, action.payload
                )
            else:
                # 状态机加了新动作而这里忘了接 —— 别静默吞掉
                logger.warning("小智：认不得的动作 {} —— 状态机加了新动作？", action)

    async def _send_message(self, message: Mapping[str, Any]) -> None:
        await self._send(protocol.encode_message(message), audio=False)

    async def _send(self, frame: bytes | str, *, audio: bool) -> None:
        await self.transport.send(frame)
        self.sent_frames += 1
        if audio:
            self.sent_audio_frames += 1

    async def _run_turn(self) -> None:
        """我们说一轮：拿应答 -> 切帧 -> 编码 -> 发下去 -> 告诉状态机说完了。"""
        request = TurnRequest(session=self.session, codec=self.codec)
        downlink = self.codec.downlink
        packets = 0
        async for chunk in self.responder.respond(request):
            for frame in iter_pcm_frames(
                chunk, downlink.sample_rate, downlink.frame_duration, downlink.channels
            ):
                if self.session.state is not SessionState.SPEAKING:
                    # 被打断了：剩下的不发。第二批接了并发收帧之后才走得到这条
                    logger.info("小智：会话 {} 被打断，剩下的音频不发了", self.session.session_id)
                    return
                await self._send(self.codec.encode(frame), audio=True)
                packets += 1
        logger.debug("小智：会话 {} 下发 {} 个音频包", self.session.session_id, packets)
        # 说完了 —— 只有还在说话才通知状态机（被打断的话它已经回 LISTENING 了）。
        # 少了这一步，手动模式下设备不会自动重新收音，会话就永远卡在 SPEAKING。
        if self.session.state is SessionState.SPEAKING:
            await self._execute(self.session.handle(Event(EventKind.SPEAK_END)))

    def _stop_speaking(self) -> None:
        """掐掉当前这一轮的下发。

        **这一版走不到**：`_run_turn` 是顺序执行的（说话期间不收帧），而打断信号
        （`abort` / `listen(detect)`）只能从帧里来 —— 所以真到不了这儿。接上它，
        是为了让"状态机要求的事"和"驱动层会做的事"一一对应，不留下没接的动作；
        第二批做成并发收帧之后，这里就变成 cancel 掉那个下发任务。
        """
        logger.debug(
            "小智：会话 {} 收到「停止说话」（这一版还掐不到，见 server.py docstring）",
            self.session.session_id,
        )

    # ------------------------------------------------------------ 收工

    async def _shutdown(self) -> None:
        try:
            await self.transport.close()
        except Exception as exc:  # noqa: BLE001
            # 关连接失败不该盖掉真正的原因（超时 / 协议错）—— 记一笔就够
            logger.warning("小智：关连接出错（会话 {}）：{}", self.session.session_id, exc)
        logger.info(
            "小智：会话 {} 已收工（{}）—— 发出 {} 帧（音频 {} 帧），忽略 {} 个事件",
            self.session.session_id,
            self.session.closed_reason or "未说明",
            self.sent_frames,
            self.sent_audio_frames,
            self.session.ignored_count,
        )
