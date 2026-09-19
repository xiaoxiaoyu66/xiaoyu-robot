"""身体接口层（S7 准备）的测试：gaze -> 转头角度的数学 + 接线。

跑法：python -m unittest discover tests
不碰硬件、不开摄像头 —— 这里全程只有假舵机。
"""

from __future__ import annotations

import dataclasses
import os
import unittest
from types import SimpleNamespace
from unittest import mock

from xiaoyu import app
from xiaoyu.body import FakeServo, HeadLimits, HeadLook, ServoBackend, gaze_to_angles
from xiaoyu.config import BodyConfig, Settings
from xiaoyu.state import StateMachine


def _settings(**body) -> Settings:
    return Settings(body=dataclasses.replace(BodyConfig(), **body))


class GazeToAnglesTest(unittest.TestCase):
    def test_center_is_zero(self):
        self.assertEqual(gaze_to_angles(0.0, 0.0), (0.0, 0.0))

    def test_direction_lock_person_on_right_turns_head_right(self):
        """方向锁：gx > 0（人在这张脸的右边）-> pan > 0（头也往那一侧转）。

        必须和 face/protocol.py 的 gaze 语义一致。要反方向就开 invert_pan，
        **别改这个符号** —— 相机没镜像那次的教训写在 xiaoyu/vision/gaze.py 里。
        """
        pan, tilt = gaze_to_angles(1.0, 0.0)
        self.assertGreater(pan, 0)
        self.assertEqual(tilt, 0.0)

    def test_person_above_looks_up(self):
        pan, tilt = gaze_to_angles(0.0, 1.0)
        self.assertEqual(pan, 0.0)
        self.assertGreater(tilt, 0)

    def test_full_deflection_uses_configured_range(self):
        limits = HeadLimits(pan_range=40.0, tilt_range=20.0)
        self.assertEqual(gaze_to_angles(1.0, -1.0, limits), (40.0, -20.0))

    def test_small_gaze_falls_into_deadzone(self):
        """人脸检测本身在抖，正中间必须是一小块"平地" ——
        没死区的话头会一直微微晃，看着像坏了。"""
        limits = HeadLimits(deadzone=0.1)
        self.assertEqual(gaze_to_angles(0.05, -0.09, limits), (0.0, 0.0))
        self.assertNotEqual(gaze_to_angles(0.2, 0.0, limits)[0], 0.0)

    def test_out_of_range_input_is_clamped(self):
        # 调用方直接喂像素坐标，也不能把舵机顶到天上去
        self.assertEqual(gaze_to_angles(999.0, -999.0), (30.0, -15.0))


class FakeServoTest(unittest.TestCase):
    def test_satisfies_backend_protocol(self):
        self.assertIsInstance(FakeServo("pan"), ServoBackend)

    def test_records_history_and_close(self):
        servo = FakeServo("pan")
        servo.set_angle(10)
        servo.set_angle(-5.5)
        self.assertEqual(servo.history, [10.0, -5.5])
        self.assertEqual(servo.angle, -5.5)
        servo.close()
        self.assertTrue(servo.closed)


class HeadLookTest(unittest.TestCase):
    def _head(self, **kwargs):
        limits = kwargs.pop("limits", HeadLimits())
        return HeadLook(FakeServo("pan"), FakeServo("tilt"), limits=limits, **kwargs)

    def test_look_drives_both_servos(self):
        head = self._head(limits=HeadLimits(pan_range=30.0, tilt_range=10.0))
        self.assertEqual(head.look(0.5, -0.5), (15.0, -5.0))
        self.assertEqual(head.pan_servo.history, [15.0])
        self.assertEqual(head.tilt_servo.history, [-5.0])

    def test_invert_flips_only_that_axis(self):
        head = self._head(invert_pan=True)
        pan, tilt = head.look(1.0, 1.0)
        self.assertLess(pan, 0)      # 装反了：配置里反一次就行
        self.assertGreater(tilt, 0)  # 另一根不受影响

    def test_center_goes_back_to_zero(self):
        head = self._head()
        head.look(1.0, 1.0)
        self.assertEqual(head.center(), (0.0, 0.0))
        self.assertEqual(head.pan_servo.angle, 0.0)

    def test_dead_servo_does_not_stop_the_other(self):
        """一根舵机掉线，另一根照样动 —— 表演类失败不能带崩动作。"""
        head = self._head()
        head.pan_servo.set_angle = mock.Mock(side_effect=RuntimeError("舵机掉线"))
        pan, tilt = head.look(1.0, 1.0)
        self.assertIsInstance(pan, float)  # 角度照算照返回
        self.assertEqual(head.tilt_servo.history, [15.0])

    def test_close_survives_a_broken_servo(self):
        head = self._head()
        head.tilt_servo.close = mock.Mock(side_effect=RuntimeError("线被拔了"))
        head.close()
        self.assertTrue(head.pan_servo.closed)


class AttachBodyTest(unittest.TestCase):
    def test_default_off(self):
        self.assertIsNone(app.attach_body(Settings()))

    def test_enabled_uses_fake_servos(self):
        head = app.attach_body(_settings(enabled=True))
        self.assertIsInstance(head, HeadLook)
        self.assertIsInstance(head.pan_servo, FakeServo)

    def test_config_reads_env(self):
        env = {
            "XIAOYU_BODY_ENABLED": "1",
            "XIAOYU_BODY_PAN_RANGE": "45",
            "XIAOYU_BODY_INVERT_PAN": "yes",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            settings = Settings.load()
        self.assertTrue(settings.body.enabled)
        self.assertEqual(settings.body.pan_range, 45.0)
        self.assertTrue(settings.body.invert_pan)

    def test_config_defaults_are_off_and_small(self):
        body = BodyConfig()
        self.assertFalse(body.enabled)
        self.assertLessEqual(body.tilt_range, body.pan_range)  # 抬头不该比转头还夸张


class VisionWiringTest(unittest.TestCase):
    """锁「同一份 gaze 要同时到脸和身体」—— 只接一边就是"眼睛动了头没动"。"""

    class _NoopTracker:
        def __init__(self, config):
            self.on_gaze = None

        def start(self):
            return True

        def set_active(self, active):
            pass

    def _attach(self, head):
        face = SimpleNamespace(publish_gaze=mock.Mock())
        settings = _settings(enabled=True)
        with mock.patch("xiaoyu.vision.tracker.GazeTracker", self._NoopTracker):
            with mock.patch.object(app, "attach_body", return_value=head):
                tracker = app.attach_vision(settings, StateMachine(), face)
        return tracker, face

    def test_gaze_reaches_face_and_head(self):
        head = SimpleNamespace(look=mock.Mock())
        tracker, face = self._attach(head)
        tracker.on_gaze(0.5, -0.25)
        face.publish_gaze.assert_called_once_with(0.5, -0.25)
        head.look.assert_called_once_with(0.5, -0.25)

    def test_broken_body_does_not_break_the_face(self):
        """身体炸了，脸必须照收 —— "情绪回调失败"那次的同类错误，别再犯。"""
        head = SimpleNamespace(look=mock.Mock(side_effect=RuntimeError("舵机炸了")))
        tracker, face = self._attach(head)
        tracker.on_gaze(0.5, 0.0)
        face.publish_gaze.assert_called_once_with(0.5, 0.0)