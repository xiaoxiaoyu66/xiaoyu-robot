"""情绪回调的接线测试 —— 锁的是一个真出过的 bug。

2026-09-18 用户实测报「情绪回调失败」，logs\\error.log 里是：

    File "xiaoyu\\llm\\client.py", line 567, in stream_reply
        on_emotion(mood, intensity)
    TypeError: _caption_emitter.<locals>.emit() takes 1 positional argument but 2 were given

原因：情绪回调被塞进了给**字幕**用的转发器，而那个转发器是按字幕的签名
写死的（`emit(text)`，1 个参数）。情绪是两个参数（mood, intensity），直接炸。

这个 bug 隐蔽在哪：异常被 stream_reply 兜住了（情绪是表演，绝不能带崩对话），
所以只在 logs\\error.log 里留一行。**聊天一切正常，就是脸永远不变表情** ——
用户不问根本发现不了。

所以要测的不是转发器本身（它很无辜），而是：
「从 app 的接法出发，走一遍真的 stream_reply，情绪能不能到脸」。
另接一遍回调的测试是抓不到「app 接错了」的，这里必须走 build_callbacks()。

    python -m unittest discover tests
不联网、不花钱：把客户端内部的 openai 对象换成一个假流。
"""

from __future__ import annotations

import dataclasses
import unittest
from types import SimpleNamespace
from unittest import mock

from xiaoyu import app
from xiaoyu.app import build_callbacks
from xiaoyu.config import LlmConfig, Settings
from xiaoyu.llm.client import DeepSeekClient


def _settings() -> Settings:
    """api_key 是假值，只为了过构造函数那一关（不会真的发请求）。"""
    return Settings(llm=dataclasses.replace(LlmConfig(), api_key="test"))


class _FakeStream:
    """假的流式响应：每个 piece 当成一个 chunk。"""

    def __init__(self, pieces):
        self._chunks = [
            SimpleNamespace(
                choices=[SimpleNamespace(delta=SimpleNamespace(content=piece))]
            )
            for piece in pieces
        ]

    def __iter__(self):
        return iter(self._chunks)


class _FakeOpenAI:
    """只实现 stream_reply 用到的那一条路径。"""

    def __init__(self, pieces):
        self._pieces = pieces
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create)
        )

    def _create(self, **kwargs):
        return _FakeStream(self._pieces)


class _FakeFace:
    """只记录收到了什么，不碰 canvas / 网络。"""

    def __init__(self, sink):
        self._sink = sink

    def publish_caption(self, text: str) -> None:
        self._sink.append(("caption", text))

    def publish_emotion(self, mood: str, intensity: float) -> None:
        self._sink.append(("emotion", mood, intensity))


class EmotionWiringTest(unittest.TestCase):
    def _client(self, pieces) -> DeepSeekClient:
        client = DeepSeekClient(_settings(), memory=None)
        client._client = _FakeOpenAI(pieces)
        return client

    def test_emotion_callback_accepts_two_arguments(self):
        """压死这个 bug 的最小断言：转发器必须吃得下 (mood, intensity)。

        以前这里就是 TypeError，而且是**静默**的。
        """
        sink = []
        on_emotion = build_callbacks(_FakeFace(sink))[1]
        self.assertIsNotNone(on_emotion)

        on_emotion("happy", 0.8)

        self.assertEqual(sink, [("emotion", "happy", 0.8)])

    def test_emotion_reaches_the_face_through_a_real_stream_reply(self):
        """端到端：真 stream_reply + app 的接法，情绪要落到脸上。"""
        sink = []
        on_caption, on_emotion = build_callbacks(_FakeFace(sink))
        client = self._client(
            ["今天", "好开心呀！", "[emotion]happy,0.8[/emotion]"]
        )

        said = "".join(client.stream_reply("你好", on_emotion=on_emotion))

        self.assertIn(("emotion", "happy", 0.8), sink)
        # 字幕那条接法同时建出来了（它由合成器按句调，不像情绪走 stream_reply）
        self.assertIsNotNone(on_caption)
        # 标记绝不能漏进要念出来的正文
        self.assertNotIn("[emotion]", said)
        self.assertIn("好开心呀", said)

    def test_partial_marker_split_across_chunks_still_lands(self):
        """流式边界切在标记中间（"[emotion]" 被切成 "[", "emotion]..."）也要认得。

        这是真 DeepSeek 上必现的切法，以前漏过一次（标记被念出来）。
        """
        sink = []
        _, on_emotion = build_callbacks(_FakeFace(sink))
        client = self._client(["我有点", "难过。", "[", "emotion]sad,0.9[/emotion]"])

        said = "".join(client.stream_reply("怎么了", on_emotion=on_emotion))

        self.assertIn(("emotion", "sad", 0.9), sink)
        self.assertNotIn("[", said)

    def test_no_face_means_no_callbacks(self):
        """脸没开的时候两个回调都是 None，client 那边会自己跳过。"""
        on_caption, on_emotion = build_callbacks(None, None)
        self.assertIsNone(on_caption)
        self.assertIsNone(on_emotion)

    def test_one_broken_receiver_does_not_block_the_others(self):
        """一个接收方炸了，别的照收 —— 字幕和情绪不能互相拖累。"""
        sink = []

        class Boom:
            def publish_caption(self, text):
                raise RuntimeError("我坏了")

            def publish_emotion(self, mood, intensity):
                sink.append((mood, intensity))

        on_caption, on_emotion = build_callbacks(Boom(), None)
        # 这行堆栈是**预期**的，临时把 logger 换掉，别让它污染测试输出
        with mock.patch.object(app, "logger", SimpleNamespace(exception=lambda *a, **k: None)):
            on_caption("随便一句")     # 不该抛出来
        on_emotion("angry", 0.5)

        self.assertEqual(sink, [("angry", 0.5)])


if __name__ == "__main__":
    unittest.main()