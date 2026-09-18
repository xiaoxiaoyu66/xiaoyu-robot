"""gaze 计算（S6a 眼睛跟随）——纯函数，不碰摄像头、不 import 任何重依赖。

坐标约定（和 TODO 里的设计骨架一致）：

    摄像头画面宽 W 高 H，人脸鼻尖关键点在 (nx, ny)。
    x = (nx - W/2) / (W/2)   # 人在画面左边 -> x 为负 -> 眼睛往左看
    y = (H/2 - ny) / (H/2)   # 人在画面上方 -> y 为正 -> 眼睛往上看

两个值都夹在 [-1, 1]。脸那边收到后乘以各自方向上的最大偏移像素。

为什么不直接拿人脸框中心：BlazeFace 的鼻尖关键点比框中心
更接近"视线交汇点"，而且低头时鼻尖下沉比框中心明显，跟随更自然。
"""

from __future__ import annotations


def face_to_gaze(nose_x: float, nose_y: float, frame_w: int, frame_h: int) -> tuple[float, float]:
    """鼻尖像素坐标 -> 归一化 gaze(x, y) ∈ [-1, 1]²。帧宽高 <= 0 返回 (0, 0)。"""
    if frame_w <= 0 or frame_h <= 0:
        return 0.0, 0.0
    x = (nose_x - frame_w / 2) / (frame_w / 2)
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
