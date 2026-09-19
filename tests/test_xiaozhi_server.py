"""小智最小服务端（`xiaoyu/xiaozhi/server.py`）的测试。

跑法：py -3.11 -m unittest discover tests

全程**离线**：没有板子、没有 `websockets`、不开任何端口。设备那一端是
`FakeTransport`，喂进去的帧全部来自 `tests/fixtures/xiaozhi/`（逐字抄自上游
协议文档），不是自己编的 JSON。

验收口径是 `docs/xiaozhi拆解.md` §2.2.1 第 5 项原文：

    用假客户端连上去能收到应答；**断开时不留悬挂任务**

第二条是这里的重点 —— 除了 happy path，专门盯这几件事：

- 收工**只有一条路径**（正常收工 / 对端断开 / hello 超时 / 被取消 / 应答里抛异常），
  都得走到同一个 finally，把连接关掉、把任务收拾干净；
- 握手之后**不许再编超时**（模型慢一点、用户想一会儿都不该被我们掐掉）——
  所以"半路卡住"是**对的**，这里用一个会超时的断言把它钉住。
"""

from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from xiaoyu.logger import get_logger
from xiaoyu.xiaozhi import protocol
from xiaoyu.xiaozhi.audio_codec import OpusCodec, create_opus_codec
from xiaoyu.xiaozhi.server import (
    Connection,
    FakeTransport,
    FixedResponder,
    Responder,
    Transport,
    TurnRequest,
)
from xiaoyu.xiaozhi.session import Action, ActionKind, SessionState

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "xiaozhi"

DEVICE_HELLO = "device/device_hello.json"
LISTEN_START_AUTO = "device/device_listen_start_auto.json"
ABORT = "device/device_abort.json"

SESSION_ID = "s-server-test"

# 设备上行的一帧（16000Hz / 60ms / 单声道 / s16le）
UPLINK_FRAME = b"\xaa" * 1920
# 我们下行的一帧（24000Hz / 60ms）—— FixedResponder 默认回一帧静音
DOWNLINK_FRAME_BYTES = 2880


def _text(rel: str) -> str:
    return (FIXTURES / rel).read_text(encoding="utf-8")


def _json(rel: str) -> dict:
    return json.loads(_text(rel))


def _listen_state(state: str) -> str:
    """按文档 §4.1.2 的 listen 形状造一条消息。

    `start` / `detect` 有真样本，**`stop` 文档没给样本** —— 所以从真样本复制字段、
    只改 `state`，不自己编字段（编了就成了"用自己的理解测自己的理解"）。
    """
    message = _json(LISTEN_START_AUTO)
    message["state"] = state
    return json.dumps(message, ensure_ascii=False)


def _codec() -> OpusCodec:
    """按设备真 hello 的参数建一个假编解码器（真依赖还没装）。"""
    hello = protocol.parse_device_hello(_text(DEVICE_HELLO)).value
    return create_opus_codec(hello.audio_params, allow_fake=True)


class BoomResponder:
    """应答时炸掉 —— 验"异常也不能漏掉收工"。"""

    async def respond(self, request: TurnRequest):
        raise RuntimeError("应答炸了")
        yield b""  # 让它是个异步生成器


def _connection(transport: Transport, **kwargs) -> Connection:
    kwargs.setdefault("session_id", SESSION_ID)
    return Connection(transport, codec=_codec(), **kwargs)


class TransportContractTest(unittest.TestCase):
    """假连接得真的像一条连接，不然下面全是自欺欺人。"""

    def test_fake_transport_satisfies_the_protocol(self):
        self.assertIsInstance(FakeTransport(), Transport)

    def test_fixed_responder_satisfies_the_protocol(self):
        self.assertIsInstance(FixedResponder(), Responder)

    def test_fixed_responder_rejects_zero_frames(self):
        """一帧都不发就等于没有应答 —— 这种"看着配好了其实什么都没发"要拦住。"""
        with self.assertRaises(ValueError):
            FixedResponder(frames=0)

    def test_send_after_close_raises(self):
        """收工路径漏了一处的话，这条会当场抓住。"""
        transport = FakeTransport()

        async def scenario():
            await transport.close()
            await transport.send("x")

        with self.assertRaises(RuntimeError):
            asyncio.run(scenario())


class HandshakeTest(unittest.IsolatedAsyncioTestCase):
    async def test_device_hello_gets_our_hello_back(self):
        transport = FakeTransport([_text(DEVICE_HELLO)])
        transport.disconnect()
        conn = _connection(transport)

        session = await conn.run()

        self.assertIs(session.state, SessionState.CLOSED)
        self.assertEqual(len(transport.text_frames), 1)
        reply = json.loads(transport.text_frames[0])
        self.assertEqual(reply["type"], "hello")
        self.assertEqual(reply["session_id"], SESSION_ID)
        self.assertEqual(reply["transport"], "websocket")
        # §8.3：下行 24000
        self.assertEqual(reply["audio_params"]["sample_rate"], 24000)
        self.assertIsNotNone(session.device_hello)

    async def test_our_hello_is_a_text_frame_not_binary(self):
        transport = FakeTransport([_text(DEVICE_HELLO)])
        transport.disconnect()
        await _connection(transport).run()
        self.assertEqual(transport.binary_frames, [])

    async def test_bad_hello_closes_instead_of_hanging(self):
        transport = FakeTransport(["{not json at all"])
        transport.disconnect()
        session = await _connection(transport).run()
        self.assertIs(session.state, SessionState.CLOSED)
        self.assertEqual(transport.text_frames, [])

    async def test_hello_without_a_version_closes(self):
        transport = FakeTransport(['{"type": "hello"}'])
        transport.disconnect()
        session = await _connection(transport).run()
        self.assertIs(session.state, SessionState.CLOSED)


class TimeoutTest(unittest.IsolatedAsyncioTestCase):
    async def test_silent_device_times_out(self):
        """连上了但一直不说话 —— 10 秒（这里调小）就收工。"""
        transport = FakeTransport()
        session = await _connection(transport, hello_timeout=0.02).run()
        self.assertIs(session.state, SessionState.CLOSED)
        self.assertIn("hello", session.closed_reason)

    async def test_timeout_does_not_apply_after_the_handshake(self):
        """握手之后**不许再编超时** —— 卡住是对的，不是 bug。

        模型慢一点、用户想一会儿，都不该被我们掐掉（session.py 同款立场）。
        所以这里反过来断言：喂完 hello 就没下文，`run()` 必须一直不返回。
        """
        transport = FakeTransport([_text(DEVICE_HELLO)])
        conn = _connection(transport, hello_timeout=0.01)
        with self.assertRaises(TimeoutError):
            await asyncio.wait_for(conn.run(), 0.2)
        # 被我们强行取消之后，收工路径照样走完了
        self.assertGreaterEqual(transport.close_calls, 1)
        self.assertTrue(transport.closed)


class TurnTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_full_turn_sends_audio_back(self):
        """hello -> listen(start) -> 音频 -> listen(stop) -> 收我们的音频 -> 断开。"""
        transport = FakeTransport(
            [
                _text(DEVICE_HELLO),
                _text(LISTEN_START_AUTO),
                UPLINK_FRAME,
                _listen_state("stop"),
            ]
        )
        transport.disconnect()
        session = await _connection(transport).run()

        self.assertIs(session.state, SessionState.CLOSED)
        # 应答真的发出了：一帧下行音频，正好 24000Hz/60ms 的 2880 字节
        self.assertEqual(len(transport.binary_frames), 1)
        self.assertEqual(len(transport.binary_frames[0]), DOWNLINK_FRAME_BYTES)
        # 而且是**静音**，不是把设备说的原样弹回去（骨架不是回声）
        self.assertEqual(transport.binary_frames[0], b"\x00" * DOWNLINK_FRAME_BYTES)

    async def test_responder_frame_count_is_respected(self):
        transport = FakeTransport(
            [_text(DEVICE_HELLO), _text(LISTEN_START_AUTO), _listen_state("stop")]
        )
        transport.disconnect()
        conn = _connection(transport, responder=FixedResponder(frames=3))
        await conn.run()
        self.assertEqual(len(transport.binary_frames), 3)
        self.assertEqual(conn.sent_audio_frames, 3)
        self.assertTrue(all(len(f) == DOWNLINK_FRAME_BYTES for f in transport.binary_frames))

    async def test_after_speaking_we_go_back_to_listening(self):
        """说完了要告诉状态机（SPEAK_END），不然手动模式下会话永远卡在 SPEAKING。"""
        transport = FakeTransport(
            [_text(DEVICE_HELLO), _text(LISTEN_START_AUTO), _listen_state("stop")]
        )
        conn = _connection(transport)
        task = asyncio.create_task(conn.run())
        for _ in range(50):
            await asyncio.sleep(0)
            if conn.sent_audio_frames:
                break
        # 说完了、断线之前，状态应该已经回到「设备在收音」
        self.assertIsNot(conn.session.state, SessionState.SPEAKING)
        transport.disconnect()
        await task

    async def test_garbage_text_does_not_kill_the_session(self):
        transport = FakeTransport(
            [
                _text(DEVICE_HELLO),
                "{not json",
                _text(LISTEN_START_AUTO),
                _listen_state("stop"),
            ]
        )
        transport.disconnect()
        conn = _connection(transport)
        session = await conn.run()
        # 坏消息被记了一笔，但会话照常走完了一轮
        self.assertGreaterEqual(session.ignored_count, 1)
        self.assertEqual(len(transport.binary_frames), 1)

    async def test_abort_before_speaking_is_harmless(self):
        transport = FakeTransport([_text(DEVICE_HELLO), _text(ABORT)])
        transport.disconnect()
        session = await _connection(transport).run()
        self.assertIs(session.state, SessionState.CLOSED)


class ShutdownTest(unittest.IsolatedAsyncioTestCase):
    async def test_peer_disconnect_closes_cleanly(self):
        transport = FakeTransport()
        transport.disconnect()
        session = await _connection(transport).run()
        self.assertEqual(session.closed_reason, "对端断开")
        self.assertEqual(transport.close_calls, 1)

    async def test_transport_is_closed_exactly_once_on_the_happy_path(self):
        """收工只有一条路径 —— 关两次说明有两处各关了一次。"""
        transport = FakeTransport([_text(DEVICE_HELLO)])
        transport.disconnect()
        await _connection(transport).run()
        self.assertEqual(transport.close_calls, 1)

    async def test_no_tasks_are_left_running(self):
        """§2.2.1 第 5 项的验收：「断开时不留悬挂任务」。"""
        transport = FakeTransport([_text(DEVICE_HELLO), _text(LISTEN_START_AUTO), UPLINK_FRAME])
        transport.disconnect()
        await _connection(transport).run()

        current = asyncio.current_task()
        leftovers = [t for t in asyncio.all_tasks() if t is not current and not t.done()]
        self.assertEqual(leftovers, [])

    async def test_responder_exception_still_closes_the_transport(self):
        """应答里炸了也不能漏掉收工 —— finally 是唯一出口，这条盯着它。"""
        transport = FakeTransport([_text(DEVICE_HELLO), _text(LISTEN_START_AUTO), _listen_state("stop")])
        conn = _connection(transport, responder=BoomResponder())
        with self.assertRaises(RuntimeError):
            await conn.run()
        self.assertTrue(transport.closed)

    async def test_run_returns_the_session(self):
        transport = FakeTransport()
        transport.disconnect()
        conn = _connection(transport)
        self.assertIs(await conn.run(), conn.session)

    async def test_closed_mirrors_the_session_state(self):
        transport = FakeTransport()
        transport.disconnect()
        conn = _connection(transport)
        self.assertFalse(conn.closed)
        await conn.run()
        self.assertTrue(conn.closed)


class ActionDispatchTest(unittest.IsolatedAsyncioTestCase):
    """状态机要求的动作，驱动层必须**每一个都接上** —— 漏一个就是静默失灵。"""

    # 每种动作都配一个**形状对得上**的载荷 —— 拿错形状会先炸在别处，
    # 那样测到的就不是"接没接上"了
    PAYLOADS = {
        ActionKind.SEND_MESSAGE: {"type": "test"},
        ActionKind.RUN_TURN: None,
        ActionKind.STOP_SPEAKING: None,
        ActionKind.CLOSE: "测试收工",
    }

    def _capture_warnings(self) -> list[str]:
        """把 WARNING 收进一个列表（loguru 是单例，用完必须 remove）。"""
        messages: list[str] = []
        sink = get_logger("test").add(
            lambda message: messages.append(message), level="WARNING"
        )
        self.addCleanup(get_logger("test").remove, sink)
        return messages

    async def test_every_action_kind_is_handled(self):
        transport = FakeTransport()
        conn = _connection(transport)
        messages = self._capture_warnings()
        for kind in ActionKind:
            with self.subTest(kind=kind.value):
                await conn._execute([Action(kind, self.PAYLOADS[kind])])
        self.assertEqual(
            [m for m in messages if "认不得的动作" in m], [],
            "有动作没接上 —— 状态机加了新动作，驱动层忘了处理",
        )

    async def test_an_unhandled_action_is_not_swallowed_silently(self):
        """状态机将来加了新动作、驱动层忘了接 —— 要告警，不许静默吞掉。

        拿 `SimpleNamespace` 冒充"一个 kind 我们认不得的 Action"：
        直接塞个非 Action 对象会先炸在 `action.kind` 上，测不到 else 这一支。
        """
        transport = FakeTransport()
        conn = _connection(transport)
        messages = self._capture_warnings()
        await conn._execute([SimpleNamespace(kind="还没有的动作", payload=None)])
        self.assertTrue(any("认不得的动作" in m for m in messages))
