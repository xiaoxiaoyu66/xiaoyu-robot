"""身体接口层（S7 准备）：屏幕 / 舵机 / 麦克风 / 喇叭 各留一个抽象。

为什么硬件还没买就先做这个：行为代码（对话、表情、视线）不该 import 具体驱动。
真驱动买回来那天，**换身体 = 加一个 backend 文件**，而不是回头改对话逻辑。
学的是 HANDOFF §8 里 RON 那个项目最值钱的一点 —— 它对硬件也留了这层缝。

现在只有舵机这一块落了地（`ServoBackend` + 假舵机 + gaze -> 转头角度），
因为它是唯一"不花钱就能把数学钉死、单测能跑"的部分。
屏幕 / 麦克风 / 喇叭的抽象等真硬件定下来再抽 —— 现在抽是凭空猜接口，不如不抽。

配套：`XIAOYU_BODY_ENABLED=1` 才会接上，默认关；开着也只是假舵机记日志。
"""

from .head import HeadLimits, HeadLook, gaze_to_angles
from .servo import FakeServo, ServoBackend

__all__ = [
    "FakeServo",
    "HeadLimits",
    "HeadLook",
    "ServoBackend",
    "gaze_to_angles",
]