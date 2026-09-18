"""S5 表情脸：协议纯逻辑 + 口型计算 + 打断事件的测试（全部不联网）。"""

from __future__ import annotations

import json
import unittest

import numpy as np

from xiaoyu.audio.player import mouth_level
from xiaoyu.face import protocol
from xiaoyu.face.server import FaceServer
from xiaoyu.config import FaceConfig


class MouthLevelTest(unittest.TestCase):
    """口型开度：纯函数，边界值必须稳。"""

    def test_silence_is_zero(self):
        self.assertEqual(mouth_level(np.zeros(800, dtype=np.float32)), 0.0)

    def test_empty_is_zero(self):
        self.assertEqual(mouth_level(np.array([], dtype=np.float32)), 0.0)

    def test_louder_is_more_open(self):
        quiet = mouth_level(np.full(800, 0.05, dtype=np.float32))
        loud = mouth_level(np.full(800, 0.30, dtype=np.float32))
        self.assertGreater(loud, quiet)

    def test_result_clamped_to_unit(self):
        level = mouth_level(np.full(800, 0.99, dtype=np.float32))
        self.assertGreaterEqual(level, 0.0)
        self.assertLessEqual(level, 1.0)

    def test_nan_input_is_zero(self):
        bad = np.array([np.nan, 0.1], dtype=np.float32)
        self.assertEqual(mouth_level(bad), 0.0)


class ProtocolTest(unittest.TestCase):
    def test_state_message(self):
        self.assertEqual(
            protocol.state_message("speaking"), {"type": "state", "state": "speaking"}
        )

    def test_state_message_rejects_unknown(self):
        with self.assertRaises(ValueError):
            protocol.state_message("sleeping")

    def test_mouth_message_clamps(self):
        self.assertEqual(protocol.mouth_message(1.7), {"type": "mouth", "level": 1.0})
        self.assertEqual(protocol.mouth_message(-0.2), {"type": "mouth", "level": 0.0})

    def test_caption_strips(self):
        self.assertEqual(
            protocol.caption_message("  你好  "), {"type": "caption", "text": "你好"}
        )

    def test_encode_keeps_chinese(self):
        raw = protocol.encode(protocol.caption_message("你好"))
        self.assertEqual(json.loads(raw)["text"], "你好")

    def test_parse_command_accepts_interrupt(self):
        self.assertEqual(protocol.parse_command('{"type":"interrupt"}'), {"type": "interrupt"})

    def test_parse_command_rejects_garbage(self):
        self.assertIsNone(protocol.parse_command("not json"))
        self.assertIsNone(protocol.parse_command('{"type":"hack"}'))
        self.assertIsNone(protocol.parse_command(b"\xff\xfe"))

    def test_token_ok(self):
        self.assertTrue(protocol.token_ok("/?token=xiaoyu", "xiaoyu"))
        self.assertFalse(protocol.token_ok("/?token=wrong", "xiaoyu"))
        self.assertFalse(protocol.token_ok("/", "xiaoyu"))
        self.assertTrue(protocol.token_ok("/anything", ""))  # 空 token = 不校验


class FaceServerBasicsTest(unittest.TestCase):
    """不启动网络服务，只验证打断事件的语义。"""

    def _make(self) -> FaceServer:
        return FaceServer(FaceConfig(enabled=True, port=8765, token="xiaoyu"))

    def test_interrupt_flag_roundtrip(self):
        face = self._make()
        self.assertFalse(face.interrupted())
        face._interrupt.set()          # 模拟 _handle 收到 interrupt 命令
        self.assertTrue(face.interrupted())
        face.clear_interrupt()
        self.assertFalse(face.interrupted())

    def test_publish_without_clients_is_noop(self):
        face = self._make()            # 没 start()，没 loop 没客户端
        face.publish_state("idle")     # 不得抛异常
        face.publish_mouth(0.5)
        face.publish_caption("你好")

    def test_on_interrupt_hook_runs(self):
        face = self._make()
        called = []
        face.on_interrupt = lambda: called.append(1)
        # 直接模拟收到命令后的分发逻辑
        face._interrupt.set()
        if face.on_interrupt is not None:
            face.on_interrupt()
        self.assertEqual(called, [1])


if __name__ == "__main__":
    unittest.main()
