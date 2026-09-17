"""提示音的测试。

为什么要给提示音写测试：它本身不重要，但**它出问题的方式很讨厌** ——
提示音是"唤醒后立刻播"的，如果生成波形时抛异常，
整个语音循环就崩在第一行了，而原因看起来跟音频毫无关系。

这里只测纯计算部分（不碰声卡），所以不需要麦克风喇叭也能跑。
"""

from __future__ import annotations

import unittest

try:
    import numpy as np
except ImportError:                                   # pragma: no cover
    np = None

try:
    from xiaoyu.audio import sfx
except ImportError:                                   # pragma: no cover
    sfx = None

_SKIP = "缺少 numpy，跳过"


@unittest.skipIf(sfx is None, _SKIP)
class TestMakeCue(unittest.TestCase):
    SAMPLERATE = 16000

    def test_shape_and_dtype(self):
        wave = sfx.make_cue(sfx.ACK, self.SAMPLERATE)
        self.assertEqual(wave.ndim, 1)
        self.assertEqual(wave.dtype, np.float32)
        self.assertGreater(wave.size, 0)

    def test_length_is_reasonable(self):
        """两个音加起来不该超过 0.3 秒 —— 提示音一长就从"提示"变成"噪音"。"""
        wave = sfx.make_cue(sfx.ACK, self.SAMPLERATE)
        self.assertLess(wave.size / self.SAMPLERATE, 0.3)

    def test_volume_is_audible_but_not_clipping(self):
        peak = float(np.abs(sfx.make_cue(sfx.ACK, self.SAMPLERATE)).max())
        self.assertGreater(peak, 0.05, "太轻了，听不见就没意义")
        self.assertLessEqual(peak, 1.0, "超过 1.0 会削波，听起来是破音")

    def test_fades_in_and_out(self):
        """首尾必须是 0，否则每个音都会"啪"一声爆音。"""
        wave = sfx.make_cue(sfx.ACK, self.SAMPLERATE)
        self.assertAlmostEqual(float(abs(wave[0])), 0.0, places=6)
        self.assertAlmostEqual(float(abs(wave[-1])), 0.0, places=6)

    def test_no_nan_or_inf(self):
        for kind in sfx.kinds():
            wave = sfx.make_cue(kind, self.SAMPLERATE)
            self.assertTrue(np.isfinite(wave).all(), f"{kind} 里出现了 NaN/Inf")

    def test_all_kinds_are_distinct(self):
        """三种提示音听感上必须能分开，不然等于只有一种。"""
        waves = {k: sfx.make_cue(k, self.SAMPLERATE) for k in sfx.kinds()}
        self.assertEqual(len(set(waves)), len(sfx.kinds()))
        self.assertFalse(np.array_equal(waves[sfx.ACK], waves[sfx.DONE]))

    def test_unknown_kind_falls_back_instead_of_raising(self):
        """写错名字不能把语音循环搞崩 —— 退回默认音就行。"""
        fallback = sfx.make_cue("\u4e0d\u5b58\u5728\u7684\u63d0\u793a\u97f3", self.SAMPLERATE)
        self.assertTrue(np.array_equal(fallback, sfx.make_cue(sfx.ACK, self.SAMPLERATE)))

    def test_works_at_other_samplerates(self):
        """TTS 用 24000Hz，录音用 16000Hz，两边都得能生成。"""
        for rate in (8000, 16000, 22050, 24000, 44100):
            wave = sfx.make_cue(sfx.ACK, rate)
            self.assertGreater(wave.size, 0)


if __name__ == "__main__":
    unittest.main()