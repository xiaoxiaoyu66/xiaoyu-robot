"""大脑桥接（`xiaoyu/xiaozhi/brain.py`）的测试 —— 全假货、不联网、不需要任何模型。

这里验的是**接线**：上行音频有没有喂给 ASR、回话怎么变成下行 PCM、采样率有没有拉对、
某一轮出岔子会不会把会话搞死。真 ASR / 真 LLM / 真 TTS 的准头不归这里管
（那是 HANDOFF §9 那条教训：冒烟必须走线上同一段代码，而单测只管接线）。
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

import numpy as np

from xiaoyu.xiaozhi.audio_codec import CodecParams, FakeOpusCodec
from xiaoyu.xiaozhi.brain import BrainParts, BrainResponder, to_pcm16

UPLINK = CodecParams(sample_rate=16000, channels=1, frame_duration=60)
DOWNLINK = CodecParams(sample_rate=24000, channels=1, frame_duration=60)

# 上行 1 秒（16000 个 16k 样本）
ONE_SECOND = (np.zeros(16000, dtype="<i2")).tobytes()


def _codec() -> FakeOpusCodec:
    return FakeOpusCodec(UPLINK, DOWNLINK)


def _request(audio: bytes = ONE_SECOND, codec: FakeOpusCodec | None = None):
    from xiaoyu.xiaozhi.server import TurnRequest

    return TurnRequest(session=None, codec=codec or _codec(), audio=audio)


def _parts(
    *,
    text: str = "今天天气怎么样",
    sentences: tuple[str, ...] = ("今天晴。", "风不大。"),
    tts_rate: int = 22050,
    samples_per_sentence: int = 4410,
    fail_in_reply: bool = False,
):
    """四个接线点的假货 + 一本调用账。"""
    calls = SimpleNamespace(transcribe=[], reply=[], synth=[], resample=[])

    def transcribe(audio: np.ndarray) -> str:
        calls.transcribe.append(np.asarray(audio))
        return text

    def reply(user_text: str, on_emotion=None):
        calls.reply.append(user_text)
        if fail_in_reply:
            raise RuntimeError("模型炸了")
        for sentence in sentences:
            yield sentence

    def synthesize(sentence: str):
        calls.synth.append(sentence)
        return np.full(samples_per_sentence, 0.1, dtype=np.float32), tts_rate

    def resample(data: np.ndarray, src: int, dst: int) -> np.ndarray:
        calls.resample.append((src, dst))
        size = int(round(np.asarray(data).size * dst / src))
        return np.zeros(max(size, 0), dtype=np.float32)

    return BrainParts(transcribe, reply, synthesize, resample), calls


class BrainResponderTest(unittest.TestCase):
    def _collect(self, responder: BrainResponder, request):
        async def scenario():
            return [chunk async for chunk in responder.respond(request)]

        return asyncio.run(scenario())

    # ------------------------------------------------------------ 正常一轮

    def test_happy_path_produces_one_chunk_per_sentence(self):
        parts, calls = _parts()
        chunks = self._collect(BrainResponder(parts), _request())
        self.assertEqual(len(chunks), 2)
        self.assertEqual(calls.reply, ["今天天气怎么样"])
        self.assertEqual(calls.synth, ["今天晴。", "风不大。"])
        # 上行本来就是 16k，不该再重采样一次上行
        self.assertNotIn((16000, 16000), calls.resample)
        # 每段都要从 TTS 的 22050 拉到下行的 24000
        self.assertEqual(calls.resample, [(22050, 24000)] * 2)

    def test_chunk_length_is_resampled_length(self):
        parts, _ = _parts(samples_per_sentence=22050, tts_rate=22050)
        chunks = self._collect(BrainResponder(parts), _request())
        # 22050 样本 @22050 -> 24000 样本 @24000 -> 每样本 2 字节
        self.assertEqual(len(chunks[0]), 24000 * 2)

    def test_tts_already_at_downlink_rate_is_not_resampled(self):
        parts, calls = _parts(tts_rate=DOWNLINK.sample_rate)
        self._collect(BrainResponder(parts), _request())
        self.assertEqual(calls.resample, [])

    # ------------------------------------------------------------ 该跳过的时候要跳过

    def test_no_audio_skips_the_turn(self):
        """设备发了个空轮：不答。答了就成了"你还没说话它自己嘀咕"。"""
        parts, calls = _parts()
        responder = BrainResponder(parts)
        self.assertEqual(self._collect(responder, _request(audio=b"")), [])
        self.assertEqual(calls.transcribe, [])
        self.assertEqual(responder.dropped, 1)

    def test_too_short_audio_is_ignored(self):
        """0.1 秒的碎片喂 ASR 只会得到幻觉（"嗯"、"谢谢观看"那种）。"""
        parts, calls = _parts()
        responder = BrainResponder(parts)
        short = np.zeros(int(16000 * 0.1), dtype="<i2").tobytes()
        self.assertEqual(self._collect(responder, _request(audio=short)), [])
        self.assertEqual(calls.transcribe, [])
        self.assertEqual(responder.dropped, 1)

    def test_empty_transcript_is_ignored(self):
        parts, calls = _parts(text="   ")
        responder = BrainResponder(parts)
        self.assertEqual(self._collect(responder, _request()), [])
        self.assertEqual(calls.reply, [])
        self.assertEqual(responder.dropped, 1)

    # ------------------------------------------------------------ 出岔子不能搞死会话

    def test_reply_exception_is_swallowed(self):
        parts, _ = _parts(fail_in_reply=True)
        responder = BrainResponder(parts)
        self.assertEqual(self._collect(responder, _request()), [])
        # turns 记的是"识别出文字、开始回答的轮数" —— 这一轮确实识别出来了，
        # 只是回答时炸了。区分"没听懂"和"答砸了"，排查时是两条完全不同的路。
        self.assertEqual(responder.turns, 1)

    def test_transcribe_exception_is_swallowed(self):
        parts, _ = _parts()
        boom = BrainParts(
            transcribe=lambda audio: (_ for _ in ()).throw(RuntimeError("ASR 炸了")),
            reply=parts.reply,
            synthesize=parts.synthesize,
            resample=parts.resample,
        )
        self.assertEqual(self._collect(BrainResponder(boom), _request()), [])

    # ------------------------------------------------------------ 别的

    def test_uplink_is_resampled_to_16k_first(self):
        """设备要是以 8000Hz 上行，先拉到 16k 再喂 ASR —— 不然识别出来是慢速怪声。"""
        parts, calls = _parts()
        codec = FakeOpusCodec(
            CodecParams(sample_rate=8000, channels=1, frame_duration=60), DOWNLINK
        )
        audio = np.zeros(8000, dtype="<i2").tobytes()
        self._collect(BrainResponder(parts), _request(audio=audio, codec=codec))
        self.assertEqual(calls.resample[0], (8000, 16000))
        self.assertEqual(calls.transcribe[0].size, 16000)

    def test_emotion_callback_is_passed_through_to_reply(self):
        """情绪回调要**原样传给** reply —— DeepSeekClient.stream_reply 的第二个参数就是它。"""
        parts, _ = _parts()
        seen: list[tuple[str, bool]] = []

        def reply(text, on_emotion=None):
            if on_emotion is not None:
                on_emotion("happy", 0.8)
            seen.append((text, on_emotion is not None))
            yield "好呀。"

        wired = BrainParts(
            transcribe=parts.transcribe,
            reply=reply,
            synthesize=parts.synthesize,
            resample=parts.resample,
        )
        self._collect(BrainResponder(wired, on_emotion=lambda mood, level: None), _request())
        self.assertEqual(seen, [("今天天气怎么样", True)])

    def test_captions_fire_for_every_sentence(self):
        parts, _ = _parts()
        captions: list[str] = []
        self._collect(BrainResponder(parts, on_caption=captions.append), _request())
        self.assertEqual(captions, ["今天晴。", "风不大。"])

    def test_caption_failure_does_not_stop_the_audio(self):
        parts, _ = _parts()
        responder = BrainResponder(
            parts, on_caption=lambda text: (_ for _ in ()).throw(RuntimeError("脸炸了"))
        )
        self.assertEqual(len(self._collect(responder, _request())), 2)

    def test_max_reply_seconds_cuts_the_tail(self):
        """模型写作文的时候别把设备端缓冲灌爆。"""
        parts, _ = _parts(sentences=tuple(f"第{i}句。" for i in range(20)), samples_per_sentence=24000 * 2)
        chunks = self._collect(BrainResponder(parts, max_reply_seconds=3.0), _request())
        self.assertLess(len(chunks), 20)
        self.assertGreaterEqual(len(chunks), 1)

    def test_turns_counter(self):
        parts, _ = _parts()
        responder = BrainResponder(parts)
        self._collect(responder, _request())
        self.assertEqual(responder.turns, 1)


class Pcm16Test(unittest.TestCase):
    def test_scales_to_full_range(self):
        out = np.frombuffer(to_pcm16(np.array([1.0, -1.0, 0.0], dtype=np.float32)), dtype="<i2")
        self.assertEqual(list(out), [32767, -32767, 0])

    def test_clips_out_of_range(self):
        out = np.frombuffer(to_pcm16(np.array([9.0, -9.0], dtype=np.float32)), dtype="<i2")
        self.assertEqual(list(out), [32767, -32767])

    def test_duplicates_channels(self):
        out = np.frombuffer(to_pcm16(np.array([1.0, 0.0], dtype=np.float32), channels=2), dtype="<i2")
        self.assertEqual(list(out), [32767, 32767, 0, 0])

    def test_empty(self):
        self.assertEqual(to_pcm16(np.zeros(0, dtype=np.float32)), b"")


if __name__ == "__main__":
    unittest.main()
