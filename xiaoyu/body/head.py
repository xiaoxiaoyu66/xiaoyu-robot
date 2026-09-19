"""gaze -> 转头角度（S7 准备）：把 S6a 的眼睛跟随，变成实体的"头往哪转多少"。

坐标约定和 `xiaoyu/face/protocol.py` 的 gaze 消息**是同一套**（别再立一套）：

    gx > 0  人在这张脸的右边（也就是机器人自己的左边）
    gy > 0  人在画面上方（抬头）

输出 pan / tilt 跟眼睛**同号**：

    pan  > 0  头往"你看到的屏幕右边"转 —— 和 gx 同号
    tilt > 0  抬头 —— 和 gy 同号

**硬件装反了不要改这里的符号**，走 `HeadLook(invert_pan=...)` / 配置。
理由见 `xiaoyu/vision/gaze.py` 记的那次返工：「人往右移，眼睛却往左看」，
根因就是两边各反了一次、最后谁也说不清反了几次。
镜像只能有一个出处 —— 要么是这里的 invert，要么是硬件，不允许散在代码里。

这一层只有数学 + 假舵机：不花钱、不等硬件、单测能跑。
真舵机买回来那天接的是 `ServoBackend` 的另一个实现，这个文件不用动。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..logger import get_logger
from .servo import ServoBackend

logger = get_logger(__name__)


@dataclass(frozen=True)
class HeadLimits:
    """转头幅度和死区。

    pan_range / tilt_range：gaze 满偏（±1）时转多少度。**先小后大** ——
    真舵机瞬间大角度转动像抽搐，而且到头会"哒哒"响（堵转，久了会烧）。
    deadzone：gaze 小于这个值就当作"正中间"。人脸检测本身有抖动，
    没有死区的话头会一直微微晃，看着像坏了。
    """

    pan_range: float = 30.0
    tilt_range: float = 15.0
    deadzone: float = 0.05


def gaze_to_angles(
    gx: float, gy: float, limits: HeadLimits | None = None
) -> tuple[float, float]:
    """归一化 gaze(x, y) -> (pan_deg, tilt_deg)。纯函数，好单测。

    gx / gy 先夹到 [-1, 1]（上游已经夹过，这里再夹一次是防调用方直接喂像素）。
    |g| < deadzone 一律当 0，所以正中间是一小块"平地"而不是一个点。
    """
    limits = limits or HeadLimits()
    x = _clamp(gx)
    y = _clamp(gy)
    if abs(x) < limits.deadzone:
        x = 0.0
    if abs(y) < limits.deadzone:
        y = 0.0
    return (x * limits.pan_range, y * limits.tilt_range)


@dataclass
class HeadLook:
    """把 gaze 送到两个舵机上。pan = 左右转，tilt = 抬头低头。"""

    pan_servo: ServoBackend
    tilt_servo: ServoBackend
    limits: HeadLimits = field(default_factory=HeadLimits)
    invert_pan: bool = False
    invert_tilt: bool = False

    def look(self, gx: float, gy: float) -> tuple[float, float]:
        """收到一帧 gaze 就转过去，返回**实际下发**的 (pan, tilt) 角度。

        这是给 `GazeTracker.on_gaze` 用的 —— 签名叫起来就是 (gx, gy)。
        """
        pan, tilt = gaze_to_angles(gx, gy, self.limits)
        if self.invert_pan:
            pan = -pan
        if self.invert_tilt:
            tilt = -tilt
        self._apply(self.pan_servo, "pan", pan)
        self._apply(self.tilt_servo, "tilt", tilt)
        return (pan, tilt)

    def center(self) -> tuple[float, float]:
        """回正。没人了、要说话了、要收工了都该回正 —— 一直歪着头很瘆人。"""
        return self.look(0.0, 0.0)

    def close(self) -> None:
        """松力。单个舵机断开失败也不能挡着另一个。"""
        self._apply_close(self.pan_servo, "pan")
        self._apply_close(self.tilt_servo, "tilt")

    @staticmethod
    def _apply(servo: ServoBackend, name: str, degrees: float) -> None:
        """一个舵机掉线不能让另一个也不动：各自吞异常，只记日志。

        外层还有 `app._fanout` 和 `GazeTracker` 兜着，这里是第二道 ——
        "情绪回调失败"那次留的教训：表演类的失败绝不能带崩主流程。
        """
        try:
            servo.set_angle(degrees)
        except Exception:
            logger.exception("{} 舵机转动失败（角度 {:.1f}°）", name, degrees)

    @staticmethod
    def _apply_close(servo: ServoBackend, name: str) -> None:
        try:
            servo.close()
        except Exception:
            logger.exception("{} 舵机断开失败", name)


def _clamp(v: float) -> float:
    return max(-1.0, min(1.0, float(v)))