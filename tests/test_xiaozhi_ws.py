"""小智真网络层（`xiaoyu/xiaozhi/ws.py`）的测试。

三层，从里到外：

1. `ReplayTransport` / `WebsocketTransport` —— 纯逻辑：把已经读掉的帧塞回去、
   把 websockets 的连接包成 `Transport` 的契约（对端断开返回 None，不抛）。
2. OTA 的 HTTP 端点 —— **必须是 POST 也能收**（上游 `ota.cc` 发的是 POST + body，
   而 websockets 的 HTTP 层不收 body，所以才分成两个口，见 ws.py 开头「坑 4」）。
3. **真起服务 + 真客户端跑一遍端到端** —— 一个 POST 到 OTA 口拿 ws 地址，
   再照那个地址连 WebSocket 口走完握手和一轮对话。端口全部由系统分配，**绝不碰 8765**。

第 3 层用的编解码器是**假货**（`allow_fake=True`）：真 Opus 由
`tests/test_xiaozhi_opus_av.py` 单独验，这里验的是网络和状态机接得对不对。
"""

from __future__ import annotations

import asyncio
import json
import socket
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from xiaoyu.config import XiaozhiConfig
from xiaoyu.xiaozhi import ota
from xiaoyu.xiaozhi.audio_codec import create_opus_codec
from xiaoyu.xiaozhi.server import FixedResponder
from xiaoyu.xiaozhi.ws import ReplayTransport, WebsocketTransport, XiaozhiServer

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "xiaozhi"
DEVICE_HELLO = "device/device_hello.json"
LISTEN_START_AUTO = "device/device_listen_start_auto.json"

# 设备上行的一帧（16000Hz / 60ms / 单声道 / s16le）
UPLINK_FRAME = b"\xaa" * 1920

# 设备 POST 上来的自述（上游 board.GetSystemInfoJson() 的形状）
DEVICE_INFO = {"board": "bread-compact-wifi-lcd", "version": "2.5.0", "mac": "aa:bb:cc:dd:ee:ff"}

ConnectionClose = object()


def _text(rel: str) -> str:
    return (FIXTURES / rel).read_text(encoding="utf-8")


def _listen(state: str) -> str:
    message = json.loads(_text(LISTEN_START_AUTO))
    message["state"] = state
    return json.dumps(message, ensure_ascii=False)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _fake_codec(params):
    """端到端用假编解码器：网络这一层不需要真 Opus。"""
    return create_opus_codec(params, allow_fake=True)


class FakeSocket:
    """`WebsocketTransport` 那一端的最小假货。"""

    def __init__(self, incoming=(), *, close_raises: bool = False) -> None:
        self._incoming = list(incoming)
        self.sent: list[object] = []
        self.closed = False
        self._close_raises = close_raises

    async def recv(self):
        if not self._incoming:
            raise AssertionError("假 socket 没料了 —— 用例给少了帧")
        item = self._incoming.pop(0)
        if item is ConnectionClose:
            import websockets

            raise websockets.exceptions.ConnectionClosedOK(None, None)
        return item

    async def send(self, frame) -> None:
        self.sent.append(frame)

    async def close(self) -> None:
        if self._close_raises:
            raise RuntimeError("关不掉")
        self.closed = True


class ReplayTransportTest(unittest.TestCase):
    """先读掉一帧再塞回去 —— 这一层错了，症状是"发完 hello 就没下文"。"""

    def test_first_frame_comes_back_first(self):
        async def scenario():
            inner = WebsocketTransport(FakeSocket(["second"]))
            transport = ReplayTransport(inner, "first")
            return await transport.recv(), await transport.recv()

        first, second = asyncio.run(scenario())
        self.assertEqual(first, "first")
        self.assertEqual(second, "second")

    def test_without_first_frame_it_delegates(self):
        async def scenario():
            transport = ReplayTransport(WebsocketTransport(FakeSocket(["only"])), None)
            return await transport.recv()

        self.assertEqual(asyncio.run(scenario()), "only")

    def test_peer_close_is_replayed_as_none(self):
        async def scenario():
            inner = WebsocketTransport(FakeSocket([ConnectionClose]))
            transport = ReplayTransport(inner, None)
            return await transport.recv()

        self.assertIsNone(asyncio.run(scenario()))

    def test_send_and_close_go_through(self):
        async def scenario():
            socket = FakeSocket([])
            transport = ReplayTransport(WebsocketTransport(socket), "first")
            await transport.send("x")
            await transport.close()
            return socket.sent, socket.closed

        sent, closed = asyncio.run(scenario())
        self.assertEqual(sent, ["x"])
        self.assertTrue(closed)


class WebsocketTransportTest(unittest.TestCase):
    def test_bytes_come_back_as_bytes(self):
        async def scenario():
            transport = WebsocketTransport(FakeSocket([b"\x01\x02", "文本"]))
            return await transport.recv(), await transport.recv()

        binary, text = asyncio.run(scenario())
        self.assertEqual(binary, b"\x01\x02")
        self.assertEqual(text, "文本")

    def test_peer_close_returns_none_not_raise(self):
        """契约：对端断开返回 None。抛出去的话上层要到处 try。"""

        async def scenario():
            transport = WebsocketTransport(FakeSocket([ConnectionClose]))
            return await transport.recv()

        self.assertIsNone(asyncio.run(scenario()))

    def test_close_is_idempotent(self):
        async def scenario():
            fake = FakeSocket([])
            transport = WebsocketTransport(fake)
            await transport.close()
            await transport.close()
            return fake.closed

        self.assertTrue(asyncio.run(scenario()))

    def test_close_failure_is_swallowed(self):
        """关连接失败不该盖掉真正的原因（超时 / 协议错）。"""

        async def scenario():
            transport = WebsocketTransport(FakeSocket([], close_raises=True))
            await transport.close()

        asyncio.run(scenario())


class HeaderNormalizationTest(unittest.TestCase):
    """标准库 `http.server` 的 `self.headers` 是 `email.message.Message` ——
    长得像 Mapping、但不是 `Mapping`。2026-09-22 被它咬过一次（OTA 回 400），这里钉住。"""

    def test_email_message_headers_are_understood(self):
        from email.message import Message

        message = Message()
        message["Host"] = "127.0.0.1:8766"
        message["Device-Id"] = "aa:bb:cc:dd:ee:ff"
        headers = ota.normalize_headers(message)
        self.assertEqual(headers["host"], "127.0.0.1:8766")
        self.assertEqual(headers["device-id"], "aa:bb:cc:dd:ee:ff")

    def test_dict_and_line_list_still_work(self):
        self.assertEqual(ota.normalize_headers({"Host": "h"})["host"], "h")
        self.assertEqual(ota.normalize_headers(["Host: h", "Device-Id: d"])["device-id"], "d")

    def test_junk_never_raises(self):
        self.assertEqual(ota.normalize_headers(object()), {})
        self.assertEqual(ota.normalize_headers("   "), {})
        self.assertEqual(ota.normalize_headers(None), {})


class EndToEndTest(unittest.TestCase):
    """真端口、真客户端。板子到货那天走的每一步，这里都先走一遍。"""

    def setUp(self):
        self.http_port = _free_port()
        self.ws_port = _free_port()
        self.server = XiaozhiServer(
            XiaozhiConfig(
                host="127.0.0.1",
                port=self.http_port,
                websocket_port=self.ws_port,
                token="e2e",
            ),
            codec_factory=_fake_codec,
            responder_factory=lambda: FixedResponder(frames=2),
            hello_timeout=5.0,
        )
        self.assertTrue(self.server.start(), "服务没起来 —— 端口被占？")
        self.addCleanup(self.server.stop)

    # ------------------------------------------------------------ OTA（HTTP）

    def test_ota_accepts_post_with_a_body(self):
        """**这条是这次最值钱的一个用例**：上游发的是 POST + body，
        而 websockets 的 HTTP 层会直接掐掉这种请求（ValueError: unsupported request body）。
        所以 OTA 必须走标准库那个 HTTP 口 —— 这条挂了就说明又退回一个口去了。"""
        status, body = self._ota_request(json.dumps(DEVICE_INFO).encode("utf-8"))
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(
            payload["websocket"]["url"], f"ws://127.0.0.1:{self.ws_port}/xiaozhi/v1/"
        )
        self.assertEqual(payload["websocket"]["token"], "e2e")
        self.assertEqual(payload["websocket"]["version"], 1)
        # 上游源码钉死的两条：必须有 websocket 段，绝不能有 activation 段
        self.assertNotIn("activation", payload)
        self.assertEqual(self.server.ota_requests, 1)

    def test_ota_answers_get_too(self):
        """上游在 GetSystemInfoJson() 为空时降级成 GET —— 两种都得答。"""
        status, body = self._ota_request(None, method="GET")
        self.assertEqual(status, 200)
        self.assertIn("websocket", json.loads(body))

    def test_unknown_path_on_the_ota_port_is_404(self):
        status, _ = self._ota_request(b"", path="/nope")
        self.assertEqual(status, 404)

    def test_websocket_port_answers_plain_http_with_a_hint(self):
        """有人拿浏览器点开 WebSocket 口时，给他一句人话，别让他干等。"""
        status, body = self._ota_request(None, method="GET", port=self.ws_port, path="/")
        self.assertEqual(status, 404)
        self.assertIn("OTA", body)

    # ------------------------------------------------------------ WebSocket

    def test_full_round_trip_over_a_real_socket(self):
        asyncio.run(self._round_trip())

    def test_bad_first_frame_gets_the_connection_closed(self):
        """连上来第一句不是 hello：关掉，别耗到超时（日志里要看得见原因）。"""

        async def scenario():
            import websockets

            async with websockets.connect(f"ws://127.0.0.1:{self.ws_port}/xiaozhi/v1/") as ws:
                await ws.send('{"type":"not-hello"}')
                try:
                    await asyncio.wait_for(ws.recv(), 3)
                    return "还连着"
                except websockets.exceptions.ConnectionClosed:
                    return "关了"

        self.assertEqual(asyncio.run(scenario()), "关了")

    # ------------------------------------------------------------ 干活

    def _ota_request(
        self,
        data: bytes | None,
        *,
        method: str = "POST",
        path: str = "/xiaozhi/ota/",
        port: int | None = None,
    ) -> tuple[int, str]:
        request = urllib.request.Request(
            f"http://127.0.0.1:{port or self.http_port}{path}",
            data=data,
            method=method,
            headers={
                "Device-Id": "aa:bb:cc:dd:ee:ff",
                "Client-Id": "11111111-2222-3333-4444-555555555555",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8")

    async def _round_trip(self) -> None:
        import websockets

        url = f"ws://127.0.0.1:{self.ws_port}/xiaozhi/v1/"
        async with websockets.connect(url) as socket:
            await socket.send(_text(DEVICE_HELLO))
            hello = json.loads(await asyncio.wait_for(socket.recv(), 5))
            self.assertEqual(hello["type"], "hello")
            self.assertEqual(hello["transport"], "websocket")
            self.assertTrue(hello["session_id"])

            await socket.send(_listen("start"))
            await socket.send(UPLINK_FRAME)
            await socket.send(_listen("stop"))

            audio = await self._collect_audio(socket, expected=2)
            self.assertEqual(len(audio), 2)
            # 假编解码器是原样透传的：下发的字节数 = 下行一帧的字节数（24000/60ms = 2880）
            self.assertEqual(len(audio[0]), 2880)

    async def _collect_audio(self, socket, *, expected: int) -> list[bytes]:
        """收到 expected 个二进制帧为止；中间夹着的文本消息（状态通知）跳过。"""
        binary: list[bytes] = []
        while len(binary) < expected:
            try:
                message = await asyncio.wait_for(socket.recv(), 5)
            except TimeoutError:
                self.fail(f"只收到 {len(binary)} 个音频帧就没了 —— 服务端没下发？")
            if isinstance(message, (bytes, bytearray)):
                binary.append(bytes(message))
        return binary


if __name__ == "__main__":
    unittest.main()
