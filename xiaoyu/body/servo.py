"""舵机接口 + 假舵机。

真驱动（买回来之后）要实现的就两个方法：

    set_angle(degrees)   转到 degrees 度
    close()              断电松手

行为代码只认 `ServoBackend` 这个形状，不认识 pyserial、不认识 PWM 引脚号。
所以"换一块板子"不会污染对话逻辑 —— 这是这一层存在的全部理由。

真实现可以是：ESP32 上跑个串口/HTTP 服务、树莓派上的 GPIO PWM、
或者 M5Stack 舵机 Unit。谁来实现都行，接上就能用。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..logger import get_logger

logger = get_logger(__name__)


@runtime_checkable
class ServoBackend(Protocol):
    """舵机的最小形状。真驱动照着实现这两个方法即可。"""

    def set_angle(self, degrees: float) -> None:
        """把舵机转到 degrees 度。超出硬件行程应由实现自己夹住。"""
        ...

    def close(self) -> None:
        """收工：松力/断电（一直通电堵转会把舵机烧热）。"""
        ...


class FakeServo:
    """假舵机：只记日志，不动任何硬件。没有硬件也能把上层跑通、能断言。

    `history` 留完整轨迹（单测断言用），`angle` 是最新角度。
    真舵机上"转一次要几十毫秒"这件事这里没有 —— 所以别拿它测时序。
    """

    def __init__(self, name: str = "servo", initial: float = 0.0) -> None:
        self.name = name
        self.angle = float(initial)
        self.history: list[float] = []
        self.closed = False
        logger.info("假舵机 {} 就位（起始 {:.1f}°）—— 还没有真硬件", name, self.angle)

    def set_angle(self, degrees: float) -> None:
        self.angle = float(degrees)
        self.history.append(self.angle)
        # 视线是 10Hz 的，用 info 会刷屏 —— 这行只在 DEBUG 下看得到
        logger.debug("假舵机 {} -> {:.1f}°", self.name, self.angle)

    def close(self) -> None:
        self.closed = True
        logger.debug("假舵机 {} 已断开", self.name)