"""Opus 编解码抽象层（`xiaoyu/xiaozhi/audio_codec.py`）的测试。

跑法：py -3.11 -m unittest discover tests

全程**离线**，也不装任何音频库：这一层现在只有假实现，真实现（`opuslib` /
`pyogg` / 调 ffmpeg）是**单独一步**（`docs/xiaozhi拆解.md` §2.2.1 第 4 项原文：
「假实现能跑通单测；真依赖单独一步，装完立刻补最小单测」）。

所以这里验三件事：

1. **帧算术对得上** —— 16000Hz / 60ms / 单声道 / s16le 一帧就是 1920 字节。
   这条错了后面全是歪的，而且错得不明显（声音会变调，但不会崩）。
2. **表外的参数当场报错**，不许凑合着跑 —— 22050（Matcha / Piper 的输出）和
   30ms（看着挺合理，Opus 就是不收）这两种最容易溜进来。
3. **假的也不能乱来** —— 默认 `create_opus_codec()` 必须拒绝交出假货；
   `decode()` 吃垃圾包必须返回空、**不许抛**（那是上行，抛出去等于把会话搞死）。

外部锚点只有真样本（`tests/fixtures/xiaozhi/`，逐字抄自上游协议文档）：
参数表要跟设备真实 hello 对得上，这一层才有意义。
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from xiaoyu.xiaozhi.audio_codec import (
    BYTES_PER_SAMPLE,
    SUPPORTED_FRAME_DURATIONS,
    SUPPORTED_SAMPLE_RATES,
    CodecError,
    CodecFormatError,
    CodecParams,
    CodecUnavailableError,
    FakeOpusCodec,
    OpusCodec,
    bytes_per_frame,
    check_channels,
    check_frame_duration,
    check_sample_rate,
    create_opus_codec,
    iter_pcm_frames,
    needs_resample,
    pcm_duration_seconds,
    samples_per_frame,
)
from xiaoyu.xiaozhi.protocol import AudioParams, parse_audio_params, parse_device_hello

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "xiaozhi"

DEVICE_HELLO = "device/device_hello.json"
DEVICE_HELLO_FULL = "device/device_hello_full.json"
SERVER_HELLO_24000 = "server/server_hello_24000.json"
SERVER_HELLO_16000 = "server/server_hello_16000.json"

# 设备端真实的 hello：opus / 16000 / 单声道 / 60ms（文档 §1.3）
UPLINK = CodecParams(sample_rate=16000, channels=1, frame_duration=60)
# 我们下行：opus / 24000 / 单声道 / 60ms（文档 §8.3）
DOWNLINK = CodecParams(sample_rate=24000, channels=1, frame_duration=60)


def _text(rel: str) -> str:
    return (FIXTURES / rel).read_text(encoding="utf-8")


def _json(rel: str) -> dict:
    return json.loads(_text(rel))


def _params(rel: str) -> AudioParams:
    """从真样本里抠出 audio_params（这一层唯一的外部锚点）。

    走 `parse_audio_params` 而不是 `parse_device_hello`：设备 hello 和
    server hello 的 `audio_params` 是同一个形状，但 server hello **没有
    `version` 字段**（§1.4 那个样本就只有 type / transport / session_id /
    audio_params），拿设备那套解析器去解它本来就不该通过。
    """
    result = parse_audio_params(_json(rel)["audio_params"])
    assert result.ok, result.error
    return result.value


class FrameMathTest(unittest.TestCase):
    """帧算术：错一个数，声音会变调而不是崩，最难查的那种。"""

    def test_16k_60ms_mono_is_1920_bytes(self):
        """设备上行那一帧。1920 = 16000 * 0.06 * 1(声道) * 2(s16le)。"""
        self.assertEqual(bytes_per_frame(16000, 60), 1920)

    def test_24k_60ms_mono_is_2880_bytes(self):
        """我们下行那一帧。"""
        self.assertEqual(bytes_per_frame(24000, 60), 2880)

    def test_48k_20ms_mono_is_1920_bytes(self):
        # 换了采样率和帧长、字节数撞回 1920 —— 别把"帧长"等同于"字节数"
        self.assertEqual(bytes_per_frame(48000, 20), 1920)

    def test_16k_60ms_is_960_samples(self):
        self.assertEqual(samples_per_frame(16000, 60), 960)

    def test_shortest_frame(self):
        # 8000Hz * 2.5ms = 20 个采样点 —— Opus 收的最小格子
        self.assertEqual(samples_per_frame(8000, 2.5), 20)

    def test_stereo_doubles_bytes(self):
        self.assertEqual(bytes_per_frame(16000, 60, channels=2), 3840)

    def test_every_supported_combination_is_integral(self):
        """参数表得自洽：任何一个合法组合都必须切得出整数个采样点。

        表里漏一个、或者加了 3000Hz 这种数，这条会当场抓住。
        """
        for rate in SUPPORTED_SAMPLE_RATES:
            for duration in SUPPORTED_FRAME_DURATIONS:
                with self.subTest(rate=rate, duration=duration):
                    samples = samples_per_frame(rate, duration)
                    self.assertEqual(samples * 1000, rate * duration)
                    self.assertGreater(bytes_per_frame(rate, duration), 0)

    def test_one_frame_lasts_exactly_the_frame_duration(self):
        total = bytes_per_frame(16000, 60)
        self.assertAlmostEqual(pcm_duration_seconds(total, 16000), 0.06, places=9)

    def test_one_second_of_16k_mono(self):
        self.assertEqual(pcm_duration_seconds(16000 * BYTES_PER_SAMPLE, 16000), 1.0)

    def test_zero_bytes_is_zero_seconds(self):
        self.assertEqual(pcm_duration_seconds(0, 24000), 0.0)

    def test_negative_bytes_is_rejected(self):
        with self.assertRaises(CodecFormatError):
            pcm_duration_seconds(-1, 16000)


class ParamValidationTest(unittest.TestCase):
    """参数校验：表外的值必须当场炸，这是这一层最值钱的部分。"""

    def test_defaults_are_the_device_uplink(self):
        params = CodecParams(sample_rate=16000)
        self.assertEqual(params.channels, 1)
        self.assertEqual(params.frame_duration, 60.0)
        self.assertEqual(params.format, "opus")
        self.assertEqual(params.frame_bytes, 1920)

    def test_frame_samples_property(self):
        self.assertEqual(UPLINK.frame_samples, 960)
        self.assertEqual(DOWNLINK.frame_bytes, 2880)

    def test_int_frame_duration_becomes_float(self):
        self.assertIsInstance(CodecParams(sample_rate=16000, frame_duration=60).frame_duration, float)

    def test_rejects_non_opus_format(self):
        with self.assertRaises(CodecFormatError):
            CodecParams(sample_rate=16000, format="pcm")

    def test_rejects_22050(self):
        """我们自己的 Matcha / Piper 就是 22050 —— 这条不是理论问题。"""
        with self.assertRaises(CodecFormatError) as ctx:
            CodecParams(sample_rate=22050)
        self.assertIn("22050", str(ctx.exception))

    def test_rejects_other_non_opus_rates(self):
        for rate in (32000, 44100, 11025, 0, -16000):
            with self.subTest(rate=rate):
                with self.assertRaises(CodecFormatError):
                    CodecParams(sample_rate=rate)

    def test_rejects_30ms_frame(self):
        """30ms 看着挺合理，Opus 就是不收 —— 必须拦住，别"四舍五入"。"""
        with self.assertRaises(CodecFormatError):
            CodecParams(sample_rate=16000, frame_duration=30)

    def test_rejects_bad_channels(self):
        for channels in (0, 3, -1):
            with self.subTest(channels=channels):
                with self.assertRaises(CodecFormatError):
                    CodecParams(sample_rate=16000, channels=channels)

    def test_rejects_non_int_channels(self):
        for channels in ("1", None, 1.0):
            with self.subTest(channels=channels):
                with self.assertRaises(CodecFormatError):
                    check_channels(channels)

    def test_bool_is_not_a_number(self):
        """True 在 Python 里 isinstance(True, int) 是成立的 —— 得挡住。"""
        with self.assertRaises(CodecFormatError):
            check_sample_rate(True)
        with self.assertRaises(CodecFormatError):
            check_channels(True)

    def test_rejects_non_numeric_frame_duration(self):
        with self.assertRaises(CodecFormatError):
            check_frame_duration("60")
        with self.assertRaises(CodecFormatError):
            check_frame_duration(None)

    def test_check_helpers_return_what_they_validated(self):
        self.assertEqual(check_sample_rate(24000), 24000)
        self.assertEqual(check_channels(2), 2)
        self.assertEqual(check_frame_duration(60), 60.0)

    def test_params_are_frozen(self):
        with self.assertRaises(Exception):
            UPLINK.sample_rate = 8000

    def test_from_audio_params_fills_missing_frame_duration(self):
        """§9.2 那个 server hello 只有 format + sample_rate，帧长得补默认值。"""
        params = CodecParams.from_audio_params(_params(SERVER_HELLO_16000))
        self.assertEqual(params.sample_rate, 16000)
        self.assertEqual(params.frame_duration, 60.0)
        self.assertEqual(params.frame_bytes, 1920)

    def test_from_audio_params_keeps_given_values(self):
        params = CodecParams.from_audio_params(_params(DEVICE_HELLO))
        self.assertEqual(params.sample_rate, 16000)
        self.assertEqual(params.channels, 1)
        self.assertEqual(params.frame_duration, 60.0)

    def test_from_audio_params_can_override_the_default(self):
        params = CodecParams.from_audio_params(
            _params(SERVER_HELLO_16000), default_frame_duration=20
        )
        self.assertEqual(params.frame_duration, 20.0)


class IterPcmFramesTest(unittest.TestCase):
    """切帧：TTS 出来的长度几乎不会正好是 60ms 的整数倍。"""

    def test_exact_multiple_gives_whole_frames(self):
        pcm = bytes(range(256)) * 15  # 3840 字节 = 2 帧
        frames = list(iter_pcm_frames(pcm, 16000, 60))
        self.assertEqual([len(f) for f in frames], [1920, 1920])
        self.assertEqual(b"".join(frames), pcm)

    def test_tail_is_padded_by_default(self):
        frames = list(iter_pcm_frames(b"\x01" * 2000, 16000, 60))
        self.assertEqual([len(f) for f in frames], [1920, 1920])
        # 补的是零，不是别的东西
        self.assertEqual(frames[1][80:], b"\x00" * 1840)

    def test_pad_tail_false_drops_the_tail(self):
        frames = list(iter_pcm_frames(b"\x01" * 2000, 16000, 60, pad_tail=False))
        self.assertEqual([len(f) for f in frames], [1920])

    def test_short_input_still_gives_one_frame(self):
        frames = list(iter_pcm_frames(b"\x02" * 100, 16000, 60))
        self.assertEqual(len(frames), 1)
        self.assertEqual(len(frames[0]), 1920)
        self.assertEqual(frames[0][:100], b"\x02" * 100)

    def test_empty_input_gives_nothing(self):
        self.assertEqual(list(iter_pcm_frames(b"", 16000, 60)), [])
        self.assertEqual(list(iter_pcm_frames(b"", 16000, 60, pad_tail=False)), [])

    def test_downlink_frames_are_2880(self):
        frames = list(iter_pcm_frames(b"\x03" * 5000, 24000, 60))
        self.assertEqual([len(f) for f in frames], [2880, 2880])

    def test_every_frame_is_the_same_size(self):
        """切出来长度不齐，下游 encode 就会炸 —— 这里先保证不齐不了。"""
        for total in (1, 100, 1919, 1920, 1921, 3839, 3840, 3841, 10000):
            with self.subTest(total=total):
                frames = list(iter_pcm_frames(b"\x04" * total, 16000, 60))
                self.assertTrue(all(len(f) == 1920 for f in frames))
                # 原数据一个字节都没丢
                self.assertEqual(b"".join(frames)[:total], b"\x04" * total)


class ResampleTest(unittest.TestCase):
    def test_opus_rates_do_not_need_resampling(self):
        for rate in SUPPORTED_SAMPLE_RATES:
            with self.subTest(rate=rate):
                self.assertFalse(needs_resample(rate))

    def test_matcha_piper_output_needs_resampling(self):
        """edge-tts 出 24000（能用），Matcha / Piper 出 22050（不能）。"""
        self.assertTrue(needs_resample(22050))

    def test_other_odd_rates_need_resampling(self):
        for rate in (32000, 44100, 11025):
            with self.subTest(rate=rate):
                self.assertTrue(needs_resample(rate))


class FakeOpusCodecTest(unittest.TestCase):
    """假实现：透传 + 记账。别让它假装自己会编 Opus。"""

    def setUp(self):
        self.codec = FakeOpusCodec(UPLINK, DOWNLINK)

    def test_conforms_to_the_protocol(self):
        """行为代码只认 OpusCodec 这个形状 —— 假货也得满足它。"""
        self.assertIsInstance(self.codec, OpusCodec)

    def test_decode_is_a_passthrough(self):
        packet = b"\xaa" * 40
        self.assertEqual(self.codec.decode(packet), packet)
        self.assertEqual(self.codec.decoded_packets, [packet])
        self.assertEqual(self.codec.decoded_bytes, 40)
        self.assertEqual(self.codec.decode_errors, 0)

    def test_decode_never_raises_on_junk(self):
        """上行是网络来的 —— 抛出去等于把会话搞死。"""
        for junk in (None, 0, 1.5, "not bytes", ["a"], {"a": 1}):
            with self.subTest(junk=junk):
                self.assertEqual(self.codec.decode(junk), b"")
        self.assertEqual(self.codec.decode_errors, 6)
        self.assertEqual(self.codec.decoded_packets, [])

    def test_decode_accepts_bytearray_and_memoryview(self):
        self.assertEqual(self.codec.decode(bytearray(b"\x01\x02")), b"\x01\x02")
        self.assertEqual(self.codec.decode(memoryview(b"\x03")), b"\x03")

    def test_decode_honours_the_reject_predicate(self):
        codec = FakeOpusCodec(UPLINK, DOWNLINK, reject_decode=lambda data: b"BAD" in data)
        self.assertEqual(codec.decode(b"BAD packet"), b"")
        self.assertEqual(codec.decode_errors, 1)
        self.assertEqual(codec.decode(b"good"), b"good")
        self.assertEqual(codec.decode_errors, 1)

    def test_encode_requires_whole_downlink_frames(self):
        """encode 是下行（我们说给设备听）—— 按 24000Hz 的 2880 字节校验。"""
        self.assertEqual(len(self.codec.encode(b"\x05" * 2880)), 2880)
        with self.assertRaises(CodecError):
            self.codec.encode(b"\x05" * 1920)

    def test_encode_rejects_empty(self):
        with self.assertRaises(CodecError):
            self.codec.encode(b"")

    def test_encode_rejects_non_bytes(self):
        with self.assertRaises(CodecError):
            self.codec.encode("not bytes")

    def test_encode_counts_packets(self):
        self.codec.encode(b"\x06" * 2880)
        self.codec.encode(b"\x07" * 2880)
        self.assertEqual(len(self.codec.encoded_packets), 2)
        self.assertEqual(self.codec.encoded_bytes, 5760)
        self.assertAlmostEqual(self.codec.sent_seconds, 0.12, places=9)

    def test_sent_seconds_is_zero_before_anything_is_sent(self):
        self.assertEqual(self.codec.sent_seconds, 0.0)

    def test_close_is_idempotent(self):
        self.codec.close()
        self.codec.close()
        self.assertTrue(self.codec.closed)

    def test_encode_after_close_raises(self):
        self.codec.close()
        with self.assertRaises(CodecError):
            self.codec.encode(b"\x08" * 2880)

    def test_decode_after_close_returns_empty_without_raising(self):
        self.codec.close()
        self.assertEqual(self.codec.decode(b"\x09" * 40), b"")
        self.assertEqual(self.codec.decode_errors, 1)


class CreateOpusCodecTest(unittest.TestCase):
    """接线点：装真依赖之前，它必须**拒绝**交出假货。"""

    def test_refuses_to_hand_out_the_fake_by_default(self):
        with self.assertRaises(CodecUnavailableError):
            create_opus_codec(_params(DEVICE_HELLO))

    def test_refusal_tells_you_what_to_do(self):
        with self.assertRaises(CodecUnavailableError) as ctx:
            create_opus_codec(_params(DEVICE_HELLO))
        message = str(ctx.exception)
        self.assertIn("allow_fake", message)
        self.assertIn("§2.2.1", message)

    def test_allow_fake_returns_a_fake_with_the_device_uplink(self):
        codec = create_opus_codec(_params(DEVICE_HELLO), allow_fake=True)
        self.assertIsInstance(codec, FakeOpusCodec)
        self.assertEqual(codec.uplink.frame_bytes, 1920)

    def test_downlink_defaults_to_24000_60(self):
        """§8.3：我们下行默认 24000。不给 downlink 就该是这个。"""
        codec = create_opus_codec(_params(DEVICE_HELLO), allow_fake=True)
        self.assertEqual(codec.downlink.sample_rate, 24000)
        self.assertEqual(codec.downlink.frame_duration, 60.0)
        self.assertEqual(codec.downlink.frame_bytes, 2880)

    def test_explicit_downlink_is_used(self):
        codec = create_opus_codec(
            _params(DEVICE_HELLO),
            AudioParams(format="opus", sample_rate=48000, channels=1, frame_duration=20),
            allow_fake=True,
        )
        self.assertEqual(codec.downlink.sample_rate, 48000)
        self.assertEqual(codec.downlink.frame_bytes, 1920)

    def test_bad_device_format_raises_even_with_fake(self):
        """设备说 format 不是 opus —— 开假货也不能装作能处理。"""
        with self.assertRaises(CodecFormatError):
            create_opus_codec(
                AudioParams(format="pcm", sample_rate=16000, channels=1, frame_duration=60),
                allow_fake=True,
            )

    def test_bad_device_rate_raises_even_with_fake(self):
        with self.assertRaises(CodecFormatError):
            create_opus_codec(
                AudioParams(format="opus", sample_rate=22050, channels=1, frame_duration=60),
                allow_fake=True,
            )


class RealFixtureTest(unittest.TestCase):
    """跟真样本对表 —— 这一层唯一的外部锚点。"""

    def test_device_hello_fixture_gives_1920_byte_frames(self):
        params = _params(DEVICE_HELLO)
        self.assertEqual(params.sample_rate, 16000)
        self.assertEqual(CodecParams.from_audio_params(params).frame_bytes, 1920)

    def test_device_hello_full_fixture_agrees(self):
        self.assertEqual(
            CodecParams.from_audio_params(_params(DEVICE_HELLO_FULL)).frame_bytes,
            CodecParams.from_audio_params(_params(DEVICE_HELLO)).frame_bytes,
        )

    def test_server_hello_24000_fixture_gives_2880_byte_frames(self):
        self.assertEqual(
            CodecParams.from_audio_params(_params(SERVER_HELLO_24000)).frame_bytes, 2880
        )

    def test_server_hello_without_frame_duration_fills_the_default(self):
        self.assertEqual(
            CodecParams.from_audio_params(_params(SERVER_HELLO_16000)).frame_bytes, 1920
        )

    def test_real_hello_drives_the_whole_offline_stack(self):
        """设备真 hello -> 建编解码器 -> 收一帧 AudioParams 说的整帧。

        「能连上」之前的整条离线链路，就靠这条串起来。
        """
        # parse_device_hello 吃的是**线上原样的 str/bytes**（它自己解 JSON）
        hello = parse_device_hello(_text(DEVICE_HELLO)).value
        self.assertIsNotNone(hello)
        codec = create_opus_codec(hello.audio_params, allow_fake=True)
        frame = bytes_per_frame(
            hello.audio_params.sample_rate,
            hello.audio_params.frame_duration,
            hello.audio_params.channels,
        )
        self.assertEqual(codec.decode(b"\x0a" * frame), b"\x0a" * frame)
        self.assertEqual(codec.decode_errors, 0)
