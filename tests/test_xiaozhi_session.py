"""小智会话状态机（`xiaoyu/xiaozhi/session.py`）的测试。

跑法：py -3.11 -m unittest discover tests

全程**离线**：不连板子、不联网。设备侧消息全部来自 `tests/fixtures/xiaozhi/`
（逐字抄自上游协议文档），不是自己编的 JSON。

验收口径是 `docs/xiaozhi拆解.md` §2.2.1 第 3 项：单测走一遍正常流转 + 乱序消息 +
超时 + 中途断线，**状态不许卡死**。所以除了 happy path，这里有两组刁难用例：

- **状态 × 事件全矩阵**：任何一个状态吃到任何一个事件，都不许抛、不许变成未知状态，
  而且**之后还一定能收工** —— 「不许卡死」这条就是这么验的；
- **收工之后再来事件**：必须一直待在 CLOSED，不许被迟到的事件拉活。
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from xiaoyu.xiaozhi.protocol import AudioParams, DeviceHello
from xiaoyu.xiaozhi.session import (
    HELLO_TIMEOUT_SECONDS,
    Action,
    ActionKind,
    Event,
    EventKind,
    Session,
    SessionState,
    event_from_frame,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "xiaozhi"

DEVICE_HELLO_FULL = "device/device_hello_full.json"
DEVICE_HELLO = "device/device_hello.json"
SERVER_HELLO_24000 = "server/server_hello_24000.json"
LISTEN_START_MANUAL = "device/device_listen_start_manual.json"
LISTEN_START_AUTO = "device/device_listen_start_auto.json"
LISTEN_DETECT = "device/device_listen_detect.json"
ABORT = "device/device_abort.json"
MCP_RESULT = "device/device_mcp_result.json"

SESSION_ID = "s-test"

# 每种事件配一个"最不客气"的载荷 —— 全矩阵用例拿它喂所有状态
HOSTILE = object()


def _text(rel: str) -> str:
    return (FIXTURES / rel).read_text(encoding="utf-8")


def _json(rel: str) -> dict:
    return json.loads(_text(rel))


def _listen(state: str) -> dict:
    """按文档 §4.1.2 的 listen 形状造一条消息。

    `start` / `detect` 有真样本，**`stop` 文档没给样本** —— 所以从真样本复制字段、
    只改 `state`，不自己编字段（编了就成了"用自己的理解测自己的理解"）。
    """
    message = _json(LISTEN_START_AUTO)
    message["state"] = state
    return message


class FakeClock:
    """可注入的时钟：超时能被精确驱动，不用真等 10 秒。"""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def drive_to(target: SessionState) -> tuple[Session, FakeClock]:
    """用**真实事件序列**把一条新会话推到目标状态（不直接改 state）。"""
    clock = FakeClock()
    session = Session(session_id=SESSION_ID, clock=clock)
    if target is SessionState.CONNECTING:
        return session, clock

    session.handle(Event(EventKind.DEVICE_HELLO, _text(DEVICE_HELLO)))
    if target is SessionState.HANDSHAKING:
        return session, clock

    session.handle(Event(EventKind.TEXT_MESSAGE, _text(LISTEN_START_AUTO)))
    if target is SessionState.LISTENING:
        return session, clock

    session.handle(Event(EventKind.TEXT_MESSAGE, _listen("stop")))
    if target is SessionState.SPEAKING:
        return session, clock

    session.handle(Event(EventKind.CLOSED, "测试收工"))
    assert session.state is SessionState.CLOSED, session.state
    return session, clock


class SessionTestCase(unittest.TestCase):
    """共同脚手架：一条会话 + 一个假时钟 + 几个把状态推过去的快捷方式。"""

    def make(self, **kwargs) -> Session:
        self.clock = FakeClock()
        self.session = Session(session_id=SESSION_ID, clock=self.clock, **kwargs)
        return self.session

    def feed(self, kind: EventKind, payload=None) -> list[Action]:
        return self.session.handle(Event(kind, payload))

    def feed_text(self, rel: str) -> list[Action]:
        return self.feed(EventKind.TEXT_MESSAGE, _text(rel))

    def handshake(self) -> list[Action]:
        actions = self.feed(EventKind.DEVICE_HELLO, _text(DEVICE_HELLO))
        self.assertIs(self.session.state, SessionState.HANDSHAKING)
        return actions

    def to_speaking(self) -> None:
        self.handshake()
        self.feed_text(LISTEN_START_AUTO)
        self.feed(EventKind.TEXT_MESSAGE, _listen("stop"))
        self.assertIs(self.session.state, SessionState.SPEAKING)


class NormalFlowTest(SessionTestCase):
    """§6 的正常流转：connecting -> handshaking -> listening -> speaking -> closed。"""

    def test_full_round_trip(self):
        self.make()
        self.assertIs(self.session.state, SessionState.CONNECTING)

        actions = self.feed(EventKind.DEVICE_HELLO, _text(DEVICE_HELLO_FULL))
        self.assertIs(self.session.state, SessionState.HANDSHAKING)
        self.assertEqual([a.kind for a in actions], [ActionKind.SEND_MESSAGE])

        self.feed_text(LISTEN_START_AUTO)
        self.assertIs(self.session.state, SessionState.LISTENING)

        actions = self.feed(EventKind.TEXT_MESSAGE, _listen("stop"))
        self.assertIs(self.session.state, SessionState.SPEAKING)
        self.assertEqual([a.kind for a in actions], [ActionKind.RUN_TURN])

        actions = self.feed(EventKind.SPEAK_END)
        self.assertIs(self.session.state, SessionState.LISTENING)
        self.assertEqual(actions, [])

        actions = self.feed(EventKind.CLOSED, "设备主动断开")
        self.assertIs(self.session.state, SessionState.CLOSED)
        self.assertEqual([a.kind for a in actions], [ActionKind.CLOSE])
        self.assertEqual(self.session.closed_reason, "设备主动断开")

    def test_our_hello_is_built_by_the_protocol_layer(self):
        """回给设备的那条 hello，字段要和协议文档 §1.4 一致（不自己拼 dict）。"""
        self.make()
        (action,) = self.handshake()
        self.assertEqual(
            action.payload,
            {
                "type": "hello",
                "transport": "websocket",
                "session_id": SESSION_ID,
                "audio_params": {
                    "format": "opus",
                    "sample_rate": 24000,
                    "channels": 1,
                    "frame_duration": 60,
                },
            },
        )

    def test_parsed_device_hello_is_kept_for_the_caller(self):
        self.make()
        self.handshake()
        hello = self.session.device_hello
        self.assertIsInstance(hello, DeviceHello)
        self.assertEqual(hello.version, 1)
        self.assertEqual(hello.audio_params.sample_rate, 16000)

    def test_listen_start_manual_auto_and_detect_all_enter_listening(self):
        for rel in (LISTEN_START_MANUAL, LISTEN_START_AUTO, LISTEN_DETECT):
            with self.subTest(fixture=rel):
                self.make()
                self.handshake()
                self.feed_text(rel)
                self.assertIs(self.session.state, SessionState.LISTENING)

    def test_second_turn_via_device_auto_relisten(self):
        """§6 自动模式：我们说完 -> 设备自己重新收音 -> 再来一轮。"""
        self.make()
        self.to_speaking()
        self.feed_text(LISTEN_START_AUTO)
        self.assertIs(self.session.state, SessionState.LISTENING)

        actions = self.feed(EventKind.TEXT_MESSAGE, _listen("stop"))
        self.assertEqual([a.kind for a in actions], [ActionKind.RUN_TURN])
        self.assertIs(self.session.state, SessionState.SPEAKING)

    def test_default_session_id_is_unique_and_non_empty(self):
        first, second = Session().session_id, Session().session_id
        self.assertTrue(first and second)
        self.assertNotEqual(first, second)

    def test_repr_is_readable(self):
        self.make()
        self.assertIn(SESSION_ID, repr(self.session))
        self.assertIn("connecting", repr(self.session))


class HandshakeTest(SessionTestCase):
    def test_server_hello_is_not_a_device_hello(self):
        """两个方向的 hello 字段不同（§1.4 的服务端 hello 没有 version）。"""
        self.make()
        self.feed(EventKind.DEVICE_HELLO, _text(SERVER_HELLO_24000))
        self.assertIs(self.session.state, SessionState.CLOSED)
        self.assertIsNotNone(self.session.last_error)
        self.assertEqual(self.session.last_error.code, "missing_field")

    def test_bad_hello_closes_with_a_reason(self):
        for payload in ("{不是 JSON", "{}", "[1,2,3]"):
            with self.subTest(payload=payload):
                self.make()
                actions = self.feed(EventKind.DEVICE_HELLO, payload)
                self.assertIs(self.session.state, SessionState.CLOSED)
                self.assertEqual([a.kind for a in actions], [ActionKind.CLOSE])
                self.assertIn("hello", self.session.closed_reason)

    def test_preparsed_device_hello_is_accepted(self):
        """调用方手上有解析好的 DeviceHello 时，不用再序列化一遍。"""
        self.make()
        hello = DeviceHello(
            version=1,
            transport="websocket",
            audio_params=AudioParams(format="opus", sample_rate=16000),
        )
        actions = self.feed(EventKind.DEVICE_HELLO, hello)
        self.assertIs(self.session.state, SessionState.HANDSHAKING)
        self.assertEqual(len(actions), 1)

    def test_duplicate_hello_is_ignored_not_fatal(self):
        self.make()
        self.handshake()
        actions = self.feed(EventKind.DEVICE_HELLO, _text(DEVICE_HELLO))
        self.assertEqual(actions, [])
        self.assertIs(self.session.state, SessionState.HANDSHAKING)
        self.assertEqual(self.session.ignored_count, 1)

    def test_hello_while_listening_is_ignored(self):
        self.make()
        self.handshake()
        self.feed_text(LISTEN_START_AUTO)
        self.feed(EventKind.DEVICE_HELLO, _text(DEVICE_HELLO))
        self.assertIs(self.session.state, SessionState.LISTENING)
        self.assertEqual(self.session.ignored_count, 1)


class TimeoutTest(SessionTestCase):
    """超时只做「等 hello 的 10 秒」—— 协议文档 §1.4 / 上游源码 pdMS_TO_TICKS(10000)。"""

    def test_default_is_the_documented_ten_seconds(self):
        self.assertEqual(HELLO_TIMEOUT_SECONDS, 10.0)

    def test_no_action_before_the_deadline(self):
        self.make()
        self.clock.advance(HELLO_TIMEOUT_SECONDS - 0.1)
        self.assertEqual(self.session.poll(), [])
        self.assertIs(self.session.state, SessionState.CONNECTING)

    def test_closes_at_the_deadline(self):
        self.make()
        self.clock.advance(HELLO_TIMEOUT_SECONDS)
        actions = self.session.poll()
        self.assertIs(self.session.state, SessionState.CLOSED)
        self.assertEqual([a.kind for a in actions], [ActionKind.CLOSE])
        self.assertIn("hello", self.session.closed_reason)

    def test_default_clock_does_not_fire_immediately(self):
        """不注入时钟（真实 monotonic）时，刚建好的会话不该立刻被超时收掉。"""
        session = Session(session_id=SESSION_ID)
        self.assertEqual(session.poll(), [])
        self.assertIs(session.state, SessionState.CONNECTING)

    def test_poll_uses_the_injected_clock(self):
        """时间只从注入的 clock 取 —— 整条链路都靠它驱动，别的地方不许另算一遍。"""
        self.make()
        self.clock.advance(HELLO_TIMEOUT_SECONDS - 0.001)
        self.assertEqual(self.session.poll(), [])
        self.clock.advance(0.001)
        self.assertEqual(len(self.session.poll()), 1)

    def test_custom_timeout_is_honoured(self):
        self.make(hello_timeout=1.0)
        self.clock.advance(1.0)
        self.assertEqual(len(self.session.poll()), 1)

    def test_poll_is_a_noop_after_handshake(self):
        """握完手之后就不该再有 hello 超时了（否则用着用着会被自己掐掉）。"""
        self.make()
        self.handshake()
        self.clock.advance(HELLO_TIMEOUT_SECONDS * 100)
        self.assertEqual(self.session.poll(), [])
        self.assertIs(self.session.state, SessionState.HANDSHAKING)

    def test_poll_is_a_noop_after_close(self):
        self.make()
        self.feed(EventKind.CLOSED, "断开")
        self.clock.advance(HELLO_TIMEOUT_SECONDS * 100)
        self.assertEqual(self.session.poll(), [])
        self.assertIs(self.session.state, SessionState.CLOSED)


class AudioFrameTest(SessionTestCase):
    def test_audio_in_handshaking_is_ok(self):
        """§4.1.4：唤醒词的 Opus 数据会先于 listen/detect 发过来 —— 不是乱序。"""
        self.make()
        self.handshake()
        self.assertEqual(self.feed(EventKind.AUDIO_FRAME, b"\x01\x02"), [])
        self.assertIs(self.session.state, SessionState.HANDSHAKING)
        self.assertEqual(self.session.ignored_count, 0)

    def test_audio_in_listening_is_ok(self):
        self.make()
        self.handshake()
        self.feed_text(LISTEN_START_AUTO)
        self.assertEqual(self.feed(EventKind.AUDIO_FRAME, b"\x01\x02"), [])
        self.assertIs(self.session.state, SessionState.LISTENING)
        self.assertEqual(self.session.ignored_count, 0)

    def test_audio_before_hello_is_ignored(self):
        self.make()
        self.feed(EventKind.AUDIO_FRAME, b"\x01")
        self.assertIs(self.session.state, SessionState.CONNECTING)
        self.assertEqual(self.session.ignored_count, 1)

    def test_audio_while_we_are_speaking_is_ignored(self):
        """我们在放音时设备不该再传音频（协议文档 §4.2.8 的同一条道理）。"""
        self.make()
        self.to_speaking()
        self.feed(EventKind.AUDIO_FRAME, b"\x01")
        self.assertIs(self.session.state, SessionState.SPEAKING)
        self.assertEqual(self.session.ignored_count, 1)


class InterruptTest(SessionTestCase):
    def test_abort_while_speaking_stops_us(self):
        self.make()
        self.to_speaking()
        actions = self.feed_text(ABORT)
        self.assertEqual([a.kind for a in actions], [ActionKind.STOP_SPEAKING])
        self.assertIs(self.session.state, SessionState.LISTENING)

    def test_wake_word_detect_while_speaking_interrupts(self):
        self.make()
        self.to_speaking()
        actions = self.feed_text(LISTEN_DETECT)
        self.assertEqual([a.kind for a in actions], [ActionKind.STOP_SPEAKING])
        self.assertIs(self.session.state, SessionState.LISTENING)

    def test_listen_start_while_speaking_is_not_an_interrupt(self):
        """设备在我们说完之后自动重新收音（§6 自动模式）—— 不是打断，别乱闭麦。"""
        self.make()
        self.to_speaking()
        actions = self.feed_text(LISTEN_START_AUTO)
        self.assertEqual(actions, [])
        self.assertIs(self.session.state, SessionState.LISTENING)

    def test_abort_while_listening_is_ignored(self):
        self.make()
        self.handshake()
        self.feed_text(LISTEN_START_AUTO)
        self.assertEqual(self.feed_text(ABORT), [])
        self.assertIs(self.session.state, SessionState.LISTENING)
        self.assertEqual(self.session.ignored_count, 1)

    def test_abort_before_handshake_is_ignored(self):
        self.make()
        self.assertEqual(self.feed_text(ABORT), [])
        self.assertIs(self.session.state, SessionState.CONNECTING)
        self.assertEqual(self.session.ignored_count, 1)

    def test_a_new_turn_can_start_after_an_interrupt(self):
        self.make()
        self.to_speaking()
        self.feed_text(ABORT)
        actions = self.feed(EventKind.TEXT_MESSAGE, _listen("stop"))
        self.assertEqual([a.kind for a in actions], [ActionKind.RUN_TURN])
        self.assertIs(self.session.state, SessionState.SPEAKING)


class OutOfOrderTest(SessionTestCase):
    """乱序 / 重复 / 认不得 —— 一律忽略 + 记一笔，状态不许被带歪。"""

    def test_listen_stop_before_handshake_is_ignored(self):
        self.make()
        self.assertEqual(self.feed(EventKind.TEXT_MESSAGE, _listen("stop")), [])
        self.assertIs(self.session.state, SessionState.CONNECTING)
        self.assertEqual(self.session.ignored_count, 1)

    def test_duplicate_listen_start_is_ignored(self):
        self.make()
        self.handshake()
        self.feed_text(LISTEN_START_AUTO)
        self.feed_text(LISTEN_START_AUTO)
        self.assertIs(self.session.state, SessionState.LISTENING)
        self.assertEqual(self.session.ignored_count, 1)

    def test_speak_end_while_listening_is_ignored(self):
        self.make()
        self.handshake()
        self.feed_text(LISTEN_START_AUTO)
        self.assertEqual(self.feed(EventKind.SPEAK_END), [])
        self.assertIs(self.session.state, SessionState.LISTENING)
        self.assertEqual(self.session.ignored_count, 1)

    def test_unknown_listen_state_is_ignored(self):
        self.make()
        self.handshake()
        self.feed(EventKind.TEXT_MESSAGE, _listen("正在发生的新状态"))
        self.assertIs(self.session.state, SessionState.HANDSHAKING)
        self.assertEqual(self.session.ignored_count, 1)

    def test_known_but_irrelevant_message_is_not_counted_as_ignored(self):
        """mcp 这类消息是合法的，只是不影响会话状态 —— 不该记成「忽略」。"""
        self.make()
        self.handshake()
        self.assertEqual(self.feed_text(MCP_RESULT), [])
        self.assertIs(self.session.state, SessionState.HANDSHAKING)
        self.assertEqual(self.session.ignored_count, 0)

    def test_every_state_survives_every_event(self):
        """**全矩阵**：任何状态吃到任何事件 —— 不抛、不变未知状态、之后还能收工。"""
        for target in SessionState:
            for kind in EventKind:
                with self.subTest(state=target.value, event=kind.value):
                    session, _clock = drive_to(target)
                    session.handle(Event(kind, HOSTILE))
                    self.assertIn(session.state, set(SessionState))
                    if session.state is SessionState.CLOSED:
                        self.assertIsNotNone(session.closed_reason)
                    # 不管刚才发生了什么，都必须还能收工 —— 这就是「不许卡死」
                    session.handle(Event(EventKind.CLOSED, "收工"))
                    self.assertIs(session.state, SessionState.CLOSED)


class ClosedTest(SessionTestCase):
    def test_closed_is_reachable_from_every_state(self):
        for target in SessionState:
            with self.subTest(state=target.value):
                session, _clock = drive_to(target)
                actions = session.handle(Event(EventKind.CLOSED, "收工"))
                self.assertIs(session.state, SessionState.CLOSED)
                if target is SessionState.CLOSED:
                    # 已经在终态了：不该重复"收工"（调用方可能已经在清理了）
                    self.assertEqual(actions, [])
                else:
                    self.assertEqual([a.kind for a in actions], [ActionKind.CLOSE])

    def test_closed_is_terminal(self):
        """收工之后再来什么都得待在 CLOSED，不许被迟到的事件拉活。"""
        self.make()
        self.handshake()
        self.feed(EventKind.CLOSED, "断开")
        for kind in EventKind:
            with self.subTest(event=kind.value):
                self.assertEqual(self.feed(kind, _text(DEVICE_HELLO)), [])
                self.assertIs(self.session.state, SessionState.CLOSED)

    def test_closed_default_reason(self):
        self.make()
        self.feed(EventKind.CLOSED)
        self.assertTrue(self.session.closed_reason)

    def test_close_action_carries_the_reason(self):
        self.make()
        (action,) = self.feed(EventKind.CLOSED, "网络断了")
        self.assertEqual(action.kind, ActionKind.CLOSE)
        self.assertEqual(action.payload, "网络断了")


class GarbageToleranceTest(SessionTestCase):
    """设备那头只说「连不上」，原因得由我们这侧讲清楚 —— 所以解析失败绝不抛。"""

    GARBAGE = (
        None, 0, 3.14, True, [], {}, object(),
        b"", b"\xff\xfe", bytearray(b"\x01"),
        "", "{", "}", "[]", "null", "123",
        '{"state": "start"}', '{"type": null}', '{"type": "listen"}',
        '{"type": "listen", "state": 3}', '{"type": "abort", "reason": 1}',
    )

    def test_text_messages_never_raise(self):
        for payload in self.GARBAGE:
            with self.subTest(payload=repr(payload)[:60]):
                self.make()
                self.handshake()
                actions = self.feed(EventKind.TEXT_MESSAGE, payload)
                self.assertEqual(actions, [])
                self.assertIn(self.session.state, set(SessionState))

    def test_bad_json_is_recorded_but_not_fatal(self):
        self.make()
        self.handshake()
        self.feed(EventKind.TEXT_MESSAGE, "{这不是 JSON")
        self.assertIs(self.session.state, SessionState.HANDSHAKING)
        self.assertIsNotNone(self.session.last_error)
        self.assertEqual(self.session.ignored_count, 1)

    def test_listen_with_non_string_state_is_ignored(self):
        self.make()
        self.handshake()
        self.feed(EventKind.TEXT_MESSAGE, {"type": "listen", "state": 3})
        self.assertIs(self.session.state, SessionState.HANDSHAKING)
        self.assertEqual(self.session.ignored_count, 1)

    def test_missing_state_is_ignored(self):
        self.make()
        self.handshake()
        self.feed(EventKind.TEXT_MESSAGE, {"type": "listen"})
        self.assertIs(self.session.state, SessionState.HANDSHAKING)
        self.assertEqual(self.session.ignored_count, 1)


class EventFromFrameTest(unittest.TestCase):
    """`event_from_frame`：把 WebSocket 的一帧翻成事件。

    文本帧**不是一律** TEXT_MESSAGE —— 设备 hello 也是文本帧，但它是握手那一步
    唯一认的信号，得单独翻成 DEVICE_HELLO。这条曾经是错的（hello 被当普通文本
    忽略掉，会话一直卡在 CONNECTING 到 10 秒超时），下面有回归用例盯着。
    """

    def test_text_frame(self):
        event = event_from_frame(_text(LISTEN_START_AUTO))
        self.assertIs(event.kind, EventKind.TEXT_MESSAGE)

    def test_device_hello_text_frame_becomes_a_hello_event(self):
        event = event_from_frame(_text(DEVICE_HELLO))
        self.assertIs(event.kind, EventKind.DEVICE_HELLO)
        # payload 留**原始文本** —— 解析是状态机的活，这里不替它解析
        self.assertIsInstance(event.payload, str)

    def test_hello_frame_completes_the_handshake(self):
        """回归：hello 必须是 DEVICE_HELLO 事件，否则握手永远走不完。"""
        session = Session(session_id=SESSION_ID)
        actions = session.handle(event_from_frame(_text(DEVICE_HELLO)))
        self.assertIs(session.state, SessionState.HANDSHAKING)
        self.assertEqual([a.kind for a in actions], [ActionKind.SEND_MESSAGE])
        self.assertEqual(session.ignored_count, 0)

    def test_hello_shaped_but_invalid_still_closes_instead_of_hanging(self):
        """自称 hello 但缺 version/transport —— 要**明确收工**，不是干等超时。"""
        session = Session(session_id=SESSION_ID)
        actions = session.handle(event_from_frame('{"type": "hello"}'))
        self.assertIs(session.state, SessionState.CLOSED)
        self.assertEqual([a.kind for a in actions], [ActionKind.CLOSE])

    def test_unparseable_text_stays_a_text_message(self):
        """坏 JSON 不该在**这里**改变事件种类 —— 它有自己的处理路径（忽略 + 记账）。"""
        for bad in ("{not json", "[1, 2]", "null", "hello", "''"):
            with self.subTest(bad=bad):
                self.assertIs(event_from_frame(bad).kind, EventKind.TEXT_MESSAGE)

    def test_a_bad_message_does_not_kill_the_session(self):
        session = Session(session_id=SESSION_ID)
        session.handle(event_from_frame(_text(DEVICE_HELLO)))
        session.handle(event_from_frame("{not json"))
        self.assertIs(session.state, SessionState.HANDSHAKING)
        self.assertEqual(session.ignored_count, 1)

    def test_binary_frame(self):
        for payload in (b"\x01\x02", bytearray(b"\x01"), memoryview(b"\x01")):
            with self.subTest(payload=repr(payload)):
                self.assertIs(event_from_frame(payload).kind, EventKind.AUDIO_FRAME)

    def test_unrecognised_frame(self):
        for payload in (None, 123, [], {}, object()):
            with self.subTest(payload=repr(payload)):
                self.assertIs(event_from_frame(payload).kind, EventKind.UNKNOWN)

    def test_unknown_event_is_ignored_by_the_machine(self):
        session = Session(session_id=SESSION_ID)
        self.assertEqual(session.handle(Event(EventKind.UNKNOWN, b"")), [])
        self.assertIs(session.state, SessionState.CONNECTING)
        self.assertEqual(session.ignored_count, 1)

    def test_audio_frame_payload_reaches_the_caller_unchanged(self):
        """音频是给 ASR 的，状态机不该动它（也别拷贝成别的东西）。"""
        payload = b"\x11\x22\x33"
        self.assertIs(event_from_frame(payload).payload, payload)


if __name__ == "__main__":
    unittest.main()
