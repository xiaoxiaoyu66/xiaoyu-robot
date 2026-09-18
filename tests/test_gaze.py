"""S6a 眼睛跟随的纯函数测试：gaze 计算 + EMA 平滑 + 协议消息。

跑法：python -m unittest discover tests
不需要摄像头、不需要 mediapipe —— 这里测的全是纯逻辑。
"""

from __future__ import annotations

import json
import unittest

from xiaoyu.face import protocol
from xiaoyu.vision.gaze import face_to_gaze, smooth_gaze


class GazeMathTest(unittest.TestCase):
    def test_center_face_looks_straight(self):
        self.assertEqual(face_to_gaze(320, 240, 640, 480), (0.0, 0.0))

    def test_face_on_left_looks_left(self):
        x, y = face_to_gaze(160, 240, 640, 480)
        self.assertEqual(x, -0.5)
        self.assertEqual(y, 0.0)

    def test_face_above_looks_up(self):
        # 鼻尖在画面上方（y 像素小）-> gaze.y 为正
        x, y = face_to_gaze(320, 120, 640, 480)
        self.assertEqual(x, 0.0)
        self.assertEqual(y, 0.5)

    def test_gaze_clamped_to_unit_square(self):
        # 鼻尖在画面左下 -> (-1, -1)；右上 -> (1, 1)
        self.assertEqual(face_to_gaze(-100, 9999, 640, 480), (-1.0, -1.0))
        self.assertEqual(face_to_gaze(9999, -100, 640, 480), (1.0, 1.0))

    def test_zero_frame_returns_origin(self):
        self.assertEqual(face_to_gaze(10, 10, 0, 0), (0.0, 0.0))


class GazeSmoothingTest(unittest.TestCase):
    def test_converges(self):
        prev = (0.0, 0.0)
        for _ in range(50):
            prev = smooth_gaze(prev, (1.0, -1.0), 0.3)
        self.assertAlmostEqual(prev[0], 1.0, delta=0.01)
        self.assertAlmostEqual(prev[1], -1.0, delta=0.01)

    def test_alpha_zero_freezes(self):
        self.assertEqual(smooth_gaze((0.3, -0.2), (1.0, 1.0), 0.0), (0.3, -0.2))

    def test_alpha_clamped(self):
        self.assertEqual(smooth_gaze((0.0, 0.0), (1.0, 1.0), 99.0), (1.0, 1.0))


class GazeProtocolTest(unittest.TestCase):
    def test_message_roundtrip(self):
        msg = protocol.gaze_message(-0.4567, 1.5)
        self.assertEqual(msg["type"], "gaze")
        self.assertEqual(msg["x"], -0.457)   # 取整
        self.assertEqual(msg["y"], 1.0)      # 夹到上限
        self.assertEqual(json.loads(protocol.encode(msg)), msg)
