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
        # 正常收摊的标志。stop() 里的 loop.stop() 会让 asyncio 抛
        # "Event loop stopped before Future completed" —— 那是正常路径不是崩溃。
        # 不区分的话每次退出都往 error.log 塞一段假堆栈，真故障会被淹掉。
        self._stopping = threading.Event()
        # 端口真的监听上了才置位。start() 靠它决定是报"已启动"还是报错 ——
        # 端口被占时以前会假报成功，常驻模式下等于"脸没了还以为有"。
        self._bound = threading.Event()
        # 正常收摊的信号。stop() 放行它，serve() 才会往下走去关服务器。
        self._stop_requested: asyncio.Future | None = None
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
        if not self._bound.wait(timeout=5):
            logger.error(
                "表情脸没能监听 {}:{}（端口被占？），本次没有脸，语音不受影响",
                self._config.host,
                self._config.port,
            )
            return
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
        self._stop_requested = self._loop.create_future()
        self._ready.set()

        # 别用 loop.stop() 收摊：那会让 run_until_complete 抛 RuntimeError，
        # 而且 socket 都没关就把循环停了，退出时满屏 "Task was destroyed" /
        # "Event loop is closed"。改成放行一个 future，让 serve() 自己
        # 关服务器、等它真关上，再让循环自然退出。
        async def serve() -> None:
            server = await websockets.serve(
                self._handle, self._config.host, self._config.port
            )
            self._bound.set()
            try:
                await self._stop_requested
            finally:
                server.close()
                await server.wait_closed()

        try:
            self._loop.run_until_complete(serve())
        except Exception:
            if self._stopping.is_set():
                logger.debug("表情脸服务已停止")
            else:
                logger.exception("表情脸服务意外退出")
        finally:
            self._shutdown_loop()

    def _shutdown_loop(self) -> None:
        """把事件循环收干净再关掉。

        Windows 上 Proactor 的 accept 协程不会随 server.close() 一起走，
        不取消的话退出时就打 "Task was destroyed but it is pending!"。
        """
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:
            logger.debug("关事件循环时有点小状况，忽略", exc_info=True)
        finally:
            loop.close()

    def stop(self) -> None:
        self._stopping.set()
        if self._loop is None:
            return
        try:
            self._loop.call_soon_threadsafe(self._release_stop)
        except RuntimeError:
            # 循环已经关了（退出时 stop() 来晚了），没什么可做的
            logger.debug("事件循环已关闭，表情脸无需再停")

    def _release_stop(self) -> None:
        """在事件循环线程里放行 serve()。"""
        fut = self._stop_requested
        if fut is not None and not fut.done():
            fut.set_result(None)

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

    def publish_emotion(self, mood: str, intensity: float) -> None:
        """情绪系统：一轮回复的情绪标签，脸拿去变眉眼/光环。"""
        self._publish(protocol.emotion_message(mood, intensity))

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