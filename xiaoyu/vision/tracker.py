"""眼睛跟随的执行层（S6a）：摄像头取流 + BlazeFace 检测 + gaze 广播。

节奏（和 TODO 设计骨架一致）：

- 守护线程常驻，但**只在 active 时取帧** —— 说话时眼睛忙、不抢 CPU，
  摄像头也没必要一直亮着（隐私 + 功耗双赢）。
- 检测不到人脸就闭嘴不发 —— 豆眼那侧有 500ms 超时，会自动回待机游荡。
- 发布节流：默认 10Hz，足够顺滑又不刷屏。

降级原则：视觉是可选件。opencv / mediapipe 没装、摄像头被占用，
一律 logger.warning 后静默禁用 —— 声音一行不受影响。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from ..config import Paths, VisionConfig
from ..logger import get_logger
from .gaze import face_to_gaze, smooth_gaze

logger = get_logger(__name__)


class GazeTracker:
    """人脸位置 -> on_gaze(x, y) 回调，x/y ∈ [-1, 1]²，已 EMA 平滑。"""

    def __init__(self, config: VisionConfig) -> None:
        self._config = config
        self._active = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # 上一帧的平滑结果；None = 还没见过人
        self._smoothed: tuple[float, float] | None = None
        # 上次发布的时间戳（节流用）
        self._last_publish = 0.0
        self.on_gaze: Callable[[float, float], None] | None = None

    # ---------------- 生命周期 ----------------

    def start(self) -> bool:
        """起守护线程。依赖没装 / 模型缺失 / 摄像头打不开都返回 False（静默禁用）。"""
        try:
            import cv2  # noqa: F401
            import mediapipe as mp  # noqa: F401
        except Exception:
            logger.warning("视觉依赖没装齐（opencv-python / mediapipe），眼睛跟随本次不启用")
            return False

        model_path = Paths().vision / self._config.model_file
        if not model_path.exists():
            logger.warning(
                "眼睛跟随模型缺失：{}（models/vision/ 目录，约 225KB），本次不启用",
                model_path.name,
            )
            return False
        self._model_path = model_path

        self._thread = threading.Thread(
            target=self._run, name="xiaoyu-vision", daemon=True
        )
        self._thread.start()
        logger.info(
            "眼睛跟随已启动（摄像头 #{}，{}Hz，只在待机/听话时跑）",
            self._config.camera_index,
            self._config.publish_hz,
        )
        return True

    def stop(self) -> None:
        self._stop.set()

    def set_active(self, active: bool) -> None:
        """主控按状态机开关它：只有待机 / 听话时才看。切换时清平滑，
        避免上次的老位置让眼睛突然跳一下。"""
        was = self._active.is_set()
        if active:
            self._active.set()
        else:
            self._active.clear()
            self._smoothed = None
        if was != active:
            logger.debug("眼睛跟随 {}", "开" if active else "关")

    # ---------------- 工作线程 ----------------

    def _run(self) -> None:
        try:
            self._loop()
        except Exception:
            logger.exception("眼睛跟随线程意外退出，本次运行不再看")

    def _loop(self) -> None:
        import cv2
        import mediapipe as mp
        from mediapipe.tasks.python.core.base_options import BaseOptions
        from mediapipe.tasks.python.vision import (
            FaceDetector,
            FaceDetectorOptions,
            RunningMode,
        )

        cap = cv2.VideoCapture(self._config.camera_index, cv2.CAP_DSHOW)
        if not cap.isOpened():
            logger.warning("摄像头 #{} 打不开，眼睛跟随本次不启用", self._config.camera_index)
            return

        # mediapipe 1.0 起只有 Tasks API（旧的 mp.solutions 已删除）。
        # BlazeFace 短距模型：6 个关键点，第 3 个（index 2）是鼻尖。
        detector = FaceDetector.create_from_options(
            FaceDetectorOptions(
                base_options=BaseOptions(model_asset_path=str(self._model_path)),
                running_mode=RunningMode.IMAGE,
                min_detection_confidence=self._config.min_confidence,
            )
        )
        interval = 1.0 / max(1, self._config.publish_hz)
        logger.info("摄像头已打开，开始等人出现")

        try:
            while not self._stop.is_set():
                if not self._active.is_set():
                    time.sleep(0.05)
                    continue
                ok, frame = cap.read()
                if not ok or frame is None:
                    time.sleep(0.05)
                    continue

                h, w = frame.shape[:2]
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                result = detector.detect(
                    mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
                )

                now = time.monotonic()
                if result.detections:
                    det = result.detections[0]  # 只跟最近/最自信的一张脸
                    nose = det.keypoints[2]     # 鼻尖（相对坐标 0~1）
                    raw = face_to_gaze(nose.x * w, nose.y * h, w, h)
                    if self._smoothed is None:
                        self._smoothed = raw
                    else:
                        self._smoothed = smooth_gaze(
                            self._smoothed, raw, self._config.smooth_alpha
                        )
                    if now - self._last_publish >= interval:
                        self._last_publish = now
                        self._emit(self._smoothed[0], self._smoothed[1])
                else:
                    self._smoothed = None  # 人不见了：停发，脸那边超时回游荡
        finally:
            detector.close()
            cap.release()

    def _emit(self, x: float, y: float) -> None:
        if self.on_gaze is None:
            return
        try:
            self.on_gaze(x, y)
        except Exception:
            logger.exception("gaze 回调执行失败")
