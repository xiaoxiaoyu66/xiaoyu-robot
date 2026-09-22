"""真 Opus 编解码（`xiaoyu/xiaozhi/opus_av.py`）的测试。

这些用例锁的是**实测出来的三个硬事实**（见那个模块开头），不是"我以为的 Opus 常识"：

1. 一帧进必须一个包出 —— 得显式要 `frame_duration`，不然 60ms 会被切成 3 个包
2. 解码器固定 48kHz 输出 —— 所以必须重采样回上行采样率，不然 ASR 拿到"快三倍"的音频
3. 重采样有一次性的 16 样本延迟 —— 下面用"几个包加起来的样本数"把它钉住

`av` 没装时**整个类跳过**，不是失败：离线机器上跑测试不该被它卡住。
"""

from __future__ import annotations

import unittest

import numpy as np

from xiaoyu.xiaozhi.audio_codec import (
    CodecError,
    CodecParams,
    FakeOpusCodec,
    OpusCodec,
    create_opus_codec,
)

try:
    import av  # noqa: F401

    HAS_AV = True
except ImportError:  # pragma: no cover —— 装了 PyAV 的机器上不走这条
    HAS_AV = False

if HAS_AV:
    from xiaoyu.xiaozhi.opus_av import (
        AvOpusCodec,
        assert_codec_contract,
        resample_float,
        resample_pcm16,
    )

# 设备真 hello 的那套参数（§1.3 样本：16000 / 单声道 / 60ms）
UPLINK = CodecParams(sample_rate=16000, channels=1, frame_duration=60)
# 我们下行声明的格式（§8.3 + build_server_hello 的默认值）
DOWNLINK = CodecParams(sample_rate=24000, channels=1, frame_duration=60)

# 重采样的一次性延迟（事实 3）：16 个样本，只出现在第一段
SWR_DELAY_SAMPLES = 16


def _tone(samples: int, rate: int, hz: float = 440.0) -> bytes:
    wave = np.sin(np.arange(samples) / rate * 2 * np.pi * hz) * 8000
    return wave.astype("<i2").tobytes()


@unittest.skipUnless(HAS_AV, "没装 PyAV（pip install av）—— 真编解码这层跳过")
class AvOpusCodecTest(unittest.TestCase):
    def setUp(self):
        self.codec = AvOpusCodec(UPLINK, DOWNLINK)

    def tearDown(self):
        self.codec.close()

    # ---------------------------------------------------------- 契约

    def test_satisfies_the_codec_protocol(self):
        """对外形状得跟假货一模一样，不然 Connection 那边要改两遍。"""
        self.assertIsInstance(self.codec, OpusCodec)
        self.assertIsInstance(self.codec, AvOpusCodec)

    def test_one_frame_in_one_packet_out(self):
        """事实 1：60ms 一帧进去，出来必须是**一个**包，不是三个。"""
        pcm = _tone(DOWNLINK.frame_samples, DOWNLINK.sample_rate)
        packet = self.codec.encode(pcm)
        self.assertTrue(packet)
        self.assertLess(len(packet), len(pcm))  # 真的压缩了

    def test_downlink_frame_size_matches_libopus(self):
        """编码器自己的 frame_size 必须等于我们声明的下行一帧 —— 不等就会多包/少包。"""
        self.assertEqual(self.codec._encoder.frame_size, DOWNLINK.frame_samples)

    def test_encode_rejects_empty(self):
        with self.assertRaises(CodecError):
            self.codec.encode(b"")

    def test_encode_rejects_half_frame(self):
        """少半个字节都不行 —— "先用 iter_pcm_frames() 切齐"这条要在错误里说出来。"""
        with self.assertRaises(CodecError):
            self.codec.encode(b"\x00" * (DOWNLINK.frame_bytes - 2))

    def test_encode_rejects_two_frames(self):
        """一次只收一帧：两帧一起进来会变成"一个包对两帧"，下游对不上。"""
        with self.assertRaises(CodecError):
            self.codec.encode(b"\x00" * (DOWNLINK.frame_bytes * 2))

    def test_encode_rejects_non_bytes(self):
        with self.assertRaises(CodecError):
            self.codec.encode("这不是字节")  # type: ignore[arg-type]

    def test_encode_after_close_raises(self):
        self.codec.close()
        with self.assertRaises(CodecError):
            self.codec.encode(_tone(DOWNLINK.frame_samples, DOWNLINK.sample_rate))

    # ---------------------------------------------------------- 解码

    def test_decode_never_raises_on_garbage(self):
        """事实 2 的伴生契约：坏包返回 b"" 并记一次错，绝不把会话搞死。"""
        before = self.codec.decode_errors
        self.assertEqual(self.codec.decode(b"not an opus packet at all"), b"")
        self.assertEqual(self.codec.decode(b""), b"")
        self.assertEqual(self.codec.decode(None), b"")  # type: ignore[arg-type]
        self.assertEqual(self.codec.decode_errors, before + 3)

    def test_decode_after_close_is_ignored(self):
        self.codec.close()
        self.assertEqual(self.codec.decode(b"\x01\x02\x03"), b"")

    def test_uplink_sample_count_comes_back_at_uplink_rate(self):
        """事实 2 + 3：解回来的音频必须是**上行采样率**的样本数。

        60ms 的上行包解出来应该接近 960 个 16k 样本（第一包差那 16 个）。
        要是忘了重采样，这里会是 2880 —— 那就是"快三倍"的音频。
        """
        encoder = av.CodecContext.create("libopus", "w")
        encoder.sample_rate = UPLINK.sample_rate
        encoder.layout = "mono"
        encoder.format = "s16"
        encoder.options = {"frame_duration": "60"}
        encoder.open()

        pcm = _tone(UPLINK.frame_samples, UPLINK.sample_rate, hz=300.0)
        frame = av.AudioFrame.from_ndarray(
            np.ascontiguousarray(np.frombuffer(pcm, dtype="<i2").reshape(1, -1)),
            format="s16",
            layout="mono",
        )
        frame.sample_rate = UPLINK.sample_rate
        frame.pts = 0
        packet = bytes(list(encoder.encode(frame))[0])

        once = len(self.codec.decode(packet)) // 2
        self.assertAlmostEqual(once, UPLINK.frame_samples, delta=SWR_DELAY_SAMPLES)

        # 连发几包：那 16 个样本**一直含在 swresample 的管道里**，不 flush 就出不来，
        # 所以总数固定少 16（不是每包都少）。一句话差 1 毫秒，ASR 不在乎 ——
        # 但**别拿它做精确的时长记账**（这就是"事实 3"的全部含义）。
        total = once
        for _ in range(2):
            total += len(self.codec.decode(packet)) // 2
        self.assertEqual(total, UPLINK.frame_samples * 3 - SWR_DELAY_SAMPLES)

    # ---------------------------------------------------------- 重采样

    def test_resample_same_rate_is_identity(self):
        data = np.linspace(-0.5, 0.5, 100, dtype=np.float32)
        out = resample_float(data, 16000, 16000)
        np.testing.assert_allclose(out, data)

    def test_resample_changes_length(self):
        """22050 -> 24000（Piper 的输出喂给 Opus 必须走这一步）。"""
        data = np.zeros(22050, dtype=np.float32)
        self.assertEqual(resample_float(data, 22050, 24000).size, 24000)

    def test_resample_48k_to_16k(self):
        data = np.zeros(48000, dtype=np.float32)
        out = resample_float(data, 48000, 16000)
        self.assertAlmostEqual(out.size, 16000, delta=SWR_DELAY_SAMPLES)

    def test_resample_pcm16_keeps_two_bytes_per_sample(self):
        pcm = _tone(22050, 22050, hz=200.0)
        out = resample_pcm16(pcm, 22050, 24000)
        self.assertEqual(len(out), 24000 * 2)

    def test_resample_empty_is_empty(self):
        self.assertEqual(resample_float(np.zeros(0, dtype=np.float32), 16000, 24000).size, 0)

    # ---------------------------------------------------------- 自检工具

    def test_assert_codec_contract_passes(self):
        """现场用的自检（scripts/xiaozhi_fake_device.py --selftest 走的就是它）。"""
        assert_codec_contract(self.codec)


class CreateOpusCodecTest(unittest.TestCase):
    """接线点：默认必须交真货，假货要显式要 —— 这条规矩不能松。"""

    def test_allow_fake_still_gives_the_fake(self):
        codec = create_opus_codec(_audio_params(), allow_fake=True)
        self.assertIsInstance(codec, FakeOpusCodec)

    @unittest.skipUnless(HAS_AV, "没装 PyAV")
    def test_default_gives_the_real_codec(self):
        codec = create_opus_codec(_audio_params())
        try:
            self.assertIsInstance(codec, AvOpusCodec)
        finally:
            codec.close()

    @unittest.skipUnless(HAS_AV, "没装 PyAV")
    def test_downlink_defaults_match_the_server_hello(self):
        """hello 里声明 24000/60ms（protocol.py 的默认值），codec 必须跟着一样。"""
        codec = create_opus_codec(_audio_params())
        try:
            self.assertEqual(codec.downlink.sample_rate, 24000)
            self.assertEqual(codec.downlink.frame_duration, 60)
        finally:
            codec.close()


def _audio_params():
    from xiaoyu.xiaozhi.protocol import AudioParams

    return AudioParams(format="opus", sample_rate=16000, channels=1, frame_duration=60)


if __name__ == "__main__":
    unittest.main()
