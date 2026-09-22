"""小智的真网络层（A 档第二批）：**HTTP 答 OTA、WebSocket 管对话**。

设计照抄 `xiaoyu/face/server.py` 的一半（独立守护线程 + 自带事件循环 + 延迟 import
`websockets`），另一半被一个实测出来的坑改掉了 —— 见下面「坑 4」。

## 坑 4（2026-09-22 实测）：OTA 和 WebSocket **不能共用一个端口**

文档里原本写的是"照脸页那套，HTTP 和 WebSocket 同一个口"。写完之后一测就发现不行：

    上游 ota.cc 发的是 **POST**（board.GetSystemInfoJson() 非空时就是 POST，空才降级成 GET）
    而 websockets 17 的 HTTP 层**根本不收带 body 的请求**：
        websockets/http11.py:  int(headers["Content-Length"]) != 0
                               -> raise ValueError("unsupported request body")
    结果：POST 过来的 OTA 请求，连接被直接掐掉，客户端只看到 RemoteDisconnected。
    证据：`logging.basicConfig(level=DEBUG)` 之后能看到那行 ValueError；
          同一个 URL 用 GET 请求就是 200 —— 区别只在 body。

所以现在是**两个口**：

    8766  HTTP（OTA）      —— 标准库 ThreadingHTTPServer，支持 POST + body
    8767  WebSocket（对话） —— websockets，只处理 Upgrade 握手

设备会连哪个口不是我们说了算，是**我们告诉它的**：OTA 应答里的 `websocket.url`
就是 `ws://<设备请求时用的主机>:8767/xiaozhi/v1/`（`OtaConfig.websocket_port`）。
上游本来就是这个模型（它默认指到 `wss://api.tenclass.net/...`，完全是另一个主机）。

## 板子到货那天，日志里要看到的两行

    小智：OTA 请求 | 设备=xx:xx:... | 板子自述={...} | 已下发 ws://10.x.x.x:8767/xiaozhi/v1/
    小智：设备连上了 | 协议版本=1 | 传输=websocket | 音频=16000Hz/60ms/1ch

`docs/到货当天_测试清单.md` 第 6 步就是照着这两行判的。
"""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ..config import XiaozhiConfig
from ..logger import get_logger
from . import ota, protocol
from .audio_codec import CodecError, OpusCodec
from .server import Connection, Transport
from .session import HELLO_TIMEOUT_SECONDS

logger = get_logger(__name__)


# ============================================================ WebSocket 那一半


class WebsocketTransport:
    """`Transport` 的真实现：把一条 websockets 连接包成 recv / send / close。"""

    def __init__(self, socket) -> None:
        self._socket = socket
        self.closed = False
        self.recv_calls = 0

    async def recv(self) -> object | None:
        import websockets

        self.recv_calls += 1
        try:
            message = await self._socket.recv()
        except websockets.exceptions.ConnectionClosed:
            # 契约：对端收工返回 None，不抛
            return None
        if isinstance(message, (bytes, bytearray, memoryview)):
            return bytes(message)
        return message

    async def send(self, frame: bytes | str) -> None:
        await self._socket.send(frame)

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            await self._socket.close()
        except Exception:  # noqa: BLE001
            logger.debug("关小智连接时有点小状况，忽略", exc_info=True)


class ReplayTransport:
    """把一个**已经读掉**的帧塞回去，再交给下家。

    只为了"先读 hello 建 codec、再让 Connection 从第一帧开始跑"这一件事 ——
    比让 `Connection` 支持延迟建 codec 简单得多，也不用动那份已经验过的状态机驱动。
    """

    def __init__(self, inner: Transport, first: object | None = None) -> None:
        self._inner = inner
        self._pending: list[object] = [] if first is None else [first]

    async def recv(self) -> object | None:
        if self._pending:
            return self._pending.pop(0)
        return await self._inner.recv()

    async def send(self, frame: bytes | str) -> None:
        await self._inner.send(frame)

    async def close(self) -> None:
        await self._inner.close()


# ============================================================ HTTP 那一半（OTA）


class _OtaRequestHandler(BaseHTTPRequestHandler):
    """OTA 端点。用标准库写，因为它要收 POST 的 body（见模块开头「坑 4」）。"""

    server_version = "xiaoyu-ota/1.0"

    def do_POST(self) -> None:  # noqa: N802 —— 标准库的命名规矩
        self._answer()

    def do_GET(self) -> None:  # noqa: N802
        self._answer()

    def _answer(self) -> None:
        body = self._read_body()
        answer = ota.handle_request(self.path, self.headers, body, self.server.ota_config)
        if answer.ok:
            info = ota.parse_device_info(body)
            device = ota.device_id(ota.normalize_headers(self.headers))
            self.server.ota_requests += 1
            logger.info(
                "小智：OTA 请求 | 设备={} | 板子自述={} | 已下发 {}",
                device or "（没带 Device-Id）",
                json.dumps(info, ensure_ascii=False) if info else "（没带 body）",
                answer.body.decode("utf-8", "replace"),
            )
        else:
            logger.warning(
                "小智：OTA 没答成（{}）| path={} | {}", answer.reason, self.path, answer.status
            )
        self.send_response(answer.status)
        self.send_header("Content-Type", answer.content_type)
        self.send_header("Content-Length", str(len(answer.body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(answer.body)

    def _read_body(self) -> bytes:
        """把 body 读全。Content-Length 缺失或不是数就当没 body —— 绝不抛。"""
        raw = self.headers.get("Content-Length")
        try:
            length = int(raw) if raw else 0
        except (TypeError, ValueError):
            length = 0
        if length <= 0:
            return b""
        try:
            return self.rfile.read(length)
        except Exception:  # noqa: BLE001
            logger.debug("读 OTA 请求 body 失败，当空的处理", exc_info=True)
            return b""

    def log_message(self, fmt: str, *args) -> None:
        # 标准库默认往 stderr 打一行，绕开它走我们的日志
        logger.debug("小智 OTA HTTP：%s", fmt % args)


class _OtaHttpServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, config) -> None:
        super().__init__(address, handler)
        self.ota_config = config
        self.ota_requests = 0


# ============================================================ 主服务


class XiaozhiServer:
    """设备这一侧的服务器：HTTP(OTA) + WebSocket(对话) 各一个口，一起起停。"""

    def __init__(
        self,
        config: XiaozhiConfig,
        *,
        codec_factory,
        responder_factory,
        hello_timeout: float = HELLO_TIMEOUT_SECONDS,
    ) -> None:
        self._config = config
        self._codec_factory = codec_factory
        self._responder_factory = responder_factory
        self.hello_timeout = float(hello_timeout)

        self._http: _OtaHttpServer | None = None
        self._http_thread: threading.Thread | None = None

        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._bound = threading.Event()
        self._stopping = threading.Event()
        self._stop_requested: asyncio.Future | None = None

        self.devices_connected = 0

    # ------------------------------------------------------------ 生命周期

    @property
    def bound(self) -> bool:
        return self._bound.is_set()

    @property
    def ota_requests(self) -> int:
        return self._http.ota_requests if self._http is not None else 0

    def start(self) -> bool:
        """起两个口。**两个都监听上了才算成功** —— 半个服务比没有更难查。"""
        if self._http_thread is None:
            if not self._start_http():
                return False
        if self._thread is None:
            if not self._start_websocket():
                return False
        logger.info(
            "小智服务就绪 | 设备配网页里填 http://<本机IP>:{} ｜ OTA 路径 {} | "
            "WebSocket 走 {} 口 {}",
            self._config.port,
            ota.OTA_PATH,
            self._config.websocket_port,
            self._config.websocket_path,
        )
        return True

    def stop(self) -> None:
        self._stopping.set()
        if self._http is not None:
            try:
                self._http.shutdown()
                self._http.server_close()
            except Exception:  # noqa: BLE001
                logger.debug("关 OTA 的 HTTP 口时有点小状况，忽略", exc_info=True)
            self._http = None
        loop = self._loop
        if loop is None:
            return
        try:
            loop.call_soon_threadsafe(self._release_stop)
        except RuntimeError:
            logger.debug("小智服务的循环已经关了，无需再停")

    def _release_stop(self) -> None:
        future = self._stop_requested
        if future is not None and not future.done():
            future.set_result(None)

    # ------------------------------------------------------------ HTTP(OTA)

    def _start_http(self) -> bool:
        try:
            self._http = _OtaHttpServer(
                (self._config.host, self._config.port),
                _OtaRequestHandler,
                self._config.ota_config(),
            )
        except OSError as exc:
            logger.error(
                "小智的 OTA 口没能监听 {}:{}（{}）—— 板子这一轮连不上",
                self._config.host,
                self._config.port,
                exc,
            )
            self._http = None
            return False
        self._http_thread = threading.Thread(
            target=self._http.serve_forever, name="xiaoyu-ota-http", daemon=True
        )
        self._http_thread.start()
        return True

    # ------------------------------------------------------------ WebSocket

    def _start_websocket(self) -> bool:
        self._thread = threading.Thread(
            target=self._run, name="xiaoyu-xiaozhi-ws", daemon=True
        )
        self._thread.start()
        self._ready.wait(timeout=5)
        if not self._bound.wait(timeout=5):
            logger.error(
                "小智的 WebSocket 口没能监听 {}:{}（端口被占？）—— 板子这一轮连不上",
                self._config.host,
                self._config.websocket_port,
            )
            self._thread = None
            return False
        return True

    def _run(self) -> None:
        import websockets  # 延迟导入：没装这个包也能 import 本模块

        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._stop_requested = self._loop.create_future()
        self._ready.set()

        async def serve() -> None:
            server = await websockets.serve(
                self._handle,
                self._config.host,
                self._config.websocket_port,
                process_request=self._process_request,
                ping_interval=20,
                ping_timeout=20,
            )
            self._bound.set()
            try:
                await self._stop_requested
            finally:
                server.close()
                await server.wait_closed()

        try:
            self._loop.run_until_complete(serve())
        except Exception:  # noqa: BLE001
            if self._stopping.is_set():
                logger.debug("小智的 WebSocket 口已停止")
            else:
                logger.exception("小智的 WebSocket 口意外退出（本体不受影响）")
        finally:
            self._shutdown_loop()

    def _shutdown_loop(self) -> None:
        """把事件循环收干净再关掉 —— 和 face/server.py 同样的理由（Windows 的 accept）。"""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:  # noqa: BLE001
            logger.debug("关小智服务的事件循环时有点小状况，忽略", exc_info=True)
        finally:
            loop.close()

    def _process_request(self, connection, request):
        """WebSocket 口上来的普通 HTTP 请求：给个明确的 404，别让人干等。

        （OTA 不在这个口上 —— 见模块开头「坑 4」。）
        """
        if (request.headers.get("Upgrade") or "").lower() == "websocket":
            return None
        from websockets.datastructures import Headers
        from websockets.http11 import Response

        body = (
            f"这里是小智的 WebSocket 口，不是网页。OTA 在 http://<主机>:{self._config.port}{ota.OTA_PATH}\n"
        ).encode("utf-8")
        logger.debug("小智：WebSocket 口收到普通 HTTP 请求 {} —— 回了 404", request.path)
        return Response(
            404,
            "Not Found",
            Headers([("Content-Type", "text/plain; charset=utf-8"), ("Content-Length", str(len(body)))]),
            body,
        )

    async def _handle(self, socket) -> None:
        transport = WebsocketTransport(socket)
        logger.info("小智：有人连上了 WebSocket，等它的 hello")
        try:
            first = await asyncio.wait_for(transport.recv(), timeout=self.hello_timeout)
        except TimeoutError:
            logger.warning(
                "小智：连上了但 {:.0f} 秒没发 hello，关掉（这不是我们要的协议？）",
                self.hello_timeout,
            )
            await transport.close()
            return

        if first is None:
            await transport.close()
            return

        parsed = protocol.parse_device_hello(first)
        if not parsed.ok:
            logger.warning(
                "小智：第一条不是合法的设备 hello（{}），关掉 —— 板子那边会打它自己的日志",
                parsed.error,
            )
            await transport.close()
            return

        hello = parsed.value
        params = hello.audio_params
        logger.info(
            "小智：设备连上了 | 协议版本={} | 传输={} | 音频={}Hz/{:.3g}ms/{}ch | features={}",
            hello.version,
            hello.transport,
            params.sample_rate,
            params.frame_duration if params.frame_duration is not None else 60.0,
            params.channels,
            ",".join(sorted(hello.features)) or "无",
        )
        self.devices_connected += 1

        codec: OpusCodec
        try:
            codec = self._codec_factory(params)
        except CodecError as exc:
            logger.error("小智：建不出编解码器，这块板子这一轮连不了：{}", exc)
            await transport.close()
            return

        connection = Connection(
            ReplayTransport(transport, first),
            codec=codec,
            responder=self._responder_factory(),
            hello_timeout=self.hello_timeout,
        )
        try:
            await connection.run()
        except Exception:  # noqa: BLE001
            logger.exception("小智：这条会话炸了（已吞掉，服务继续）")
        finally:
            try:
                codec.close()
            except Exception:  # noqa: BLE001
                logger.debug("关编解码器时有点小状况，忽略", exc_info=True)
            await transport.close()
