"""触屏打断：speak_stream 必须真的停下来（不联网，用假引擎假喇叭）。"""

from __future__ import annotations

import threading
import unittest

import numpy as np

from xiaoyu.tts.engine import Speech
from xiaoyu.tts.synthesizer import Synthesizer


class _FakeSpeaker:
    """假装喇叭：记录放了几段；可配置成"放到第 N 段时触发打断"。"""

    def __init__(self, interrupt_after: int | None = None, owner: Synthesizer | None = None):
        self.played: list[str] = []
        self.stop_calls = 0
        self._interrupt_after = interrupt_after
        self._owner = owner

    def play_array(self, samples, samplerate=None, wait=True) -> None:
        self.played.append("x")
        if (
            self._interrupt_after is not None
            and len(self.played) == self._interrupt_after
            and self._owner is not None
        ):
            self._owner.interrupt()

    def stop(self) -> None:
        self.stop_calls += 1


class _FakeEngine:
    name = "fake"

    def synthesize(self, text: str) -> Speech:
        return Speech(samples=np.zeros(32, dtype=np.float32), samplerate=16000)


def _make_synthesizer(interrupt_after: int | None = None):
    synth = Synthesizer.__new__(Synthesizer)   # 绕过 __init__（不加载真模型）
    synth._engine = _FakeEngine()
    synth._interrupted = threading.Event()
    speaker = _FakeSpeaker(interrupt_after=interrupt_after, owner=synth)
    synth._speaker = speaker
    return synth, speaker


def _sentences(n: int):
    """生成器：用 GeneratorExit 侦测有没有被 close()（正常迭代完不算）。"""
    closed = {"v": False}

    def gen():
        try:
            for i in range(n):
                yield f"第{i}句"
        except GeneratorExit:
            # 只有被 close() 才会走到这里；正常迭代完不会
            closed["v"] = True
            raise

    return gen(), closed


class SpeakStreamInterruptTest(unittest.TestCase):
    def test_normal_run_plays_everything(self):
        synth, speaker = _make_synthesizer()
        sentences, closed = _sentences(5)
        captions: list[str] = []
        synth.speak_stream(sentences, on_sentence=captions.append)
        self.assertEqual(len(speaker.played), 5)
        self.assertEqual(captions, [f"第{i}句" for i in range(5)])
        self.assertFalse(closed["v"])          # 正常播完不关流

    def test_interrupt_midway_stops_early(self):
        synth, speaker = _make_synthesizer(interrupt_after=1)
        sentences, closed = _sentences(50)
        synth.speak_stream(sentences)
        self.assertLess(len(speaker.played), 50)   # 没放完
        self.assertGreaterEqual(speaker.stop_calls, 1)  # 喇叭被 stop() 过
        self.assertTrue(closed["v"])               # 大模型的流被主动关闭

    def test_should_stop_extra_check(self):
        synth, speaker = _make_synthesizer()
        sentences, _ = _sentences(10)
        calls = {"n": 0}

        def stop_after_three() -> bool:
            calls["n"] += 1
            return calls["n"] > 3

        synth.speak_stream(sentences, should_stop=stop_after_three)
        self.assertLess(len(speaker.played), 10)

    def test_interrupt_flag_survives_for_app_loop(self):
        """打断标志在 speak_stream 结束后仍能被主控读到（主控要靠它换状态）。"""
        synth, _ = _make_synthesizer(interrupt_after=1)
        sentences, _ = _sentences(10)
        synth.speak_stream(sentences)
        self.assertTrue(synth.interrupted)


if __name__ == "__main__":
    unittest.main()
