"""表情脸的 WebSocket 服务（S5）。

设计要点：

- 跑在独立守护线程里，自带一个 asyncio 事件循环 —— 主控的串行循环
  是阻塞式的，不为一张脸重写主循环。
- 广播用 run_coroutine_threadsafe 跨线程投递：脸卡了、网抖了，
  最坏是几条消息没送到，主控的声音一秒都不许多等。
- websockets 只在 start() 时才 import：没装这个包只是脸不启用，
  语音功能一行不受影响。
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable

from ..config import FaceConfig
from ..logger import get_logger
from . import protocol

logger = get_logger(__name__)


class FaceServer:
    """状态 / 口型 / 字幕的广播站 + 触屏打断的接收站。"""

    def __init__(self, config: FaceConfig) -> None:
        self._config = config
        self._clients: set = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._interrupt = threading.Event()
        # 收到打断时立刻执行的动作（通常是 synthesizer.interrupt）。
        # 不能等主循环下一轮轮询 —— 一句长话要是等它播完再停就太蠢了。
        self.on_interrupt: Callable[[], None] | None = None

    # ---------------- 生命周期 ----------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="xiaoyu-face", daemon=True
        )
        self._thread.start()
        self._ready.wait(timeout=5)
        logger.info(
            "表情脸服务已启动 | 端口 {} | 浏览器打开 face/index.html?token={} "
            "（同一 WiFi 的设备把 localhost 换成本机 IP）",
            self._config.port,
            self._config.token,
        )

    def _run(self) -> None:
        import websockets  # 延迟导入：没装这个包也能 import 本模块

        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._ready.set()

        async def serve() -> None:
            async with websockets.serve(
                self._handle, self._config.host, self._config.port
            ):
                await asyncio.Future()  # 永远运行，直到 stop()

        try:
            self._loop.run_until_complete(serve())
        except Exception:
            logger.exception("表情脸服务意外退出")

    def stop(self) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)

    # ---------------- 接收（脸 -> 主控） ----------------

    async def _handle(self, websocket) -> None:
        # websockets 17.x 用 websocket.request.path，老版本是 websocket.path，都试一下
        path = getattr(getattr(websocket, "request", None), "path", None)
        if path is None:
            path = getattr(websocket, "path", "")
        if not protocol.token_ok(path, self._config.token):
            logger.warning("表情脸连接被拒绝：token 不对")
            await websocket.close(code=4401, reason="bad token")
            return

        self._clients.add(websocket)
        logger.info("表情脸已连接（当前 {} 张脸）", len(self._clients))
        try:
            async for raw in websocket:
                if protocol.parse_command(raw):
                    logger.info("收到触屏打断")
                    self._interrupt.set()
                    if self.on_interrupt is not None:
                        try:
                            self.on_interrupt()
                        except Exception:
                            logger.exception("打断动作执行失败")
        except Exception:
            logger.debug("表情脸连接中断", exc_info=True)
        finally:
            self._clients.discard(websocket)
            logger.info("表情脸断开（还剩 {} 张脸）", len(self._clients))

    # ---------------- 打断查询（主控用） ----------------

    def interrupted(self) -> bool:
        """本轮说话期间有没有被点过脸。主控查完要走 clear_interrupt()。"""
        return self._interrupt.is_set()

    def clear_interrupt(self) -> None:
        self._interrupt.clear()

    # ---------------- 发送（主控 -> 脸） ----------------

    def publish_state(self, state: str) -> None:
        self._publish(protocol.state_message(state))

    def publish_mouth(self, level: float) -> None:
        self._publish(protocol.mouth_message(level))

    def publish_caption(self, text: str) -> None:
        text = text.strip()
        if text:
            self._publish(protocol.caption_message(text))

    def publish_gaze(self, x: float, y: float) -> None:
        """眼睛跟随（S6a）。发布节奏由 vision 那边节流（默认 10Hz）。"""
        self._publish(protocol.gaze_message(x, y))

    def _publish(self, payload: dict) -> None:
        """跨线程广播。没有脸连着时直接丢 —— 脸是锦上添花，
        绝不能反过来拖住主控。"""
        loop = self._loop
        if loop is None or not self._clients:
            return
        asyncio.run_coroutine_threadsafe(
            self._broadcast(protocol.encode(payload)), loop
        )

    async def _broadcast(self, message: str) -> None:
        dead = []
        for client in self._clients:
            try:
                await client.send(message)
            except Exception:
                dead.append(client)
        for client in dead:
            self._clients.discard(client)