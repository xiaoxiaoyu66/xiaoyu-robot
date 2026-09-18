"""gaze 计算（S6a 眼睛跟随）——纯函数，不碰摄像头、不 import 任何重依赖。

输出语义就是收件人 `face/protocol.py` 那句「人往哪，眼往哪」，
也就是**"该往屏幕哪边看"**，不是"鼻尖在画面里的原始位置"：

    摄像头画面宽 W 高 H，人脸鼻尖关键点在 (nx, ny)。
    x = -(nx - W/2) / (W/2)   # 左负右正：+1 = 人在这张屏幕的右边
    y = (H/2 - ny) / (H/2)    # 下负上正：+1 = 人在画面上方（抬头）

两个值都夹在 [-1, 1]。脸那边收到后乘以各自方向上的最大偏移像素。

**x 为什么要反号（2026-09-18 真人实测修的那次，别删）**：
摄像头拍的是**未镜像**画面 —— 你往自己的右边动，在画面里你出现在**左边**，
所以 `(nx - W/2)` 是负的。可脸是**朝着你**画的：你在它的右边，
它该把眼珠转向它自己的左边，而它自己的左边**在你看来**正是屏幕的右边。
两边各反一次、互相抵消，于是这里必须再反一次，输出才是"该往哪看"。

不反的症状就是用户报的那句：「人往右移，眼睛却往左看」。
原来的注释写的"人在画面左边 -> 眼睛往左看"，是把"画面左边"当成了"屏幕左边"，
病根就在这一句。

顺带：**上下不存在镜像**（你抬头，画面里你也偏上），所以 y 不反号。

为什么不直接拿人脸框中心：BlazeFace 的鼻尖关键点比框中心
更接近"视线交汇点"，而且低头时鼻尖下沉比框中心明显，跟随更自然。
"""

from __future__ import annotations


def face_to_gaze(nose_x: float, nose_y: float, frame_w: int, frame_h: int) -> tuple[float, float]:
    """鼻尖像素坐标 -> 归一化 gaze(x, y) ∈ [-1, 1]²，语义是"该往屏幕哪边看"。

    x：+1 = 人在这张屏幕的右边（摄像头未镜像 + 脸朝着人画，两次翻转相抵，
       所以这里要反号 —— 推导见模块 docstring）。
    y：+1 = 人在画面上方（抬头）。
    帧宽高 <= 0 返回 (0, 0)。
    """
    if frame_w <= 0 or frame_h <= 0:
        return 0.0, 0.0
    x = -(nose_x - frame_w / 2) / (frame_w / 2)
    y = (frame_h / 2 - nose_y) / (frame_h / 2)
    return _clamp(x), _clamp(y)


def smooth_gaze(prev: tuple[float, float], raw: tuple[float, float], alpha: float) -> tuple[float, float]:
    """EMA 平滑。alpha 越大越跟手、越小越稳（常用 0.25~0.4）。"""
    alpha = max(0.0, min(1.0, alpha))
    return (
        prev[0] + (raw[0] - prev[0]) * alpha,
        prev[1] + (raw[1] - prev[1]) * alpha,
    )


def _clamp(v: float) -> float:
    return max(-1.0, min(1.0, v))
