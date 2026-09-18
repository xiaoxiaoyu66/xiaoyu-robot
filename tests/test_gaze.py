"""S6a 眼睛跟随的纯函数测试：gaze 计算 + EMA 平滑 + 协议消息。

跑法：python -m pytest tests/test_gaze.py -v
不需要摄像头、不需要 mediapipe —— 这里测的全是纯逻辑。
"""

from xiaoyu.face import protocol
from xiaoyu.vision.gaze import face_to_gaze, smooth_gaze


def test_center_face_looks_straight():
    assert face_to_gaze(320, 240, 640, 480) == (0.0, 0.0)


def test_face_on_left_looks_left():
    x, y = face_to_gaze(160, 240, 640, 480)
    assert x == -0.5
    assert y == 0.0


def test_face_above_looks_up():
    # 鼻尖在画面上方（y 像素小）-> gaze.y 为正
    x, y = face_to_gaze(320, 120, 640, 480)
    assert x == 0.0
    assert y == 0.5


def test_gaze_clamped_to_unit_square():
    # 鼻尖在画面左下 -> (-1, -1)；右上 -> (1, 1)
    assert face_to_gaze(-100, 9999, 640, 480) == (-1.0, -1.0)
    assert face_to_gaze(9999, -100, 640, 480) == (1.0, 1.0)


def test_zero_frame_returns_origin():
    assert face_to_gaze(10, 10, 0, 0) == (0.0, 0.0)


def test_smooth_gaze_converges():
    prev = (0.0, 0.0)
    raw = (1.0, -1.0)
    for _ in range(50):
        prev = smooth_gaze(prev, raw, 0.3)
    assert abs(prev[0] - 1.0) < 0.01
    assert abs(prev[1] + 1.0) < 0.01


def test_smooth_gaze_alpha_zero_freezes():
    assert smooth_gaze((0.3, -0.2), (1.0, 1.0), 0.0) == (0.3, -0.2)


def test_smooth_gaze_alpha_clamped():
    x, y = smooth_gaze((0.0, 0.0), (1.0, 1.0), 99.0)
    assert (x, y) == (1.0, 1.0)


def test_gaze_message_roundtrip():
    msg = protocol.gaze_message(-0.4567, 1.5)
    assert msg["type"] == "gaze"
    assert msg["x"] == -0.457   # 取整
    assert msg["y"] == 1.0      # 夹到上限
    import json

    assert json.loads(protocol.encode(msg)) == msg
