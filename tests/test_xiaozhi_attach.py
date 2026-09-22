"""`app.attach_xiaozhi` 的接线测试 —— 本体和板子之间**唯一的一处焊点**。

为什么值得单独一个文件：这处接错了（transcribe 接错对象、caption 忘了给、
face 传成 None、resample 漏掉）表现出来的样子是
「板子能连上、能握手、会话也不报错，但问它什么都不理你」——
光看日志看不出来，必须**真起服务、真连 WebSocket、真编解码**走一遍。

音频是合成的正弦，不是人声：这一层不测"听得准不准"，只测**线接对了没有**。
听得准不准是 `test_xiaozhi_brain.py` 和 ASR 自己的事。
"""

from __future__ import annotations

import asyncio
import json
import socket
import unittest
import urllib.request
from pathlib import Path

import numpy as np

from xiaoyu.app import attach_xiaozhi
from xiaoyu.config import Settings, XiaozhiConfig
from xiaoyu.xiaozhi.audio_codec import create_opus_codec
from xiaoyu.xiaozhi.protocol import AudioParams

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "xiaozhi" / "device"
DEVICE_PARAMS = AudioParams(
    format="opus", sample_rate=16000, channels=1, frame_duration=60
)
MAC = "aa:bb:cc:dd:ee:ff"
UPLINK_FRAMES = 8  # 8 x 60ms = 0.48 秒，刚好过 brain 那道 0.3 秒的门槛


# ---------------------------------------------------------------- 假接线对象


class _Recognizer:
    """冒充 SpeechRecognizer：只记下喂进来多长，回一句固定的话。"""

    def __init__(self, text: str = "今天天气不错") -> None:
        self.text = text
        self.calls = 0
        self.seconds = 0.0

    def transcribe(self, samples) -> str:
        self.calls += 1
        self.seconds = float(np.asarray(samples).size) / DEVICE_PARAMS.sample_rate
        return self.text


class _Client:
    """冒充 DeepSeekClient：记下收到的字，回一句话外加一个情绪。"""

    def __init__(self) -> None:
        self.seen: list[str] = []

    def stream_reply(self, user_text: str, on_emotion=None):
        self.seen.append(user_text)
        if on_emotion is not None:
            on_emotion("happy", 0.8)
        yield "我在呢"


class _Synth:
    """冒充 Synthesizer：合成 0.5 秒 440Hz 正弦（24k，跟下行的采样率一致）。"""

    RATE = 24000

    def __init__(self) -> None:
        self.said: list[str] = []

    def synthesize_array(self, text: str):
        self.said.append(text)
        count = int(self.RATE * 0.5)
        seconds = np.arange(count, dtype=np.float64) / self.RATE
        wave = np.sin(2 * np.pi * 440.0 * seconds) * 0.3
        return wave.astype(np.float32), self.RATE


class _Face:
    """冒充 FaceServer：只记下字幕和情绪，不开 8765 那个口。"""

    def __init__(self) -> None:
        self.captions: list[str] = []
        self.emotions: list[tuple[str, float]] = []

    def publish_caption(self, text: str) -> None:
        self.captions.append(text)

    def publish_emotion(self, mood: str, intensity: float) -> None:
        self.emotions.append((mood, intensity))


# ---------------------------------------------------------------- 工具


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _settings(**overrides) -> Settings:
    return Settings(xiaozhi=XiaozhiConfig(**overrides))


def _ask_ota(url: str) -> str:
    """照上游 ota.cc 的样子 POST 一次，返回它让我们连的 ws 地址。"""
    data = json.dumps({"board": "test", "version": "0", "mac": MAC}).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Device-Id": MAC,
            "Client-Id": "11111111-2222-3333-4444-555555555555",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return payload["websocket"]["url"]


def _listen(state: str) -> str:
    message = json.loads(
        (FIXTURES / "device_listen_start_auto.json").read_text(encoding="utf-8")
    )
    message["state"] = state
    return json.dumps(message, ensure_ascii=False)


async def _talk(ws_url: str) -> list[bytes]:
    """连上去、握手、说 0.48 秒的话、收下行二进制帧。"""
    import websockets

    codec = create_opus_codec(DEVICE_PARAMS, DEVICE_PARAMS)
    try:
        size = codec.downlink.frame_samples
        rate = codec.downlink.sample_rate
        packets = []
        for index in range(UPLINK_FRAMES):
            seconds = np.arange(index * size, (index + 1) * size, dtype=np.float64) / rate
            wave = np.sin(2 * np.pi * 440.0 * seconds) * 8000.0
            packets.append(codec.encode(wave.astype("<i2").tobytes()))

        async with websockets.connect(ws_url, open_timeout=10) as socket:
            await socket.send((FIXTURES / "device_hello.json").read_text(encoding="utf-8"))
            await asyncio.wait_for(socket.recv(), 10)  # 服务端的 hello
            await socket.send(_listen("start"))
            for packet in packets:
                await socket.send(packet)
            await socket.send(_listen("stop"))

            binary: list[bytes] = []
            while len(binary) < 2:
                try:
                    message = await asyncio.wait_for(socket.recv(), 10)
                except TimeoutError:
                    break
                if isinstance(message, (bytes, bytearray)):
                    binary.append(bytes(message))
            return binary
    finally:
        codec.close()


# ---------------------------------------------------------------- 用例


class AttachXiaozhiTest(unittest.TestCase):
    def test_disabled_by_default(self):
        """默认关：没板子的时候不该占端口、不该建编解码器。"""
        self.assertIsNone(
            attach_xiaozhi(Settings(), _Recognizer(), _Client(), _Synth(), face=None)
        )

    def test_whole_way_through_when_enabled(self):
        """从 OTA 到下行音频走一整遍，五个接线点一个都不许断。"""
        http_port, ws_port = _free_port(), _free_port()
        settings = _settings(
            enabled=True, host="127.0.0.1", port=http_port, websocket_port=ws_port
        )
        recognizer, client, synth, face = _Recognizer(), _Client(), _Synth(), _Face()

        server = attach_xiaozhi(settings, recognizer, client, synth, face=face)
        if server is None:
            self.fail("attach_xiaozhi 返回了 None —— 端口被占？PyAV 没装？")
        self.addCleanup(server.stop)
        self.assertTrue(server.bound, "两个口没都监听上")

        ws_url = _ask_ota(f"http://127.0.0.1:{http_port}/xiaozhi/ota/")
        self.assertEqual(ws_url, f"ws://127.0.0.1:{ws_port}/xiaozhi/v1/")
        binary = asyncio.run(_talk(ws_url))

        # ① 上行音频完整地到了 ASR（0.48 秒，没被截断）
        self.assertEqual(recognizer.calls, 1)
        self.assertAlmostEqual(recognizer.seconds, 0.48, places=2)
        # ② 识别出来的字原样交给了大脑
        self.assertEqual(client.seen, ["今天天气不错"])
        # ③ 大脑的回话交给了 TTS
        self.assertEqual(synth.said, ["我在呢"])
        # ④ 字幕和情绪真的流到脸上了
        self.assertEqual(face.captions, ["我在呢"])
        self.assertEqual(face.emotions, [("happy", 0.8)])
        # ⑤ 合成出来的声音真的编码下发到设备了（>0 帧，且不是静音）
        self.assertGreaterEqual(len(binary), 2, "一帧音频都没下发 —— 接线断了")

        codec = create_opus_codec(DEVICE_PARAMS, DEVICE_PARAMS)
        try:
            decoded = b"".join(codec.decode(packet) for packet in binary)
        finally:
            codec.close()
        samples = np.frombuffer(decoded, dtype="<i2")
        self.assertGreater(samples.size, 0, "下发的东西解不回 PCM")
        rms = float(np.sqrt(np.mean(np.square(samples.astype(np.float64)))))
        self.assertGreater(rms, 100.0, "下发的是静音 —— TTS 那段没接上？")


if __name__ == "__main__":
    unittest.main()
