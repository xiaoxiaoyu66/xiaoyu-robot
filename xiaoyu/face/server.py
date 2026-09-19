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
from pathlib import Path

from ..config import FaceConfig
from ..logger import get_logger
from . import protocol, static

logger = get_logger(__name__)

# 心跳间隔（B 阶段）。页面那边超过 15 秒收不到任何消息（含心跳）就重连，
# 所以这里必须显著小于 15 秒，留够抖动余量。
HEARTBEAT_SECONDS = 5.0


class FaceServer:
    """状态 / 口型 / 字幕的广播站 + 触屏打断的接收站。"""

    def __init__(self, config: FaceConfig, web_root: Path | None = None) -> None:
        self._config = config
        # face/ 目录：给了就顺带用 HTTP 伺服它，平板开 http://<主机IP>:8765/ 就是脸
        self._web_root = web_root
        # 最近一次状态。新脸连上时补推一次，不然它要干等到"下一次状态变化"
        # 才知道现在是什么状态（平板挂起重连就是这个场景）。
        self._last_state: str | None = None
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
            "表情脸服务已启动 | 平板 / 浏览器打开 http://<本机IP>:{} "
            "（同一 WiFi 的平板把 IP 换成这台机器的；本机直接 http://localhost:{}）",
            self._config.port,
            self._config.port,
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
                self._handle,
                self._config.host,
                self._config.port,
                # 同一个端口既接 WebSocket、也发网页（B 阶段）
                process_request=self._process_request,
            )
            self._bound.set()
            heartbeat = asyncio.create_task(self._heartbeat())
            try:
                await self._stop_requested
            finally:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
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

    # ---------------- HTTP：网页和 WebSocket 共用一个端口（B 阶段） ----------------

    def _process_request(self, connection, request):
        """握手是 WebSocket 就放行，其余请求当普通网页伺服。

        为什么合成一个端口：平板只要打开 `http://<主机IP>:8765/`，
        页面里的 location.hostname 天然就是主机地址 —— 配对零操作，
        也不用旁边再起一个 `python -m http.server 8080`（那个重启就没了）。
        """
        if (request.headers.get("Upgrade") or "").lower() == "websocket":
            return None
        return self._http_response(request)

    def _http_response(self, request):
        """静态文件的 HTTP 响应；文件不在就是 404。"""
        from websockets.datastructures import Headers
        from websockets.http11 import Response

        root = self._web_root
        found = static.resolve(root, request.path) if root is not None else None
        if found is None:
            body = b"not found"
            return Response(
                404, "Not Found", Headers([("Content-Length", str(len(body)))]), body
            )

        content_type, body = found
        if content_type.startswith("text/html"):
            # 把真 token 填进页面：平板不用记 ?token=...
            body = static.with_token(body, self._config.token)
        headers = Headers(
            [
                ("Content-Type", content_type),
                ("Content-Length", str(len(body))),
                # 平板常年开着，别缓存住旧版本 —— 改了脸刷新就看见
                ("Cache-Control", "no-store"),
            ]
        )
        return Response(200, "OK", headers, body)

    async def _heartbeat(self) -> None:
        """闲置时定期发个空包，让页面能自己发现假死。

        主控待机时一句话都不发，页面分不清"安静"和"断线"：实测平板挂起浏览器后，
        脸还在屏幕上画着，其实连接早断了，要等 50 多秒才接回。
        """
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            if self._clients:
                await self._broadcast(protocol.encode(protocol.tick_message()))

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
        await self._send_current_state(websocket)
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

    async def _send_current_state(self, websocket) -> None:
        """刚连上先把当前状态推一次。

        不推的话，新脸要等到"下一次状态变化"才知道现在什么状态：
        平板挂起后重连时主控多半在待机，屏幕上就永远停在"连接中"。
        """
        if self._last_state is None:
            return
        try:
            await websocket.send(
                protocol.encode(protocol.state_message(self._last_state))
            )
        except Exception:
            logger.debug("给新连上的脸补推状态失败", exc_info=True)

    # ---------------- 打断查询（主控用） ----------------

    def interrupted(self) -> bool:
        """本轮说话期间有没有被点过脸。主控查完要走 clear_interrupt()。"""
        return self._interrupt.is_set()

    def clear_interrupt(self) -> None:
        self._interrupt.clear()

    # ---------------- 发送（主控 -> 脸） ----------------

    def publish_state(self, state: str) -> None:
        # 记下来，给之后连上的脸补推
        self._last_state = state
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