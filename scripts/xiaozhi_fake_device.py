"""假设备：**没有板子**，也能把板子到货那天要走的每一步先走一遍。

## 它解决什么问题

板子不通的时候，原因只有两类：**硬件**（接线 / 供电 / 固件）还是**我们的代码**。
这个脚本跟上游 `ota.cc` + `websocket_protocol.cc` 做一模一样的网络动作
（POST 自述 -> 拿 websocket 地址 -> hello -> listen start -> 音频 -> listen stop），
唯独音频是它自己用正弦波合成的，不经过麦克风、喇叭、杜邦线。

    这个脚本通了、板子不通   ->   问题在硬件，去查线
    这个脚本也不通           ->   问题在我们代码，别去翻杜邦线

这就是它唯一的用处：**把「硬件坏」和「代码坏」分开**。它验不了麦克风和喇叭。

## 跑法（项目根目录下）

    # 一、只体检音频编解码（不联网、本体不用在跑）
    python scripts\\xiaozhi_fake_device.py --selftest

    # 二、自己起个服务、自己当设备连上去（一条命令验完我们这一侧）
    python scripts\\xiaozhi_fake_device.py

    # 三、打正在跑的本体（另一个窗口先设 XIAOYU_XIAOZHI_ENABLED=1 把本体起起来）
    python scripts\\xiaozhi_fake_device.py --ota http://127.0.0.1:8766/xiaozhi/ota/

    # 四、打板子真会打的那个地址（IP 换成 ipconfig 里的 IPv4）
    python scripts\\xiaozhi_fake_device.py --ota http://192.168.1.23:8766/xiaozhi/ota/

## 参数

    --selftest        只跑编解码体检，不联网
    --ota URL         打这个地址的 OTA 口（不给就自己起服务，见上面「二」）
    --frames N        上行发几帧音频（默认 8 帧 = 0.48 秒）
    --timeout SEC     等**第一帧**回话的上限（默认 20 秒）
                      —— 真大脑要走 ASR + LLM + TTS，慢是正常的，别当成失败
    --port N          自查模式的 OTA 口（默认随便挑一个空的，不占 8766）
    --ws-port N       自查模式的 WebSocket 口（默认随便挑一个空的，不占 8767）

## 看什么

每行 `->` 是设备发出去的，`<-` 是服务器回的，最后「结论」那几行是给眼睛看的。
本体那边的窗口同时会打「小智：OTA 请求 | ...」和「小智：设备连上了 | ...」，
两边对着看，一眼就知道卡在哪一步。

## 输出为什么全是 ASCII 加中文、没有一个表情符号

因为把它重定向到文件时（`> out.txt`）Windows 的默认编码是 GBK，
表情符号会直接让脚本崩在 `print` 上。现场排查的工具，先保证打得出来。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import socket
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from xiaoyu.config import XiaozhiConfig
from xiaoyu.logger import setup_logging
from xiaoyu.xiaozhi import ota, protocol
from xiaoyu.xiaozhi.audio_codec import OpusCodec, create_opus_codec
from xiaoyu.xiaozhi.opus_av import assert_codec_contract
from xiaoyu.xiaozhi.server import FixedResponder
from xiaoyu.xiaozhi.ws import XiaozhiServer

FIXTURES = ROOT / "tests" / "fixtures" / "xiaozhi" / "device"

# 板子报上来的音频参数：跟固件里写死的一样（16000Hz / 60ms / 单声道 / opus）
DEVICE_PARAMS = protocol.AudioParams(
    format="opus", sample_rate=16000, channels=1, frame_duration=60
)
DEVICE_MAC = "aa:bb:cc:dd:ee:ff"
BOARD = "bread-compact-wifi-lcd"
FIRMWARE_VERSION = "2.5.0"

RULE = "=" * 68


def say(line: str = "") -> None:
    print(line, flush=True)


class Report:
    """收集「通了 / 没通」，最后一次性打出来 —— 中间不打断，方便看全过程。"""

    def __init__(self) -> None:
        self.checks: list[tuple[bool, str]] = []

    def check(self, ok: bool, text: str) -> bool:
        self.checks.append((ok, text))
        say(f"  [{'OK' if ok else '!!'}] {text}")
        return ok

    def finish(self) -> int:
        failed = [text for ok, text in self.checks if not ok]
        say()
        say(RULE)
        if not failed:
            say("结论：我们这一侧（OTA + 协议 + 编解码）是通的。")
            say("      板子连不上时，先去查硬件：接线、供电、固件版本。")
            return 0
        say(f"结论：我们自己这边就有 {len(failed)} 处不通 —— 先别动杜邦线：")
        for text in failed:
            say(f"      · {text}")
        return 1


# ---------------------------------------------------------------- 编解码


def make_codec() -> OpusCodec:
    """假设备用的编解码器。

    **方向说明**（容易看反）：`create_opus_codec` 里 `encode` 是「要发给对端」的那一方向。
    假设备把上行参数两边都填上，于是 `encode` 出来的是**上行包**（16000Hz），
    `decode` 解回来的是**自己听到的音频**（也按 16000Hz 输出）——
    一个实例同时扮演板子的嘴和耳朵，跟真板子一致。
    """
    return create_opus_codec(DEVICE_PARAMS, DEVICE_PARAMS)


def tone_packets(codec: OpusCodec, count: int) -> list[bytes]:
    """合成 count 个上行 Opus 包（440Hz 正弦，约四分之一音量）。"""
    size = codec.downlink.frame_samples
    rate = codec.downlink.sample_rate
    packets: list[bytes] = []
    for index in range(count):
        start = index * size
        seconds = np.arange(start, start + size, dtype=np.float64) / rate
        wave = np.sin(2 * np.pi * 440.0 * seconds) * 8000.0
        packets.append(codec.encode(wave.astype("<i2").tobytes()))
    return packets


# ---------------------------------------------------------------- ① OTA


def ask_ota(url: str, mac: str, timeout: float, report: Report) -> tuple[str, str] | None:
    say()
    say("== 1. OTA（板子开机第一件事就是问这个）==")
    body = json.dumps(
        {"board": BOARD, "version": FIRMWARE_VERSION, "mac": mac}, ensure_ascii=False
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Device-Id": mac,
            "Client-Id": str(uuid.uuid4()),
            "Content-Type": "application/json",
        },
    )
    say(f"  -> POST {url}")
    say(f"     {body.decode('utf-8')}")
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read()
    except Exception as exc:  # noqa: BLE001 —— 这一条就是给人看的诊断
        say(f"  <- 连不上：{type(exc).__name__}: {exc}")
        report.check(False, f"OTA 口连不上（{url}）—— 本体起了吗？XIAOYU_XIAOZHI_ENABLED=1")
        return None

    took_ms = (time.monotonic() - started) * 1000
    text = raw.decode("utf-8", errors="replace")
    say(f"  <- {status}（{took_ms:.0f}ms）")
    say(f"     {text}")

    if not report.check(status == 200, f"OTA 回了 200（拿到 {status}）"):
        return None
    try:
        payload = json.loads(text)
    except ValueError as exc:
        report.check(False, f"OTA 应答不是合法 JSON：{exc}")
        return None
    if not isinstance(payload, dict):
        report.check(False, "OTA 应答的顶层不是对象")
        return None
    section = payload.get("websocket")
    if not report.check(isinstance(section, dict), "应答里有 websocket 段"):
        return None
    ws_url = section.get("url")
    if not report.check(isinstance(ws_url, str) and ws_url, "websocket 段里有 url"):
        return None
    report.check(
        "activation" not in payload,
        "应答里没有 activation 段（有的话板子会卡在激活界面不动）",
    )
    say(f"  解读：板子接下来会去连 {ws_url}")
    return str(ws_url), str(section.get("token") or "")


# ---------------------------------------------------------------- ②③ WebSocket


def _listen(state: str, session_id: str) -> str:
    message = json.loads(
        (FIXTURES / "device_listen_start_auto.json").read_text(encoding="utf-8")
    )
    message["state"] = state
    if session_id:
        message["session_id"] = session_id
    return json.dumps(message, ensure_ascii=False)


async def _drain(
    socket, wait_first: float, *, idle: float = 2.0
) -> tuple[list[bytes], list[str], float | None]:
    """收帧，直到「安静了 idle 秒」为止；中间每条都打出来。

    **等待分两段，这是故意的**：第一帧要给足 `wait_first` 秒 —— 本体那边是
    ASR -> LLM -> TTS 串起来的，好几秒没有下行音频是正常的。第一帧到了之后
    才用 `idle` 判断「说完了」。要是一开始就只等 2 秒，会把正常的大脑误判成失败。

    返回值第三个是**最后一条消息到达的时刻**（一条都没有就是 None）——
    延迟要用它算，不能用「函数返回的时刻」，那样等于把 idle 也算成了延迟。
    """
    binary: list[bytes] = []
    texts: list[str] = []
    last_at: float | None = None
    started = time.monotonic()
    while True:
        if last_at is None:
            left = wait_first - (time.monotonic() - started)
        else:
            left = idle - (time.monotonic() - last_at)
        if left <= 0:
            break
        try:
            message = await asyncio.wait_for(socket.recv(), left)
        except TimeoutError:
            break
        except Exception as exc:  # noqa: BLE001 —— 对端断了也是一种结果
            say(f"  <- 连接断了：{type(exc).__name__}: {exc}")
            break
        last_at = time.monotonic()
        if isinstance(message, (bytes, bytearray)):
            binary.append(bytes(message))
            say(f"  <- 二进制 {len(message)} 字节（第 {len(binary)} 帧音频）")
        else:
            texts.append(str(message))
            say(f"  <- {message}")
    return binary, texts, last_at


async def converse(
    ws_url: str, token: str, frames: int, timeout: float, report: Report
) -> None:
    import websockets

    say()
    say("== 2. WebSocket 握手（板子拿到 url 后连这里）==")
    say(f"  -> 连 {ws_url}")
    headers = {"Authorization": f"Bearer {token}"} if token else None

    async with websockets.connect(
        ws_url, additional_headers=headers, open_timeout=timeout
    ) as socket:
        hello = (FIXTURES / "device_hello.json").read_text(encoding="utf-8")
        say(f"  -> {hello.strip()}")
        await socket.send(hello)
        reply = json.loads(await asyncio.wait_for(socket.recv(), timeout))
        say(f"  <- {json.dumps(reply, ensure_ascii=False)}")

        session_id = str(reply.get("session_id") or "")
        report.check(
            reply.get("type") == "hello" and reply.get("transport") == "websocket",
            "服务器回了它自己的 hello（transport=websocket）",
        )
        report.check(bool(session_id), f"服务器给了会话 id（{session_id or '空'}）")
        down = reply.get("audio_params") or {}
        if isinstance(down, dict) and down:
            say(
                f"  解读：服务器说它的下行音频是 {down.get('sample_rate')}Hz / "
                f"{down.get('frame_duration')}ms / {down.get('channels')}ch"
            )

        codec = make_codec()
        try:
            payloads = tone_packets(codec, max(1, frames))
        except Exception as exc:  # noqa: BLE001
            report.check(False, f"合成上行音频失败：{exc}")
            return

        size = codec.downlink.frame_bytes
        frame_ms = codec.downlink.frame_duration
        seconds = len(payloads) * frame_ms / 1000.0
        say()
        say(f"== 3. 说一句话（listen start -> {len(payloads)} 帧音频 -> listen stop）==")
        say(
            f"     上行：{len(payloads)} 帧 x {size} 字节 = {seconds:.2f} 秒 "
            f"@{codec.downlink.sample_rate}Hz/{frame_ms:g}ms"
            "（合成的 440Hz 正弦，不是人声 —— 真大脑会听不出来，那是正常的）"
        )
        started = time.monotonic()
        await socket.send(_listen("start", session_id))
        say("  -> listen start")
        for payload in payloads:
            await socket.send(payload)
        say(f"  -> {len(payloads)} 个二进制包（上面合成的音频）")
        await socket.send(_listen("stop", session_id))
        say("  -> listen stop")

        binary, _texts, last_at = await _drain(socket, timeout)
        if not binary:
            if last_at is None:
                say(f"     等了 {timeout:.0f} 秒，连一条消息都没等到。")
            else:
                say("     收到了状态消息，但一帧音频都没有。")
            say(
                "     可能原因：这一轮里 ASR / LLM / TTS 抛异常了，或者它还在算。"
                "去本体那边的日志找「小智会话 ... 收工」那一行；"
                "确认是「还在算」就加大 --timeout。"
            )
            report.check(False, "服务器没回音频（这一轮没跑完）")
            return
        turn_ms = ((last_at or started) - started) * 1000

        decoded = b"".join(codec.decode(packet) for packet in binary)
        samples = np.frombuffer(decoded, dtype="<i2") if decoded else np.zeros(0, "<i2")
        played = samples.size / codec.downlink.sample_rate if samples.size else 0.0
        rms = (
            float(np.sqrt(np.mean(np.square(samples.astype(np.float64)))))
            if samples.size
            else 0.0
        )
        say(
            f"     下行：{len(binary)} 帧 -> 解出 {samples.size} 个采样 = {played:.2f} 秒，"
            f"音量 RMS={rms:.0f}"
        )
        say(f"     这一轮从 listen stop 到收完用了 {turn_ms:.0f}ms")
        if rms < 1.0:
            say(
                "     注意：回的全是静音。自查模式用的是固定应答，本来就发静音；"
                "如果打的是本体，那就是 TTS 这段没合成出声音。"
            )
        report.check(played > 0.0, f"下行音频能解回 PCM（{played:.2f} 秒）")


# ---------------------------------------------------------------- 自查模式


def run_standalone(args: argparse.Namespace, report: Report) -> tuple[Any, str] | None:
    """在本进程里起一个小智服务（固定应答），然后把 OTA 地址交出去。

    端口默认随机挑空的，**故意不占 8766 / 8767** —— 免得跟正在跑的本体撞上，
    那样只会得到一堆莫名其妙的失败。
    """
    http_port = args.port or _free_port()
    ws_port = args.ws_port or _free_port()
    config = XiaozhiConfig(
        host="127.0.0.1", port=http_port, websocket_port=ws_port, token="xiaoyu"
    )
    server = XiaozhiServer(
        config,
        codec_factory=create_opus_codec,
        responder_factory=lambda: FixedResponder(frames=3),
    )
    say()
    say(f"（自查模式：本进程里起了一个小智服务 —— OTA {http_port} 口 / WebSocket {ws_port} 口，"
        "应答是固定的 3 帧静音）")
    if not server.start():
        report.check(False, f"起不了小智服务（{http_port} 或 {ws_port} 被占？）")
        return None
    return server, f"http://127.0.0.1:{http_port}{ota.OTA_PATH}"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# ---------------------------------------------------------------- 入口


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="xiaozhi_fake_device",
        description="假设备：不用板子，先把板子要走的路走一遍",
    )
    parser.add_argument("--selftest", action="store_true", help="只跑编解码体检，不联网")
    parser.add_argument("--ota", default=None, help="打这个地址的 OTA 口（不给就自己起服务）")
    parser.add_argument("--frames", type=int, default=8, help="上行发几帧音频（默认 8）")
    parser.add_argument(
        "--timeout",
        type=float,
        default=20.0,
        help="等第一帧回话的上限秒数（真大脑要 ASR+LLM+TTS，默认 20）",
    )
    parser.add_argument("--port", type=int, default=None, help="自查模式的 OTA 口")
    parser.add_argument("--ws-port", type=int, default=None, help="自查模式的 WebSocket 口")
    parser.add_argument("--log-level", default="INFO", help="日志级别，默认 INFO")
    args = parser.parse_args(argv)

    setup_logging(level=args.log_level)
    report = Report()

    say(RULE)
    say("假设备：把板子要走的路先走一遍（音频是合成的，不过麦克风和喇叭）")
    say(RULE)

    say()
    say("== 0. 编解码体检 ==")
    codec = make_codec()
    try:
        assert_codec_contract(codec)
    except Exception as exc:  # noqa: BLE001 —— 现场工具，报清楚比抛栈有用
        report.check(False, f"编解码体检没过：{exc}")
        say("     先修这个。最常见的原因是没装 PyAV：py -3.11 -m pip install av")
        return report.finish()
    finally:
        try:
            codec.close()
        except Exception:  # noqa: BLE001
            pass
    report.check(True, "Opus 编 / 解都对得上（PyAV 就位）")

    if args.selftest:
        return report.finish()

    standalone = None
    try:
        if args.ota:
            ota_url = args.ota
        else:
            started = run_standalone(args, report)
            if started is None:
                return report.finish()
            standalone, ota_url = started

        fetched = ask_ota(ota_url, DEVICE_MAC, args.timeout, report)
        if fetched is None:
            return report.finish()
        ws_url, token = fetched

        asyncio.run(converse(ws_url, token, args.frames, args.timeout, report))
        return report.finish()
    finally:
        if standalone is not None:
            standalone.stop()
            say()
            say("（自查模式起的那个服务已经收工）")


if __name__ == "__main__":
    raise SystemExit(main())
