"""B 阶段：把脸用 HTTP 发出去，平板直接开 http://<主机IP>:8765/。

为什么要测：
    平板那边以前靠临时 `python -m http.server 8080` 打开这张脸（重启就没了，
    还得手输 IP）。改成主控自己发之后，静态文件解析 + token 注入就是"平板能不能
    打开"的唯一路径；顺手把目录穿越挡在这里 —— 跑出去就等于把 .env 和记忆库
    发到局域网上。

全部离线，不启真服务、不占端口。
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from xiaoyu.config import FaceConfig
from xiaoyu.face import protocol, static
from xiaoyu.face.server import FaceServer

ROOT = Path(__file__).resolve().parent.parent

try:
    import websockets  # noqa: F401
    HAS_WEBSOCKETS = True
except ImportError:                      # pragma: no cover - 没装就是可选件缺失
    HAS_WEBSOCKETS = False


class StaticResolveTest(unittest.TestCase):
    """静态文件解析：纯函数，边界值必须稳。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.root = base / "web"
        self.root.mkdir()
        (self.root / "index.html").write_text("<html>脸</html>", encoding="utf-8")
        (self.root / "app.js").write_text("console.log(1)", encoding="utf-8")
        (base / "secret.env").write_text("API_KEY=1", encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def test_root_serves_index(self):
        found = static.resolve(self.root, "/")
        self.assertIsNotNone(found)
        content_type, body = found
        self.assertTrue(content_type.startswith("text/html"))
        self.assertIn("脸", body.decode("utf-8"))

    def test_query_string_is_ignored(self):
        self.assertIsNotNone(static.resolve(self.root, "/index.html?token=xiaoyu"))

    def test_javascript_content_type(self):
        kind, _body = static.resolve(self.root, "/app.js")
        self.assertTrue(kind.startswith("text/javascript"))

    def test_missing_file_is_none(self):
        self.assertIsNone(static.resolve(self.root, "/nope.js"))

    def test_directory_is_not_a_file(self):
        (self.root / "sub").mkdir()
        self.assertIsNone(static.resolve(self.root, "/sub"))

    def test_traversal_is_refused(self):
        for path in ("/../secret.env", "/%2e%2e/secret.env", "/sub/../../secret.env"):
            self.assertIsNone(static.resolve(self.root, path), path)

    def test_token_injection(self):
        html = b'<head><meta name="xiaoyu-token" content=""></head>'
        self.assertIn(b'content="s3cret"', static.with_token(html, "s3cret"))

    def test_token_injection_without_marker_is_harmless(self):
        """页面改版把 meta 删了也不能把脸搞挂 —— 还有 ?token= 兜底。"""
        html = b"<html>no marker</html>"
        self.assertEqual(static.with_token(html, "s3cret"), html)

    def test_empty_token_does_not_inject(self):
        html = b'<meta name="xiaoyu-token" content="">'
        self.assertEqual(static.with_token(html, ""), html)


class RealPageTest(unittest.TestCase):
    """真页面里那个空 meta 不能删 —— 删了 token 注入会静默失效，平板又要手输。"""

    def test_index_has_the_token_marker(self):
        html = (ROOT / "face" / "index.html").read_text(encoding="utf-8")
        self.assertIn(static.TOKEN_MARKER, html)


class TickMessageTest(unittest.TestCase):
    def test_tick_shape(self):
        self.assertEqual(protocol.tick_message(), {"type": "tick"})


class FakeFace:
    """够 _handle / _broadcast 用就行：一个路径，能收能发。"""

    def __init__(self, path="/?token=t", incoming=()):
        self.request = SimpleNamespace(path=path)
        self.sent = []
        self.closed = None
        self._incoming = list(incoming)

    async def send(self, message):
        self.sent.append(message)

    async def close(self, code=1000, reason=""):
        self.closed = (code, reason)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._incoming:
            raise StopAsyncIteration
        return self._incoming.pop(0)


class FaceServerTestBase(unittest.TestCase):
    def setUp(self):
        self.server = FaceServer(FaceConfig(host="127.0.0.1", port=8766, token="t"))

    def _quiet(self, coro):
        """服务器每次连接都记日志 —— 测试里别刷屏。"""
        from loguru import logger as loguru_logger

        loguru_logger.disable("xiaoyu.face.server")
        try:
            return asyncio.run(coro)
        finally:
            loguru_logger.enable("xiaoyu.face.server")


class WelcomeOnConnectTest(FaceServerTestBase):
    """刚连上要立刻看到当前状态（平板挂起后重连就是这个场景）。"""

    def test_new_face_gets_the_current_state(self):
        self.server.publish_state("speaking")
        face = FakeFace()
        self._quiet(self.server._send_current_state(face))
        self.assertEqual(json.loads(face.sent[0])["state"], "speaking")

    def test_welcome_sends_the_latest_state_not_the_first(self):
        self.server.publish_state("listening")
        self.server.publish_state("idle")
        face = FakeFace()
        self._quiet(self.server._send_current_state(face))
        self.assertEqual(json.loads(face.sent[0])["state"], "idle")

    def test_nothing_to_send_when_no_state_yet(self):
        face = FakeFace()
        self._quiet(self.server._send_current_state(face))
        self.assertEqual(face.sent, [])


class HandleConnectionTest(FaceServerTestBase):
    """走一遍 _handle：token 不对要被拒，token 对要先收到当前状态。"""

    def test_bad_token_is_refused(self):
        face = FakeFace(path="/?token=wrong")
        self._quiet(self.server._handle(face))
        self.assertEqual(face.closed[0], 4401)
        self.assertEqual(face.sent, [])

    def test_good_token_gets_state_then_cleans_up(self):
        self.server.publish_state("idle")
        face = FakeFace(path="/?token=t")
        self._quiet(self.server._handle(face))
        self.assertEqual(json.loads(face.sent[0])["state"], "idle")
        self.assertEqual(self.server._clients, set())


class HeartbeatTest(FaceServerTestBase):
    """闲置时要发心跳，页面才分得清"安静"和"断线"。"""

    def test_ticks_connected_faces(self):
        face = FakeFace()
        self.server._clients.add(face)

        async def collect_one():
            task = asyncio.create_task(self.server._heartbeat())
            try:
                for _ in range(200):
                    if face.sent:
                        return
                    await asyncio.sleep(0.005)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        with mock.patch("xiaoyu.face.server.HEARTBEAT_SECONDS", 0.01):
            asyncio.run(collect_one())

        self.assertTrue(face.sent, "一直没等到心跳")
        self.assertEqual(json.loads(face.sent[0])["type"], "tick")


@unittest.skipUnless(HAS_WEBSOCKETS, "没装 websockets，跳过 HTTP 那一层")
class HttpServingTest(unittest.TestCase):
    """网页和 WebSocket 共用一个端口 —— 平板开 http://<主机IP>:8765/ 就是脸。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        (root / "index.html").write_text(
            '<html><meta name="xiaoyu-token" content=""></html>', encoding="utf-8"
        )
        self.server = FaceServer(
            FaceConfig(host="127.0.0.1", port=8766, token="t"), web_root=root
        )

    def tearDown(self):
        self._tmp.cleanup()

    @staticmethod
    def _request(path, upgrade=None):
        headers = {"Upgrade": upgrade} if upgrade else {}
        return SimpleNamespace(path=path, headers=headers)

    def test_index_is_served_with_the_real_token(self):
        response = self.server._process_request(None, self._request("/"))
        self.assertEqual(response.status_code, 200)
        self.assertIn('content="t"', response.body.decode("utf-8"))

    def test_websocket_handshake_is_left_alone(self):
        request = self._request("/?token=t", "WebSocket")
        self.assertIsNone(self.server._process_request(None, request))

    def test_missing_file_is_404(self):
        response = self.server._process_request(None, self._request("/nope.js"))
        self.assertEqual(response.status_code, 404)

    def test_traversal_is_404(self):
        response = self.server._process_request(None, self._request("/../.env"))
        self.assertEqual(response.status_code, 404)

    def test_without_web_root_it_still_answers(self):
        """没给 web_root（比如单测里直接构造）也不能抛异常把连接带崩。"""
        server = FaceServer(FaceConfig(host="127.0.0.1", port=8766, token="t"))
        response = server._process_request(None, self._request("/"))
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()