"""表情参数层（xiaoyu/face/expression.py）的测试。

跑法：python -m unittest discover tests
纯离线：不碰 Pillow、不碰硬件、不起浏览器。
这一层是**两份渲染实现共用的参数出处**（设备端出图、PC 上那张 Canvas 脸），
它错了两边一起错，所以钉得比一般模块细。

同事：test_face_render.py 管像素，test_face_assets.py 管命名和出图脚本。
"""

from __future__ import annotations

import math
import re
import unittest

from xiaoyu.config import PROJECT_ROOT
from xiaoyu.face import protocol as face_protocol
from xiaoyu.face.expression import (
    BASE,
    MOOD_COLORS,
    MOUTHS,
    VALID_MOODS,
    expression_for,
    mood_color,
)
from xiaoyu.llm.emotion import VALID_MOODS as LLM_VALID_MOODS

# 每个字段的合法区间。渲染器**不再夹一遍**（它信这一层），所以这里是唯一的岗。
# None = 没有上界（eye_scale 会大于 1：惊讶时眼睛放大 15%）。
_BOUNDS: dict[str, tuple[float, float | None]] = {
    "eye_shape": (-1.0, 1.0),
    "eye_scale": (0.0, None),
    "lid_top": (0.0, 1.0),
    "lid_bottom": (0.0, 1.0),
    "lid_tilt": (-1.0, 1.0),
    "pupil_x": (-1.0, 1.0),
    "pupil_y": (-1.0, 1.0),
    "brow_angle": (-1.0, 1.0),
    "brow_lift": (-1.0, 1.0),
    "mouth_scale": (0.0, 1.0),
    "blush": (0.0, 1.0),
    "stripe_level": (0.0, 1.0),
}

def _numeric(expression) -> dict[str, float]:
    return {
        name: getattr(expression, name)
        for name in _BOUNDS
    }


class MoodTableTest(unittest.TestCase):
    """情绪名单和配色。三处名单必须永远是同一份。"""

    def test_moods_match_the_other_two_modules(self):
        """expression / face.protocol / llm.emotion 三处各写了一遍 VALID_MOODS。

        写三遍是有意的（各自的模块不想反向依赖对方），代价是**必须对得上**：
        模型吐 "angry"、协议层原样转发、参数层却不认识它 —— 表现就是"聊得好好的
        但脸不跟着变"。这条用例就是给这个代价上的保险。
        """
        self.assertEqual(set(VALID_MOODS), set(face_protocol.VALID_MOODS))
        self.assertEqual(set(VALID_MOODS), set(LLM_VALID_MOODS))

    def test_mood_colors_cover_every_mood(self):
        self.assertEqual(set(MOOD_COLORS), set(VALID_MOODS))
        for mood, rgb in MOOD_COLORS.items():
            with self.subTest(mood=mood):
                self.assertEqual(len(rgb), 3)
                for channel in rgb:
                    self.assertIsInstance(channel, int)
                    self.assertTrue(0 <= channel <= 255)

    def test_mouth_names_are_the_renderer_dialect(self):
        """嘴型是字符串枚举。BASE 和所有目标脸的嘴都得在这张表里 ——
        写错一个字母渲染器不会报错，它会安静地退回 soft（一张脸永远不换嘴型）。"""
        self.assertIn(BASE.mouth, MOUTHS)
        for mood in VALID_MOODS:
            with self.subTest(mood=mood):
                self.assertIn(expression_for(mood, 1.0).mouth, MOUTHS)


class MoodColorTest(unittest.TestCase):
    def test_known_mood_gets_its_own_color(self):
        self.assertEqual(mood_color("angry"), MOOD_COLORS["angry"])

    def test_unknown_mood_falls_back_to_grey(self):
        self.assertEqual(mood_color("what"), MOOD_COLORS["neutral"])
        self.assertEqual(mood_color(None), MOOD_COLORS["neutral"])

    def test_unhashable_mood_does_not_raise(self):
        """mood 是从 JSON 来的，理论上可能是个 list/dict。
        dict.get(不可哈希) 会抛 TypeError —— 一张脸不该因为配色崩掉。"""
        for bad in (["happy"], {"mood": "happy"}, 3.5):
            with self.subTest(bad=bad):
                self.assertEqual(mood_color(bad), MOOD_COLORS["neutral"])


class ExpressionForTest(unittest.TestCase):
    def test_zero_intensity_is_the_base_face_for_every_mood(self):
        for mood in VALID_MOODS:
            with self.subTest(mood=mood):
                self.assertEqual(expression_for(mood, 0.0), BASE)

    def test_full_intensity_changes_the_face(self):
        """每个情绪在满强度下都必须**真的长得不一样** ——
        全都等于基准脸的话，出资产会出一堆一模一样的图，而且不报错。"""
        for mood in VALID_MOODS:
            if mood == "neutral":
                continue
            with self.subTest(mood=mood):
                self.assertNotEqual(expression_for(mood, 1.0), BASE)

    def test_neutral_is_the_base_face_at_any_intensity(self):
        """neutral 没有"更强的 neutral" —— 它自己就是基准。
        出资产时这也意味着 neutral 只需要一张图（见 assets.asset_specs）。"""
        for intensity in (0.0, 0.4, 1.0, 5.0):
            with self.subTest(intensity=intensity):
                self.assertEqual(expression_for("neutral", intensity), BASE)

    def test_half_intensity_is_the_midpoint(self):
        """强度是"从基准脸到目标脸的插值比例"，不是各字段自己的一套曲线。"""
        happy = expression_for("happy", 1.0)
        half = expression_for("happy", 0.5)
        self.assertAlmostEqual(half.lid_bottom, happy.lid_bottom / 2)
        self.assertAlmostEqual(half.blush, happy.blush / 2)
        self.assertAlmostEqual(half.mouth_scale, (BASE.mouth_scale + happy.mouth_scale) / 2)

    def test_unknown_mood_falls_back_to_base(self):
        for bad in ("happpy", "HAPPY", "", None, 42, 3.5, ["happy"], {"happy": 1}):
            with self.subTest(bad=bad):
                self.assertEqual(expression_for(bad), BASE)

    def test_bad_intensity_does_not_raise(self):
        for bad in (None, "abc", "", float("nan"), float("inf"), -float("inf"), object()):
            with self.subTest(bad=bad):
                self.assertEqual(expression_for("happy", bad), BASE)

    def test_intensity_is_clamped(self):
        full = expression_for("angry", 1.0)
        self.assertEqual(expression_for("angry", 9.0), full)
        self.assertEqual(expression_for("angry", -3.0), BASE)

    def test_mouth_is_discrete_not_interpolated(self):
        """嘴型是字符串，插不了值。强度一过阈值就换成目标嘴型，
        大小交给 mouth_scale —— 所以 (angry, 0.25) 是"一张很小的撇嘴"。"""
        self.assertEqual(expression_for("angry", 0.0).mouth, BASE.mouth)
        self.assertEqual(expression_for("angry", 0.5).mouth, expression_for("angry", 1.0).mouth)
        self.assertNotEqual(expression_for("angry", 0.5).mouth, BASE.mouth)

    def test_low_intensity_angry_is_the_design_sheet_sulk(self):
        """设计稿里那张"不爽"就是低强度生气，不是单独一个情绪。

        这条把"白赚一个表情"这件事钉在测试里：眼睛压下来一点点、眉毛内低外高
        一点点、嘴换成撇嘴 —— 三样都得**朝生气的方向走**，只是走得少。
        """
        sulk = expression_for("angry", 0.25)
        full = expression_for("angry", 1.0)
        self.assertGreater(sulk.lid_top, 0.0)
        self.assertLess(sulk.lid_top, full.lid_top)
        self.assertGreater(sulk.brow_angle, 0.0)
        self.assertLess(sulk.brow_angle, full.brow_angle)
        self.assertEqual(sulk.mouth, "frown")
        self.assertLess(sulk.mouth_scale, full.mouth_scale)

    def test_lid_tilt_sign_says_which_eye(self):
        """符号约定（渲染器按这个画，别改）：正 = 内低外高（锐眼/生气），
        负 = 外低内高（垂眼/难过）。反了的话生气会看着像难过。"""
        self.assertGreater(expression_for("angry", 1.0).lid_tilt, 0.0)
        self.assertLess(expression_for("sad", 1.0).lid_tilt, 0.0)

    def test_sad_looks_down_and_happy_does_not(self):
        """难过时视线往下（正 y = 屏幕往下），开心时微微朝上。"""
        self.assertGreater(expression_for("sad", 1.0).pupil_y, 0.0)
        self.assertLess(expression_for("happy", 1.0).pupil_y, 0.0)

    def test_values_stay_inside_the_documented_bounds(self):
        """渲染器**不夹**这些数（它信这一层）。所以每一档都得在界内 ——
        越界了 Pillow 不会报错，只会画出一张眼睛飞出脸外的图。"""
        moods = list(VALID_MOODS) + ["nonsense", None]
        for mood in moods:
            for intensity in (0.0, 0.13, 0.5, 0.87, 1.0, 2.0, -1.0, float("nan")):
                with self.subTest(mood=mood, intensity=intensity):
                    self._assert_bounded(expression_for(mood, intensity))

    def _assert_bounded(self, expression) -> None:
        for name, value in _numeric(expression).items():
            low, high = _BOUNDS[name]
            self.assertTrue(math.isfinite(value), f"{name} 不是有限数：{value}")
            self.assertGreaterEqual(value, low, f"{name}={value} 小于 {low}")
            if high is not None:
                self.assertLessEqual(value, high, f"{name}={value} 大于 {high}")

    def test_result_is_frozen(self):
        """返回值是 frozen dataclass：渲染器拿到手就只能读。
        能被改写的话，blink_frames 那种"复制一份改一个字段"的玩法会互相干扰。"""
        expression = expression_for("happy", 1.0)
        with self.assertRaises(Exception):
            expression.lid_top = 0.9        # type: ignore[misc]


class PaletteMatchesWebFaceTest(unittest.TestCase):
    """PC 那张脸（face/index.html）和这里必须是同一套颜色。

    这两份渲染实现共用一套配色是**有意的重复**（一份参数出处，两份画法）：
    设备端屏幕上那条情绪色条、PC 上那圈光环，看着得像同一个情绪。
    改了一边忘了另一边，表现是"平板上的生气和板子上的生气不是一个颜色"。
    """

    HTML = PROJECT_ROOT / "face" / "index.html"
    _HEX_RE = re.compile(r'const (PANEL|EYE) = "#([0-9A-Fa-f]{6})"')
    _HALO_RE = re.compile(r"(happy|sad|angry|surprised):\s*\"rgba\((\d+),(\d+),(\d+),")

    def setUp(self):
        if not self.HTML.exists():
            self.skipTest(f"没有 {self.HTML}")
        self.text = self.HTML.read_text(encoding="utf-8")

    def test_panel_and_eye_colors_match_the_renderer(self):
        from xiaoyu.face.render import EYE, PANEL

        found = {name: self._hex_to_rgb(value) for name, value in self._HEX_RE.findall(self.text)}
        self.assertEqual(found.get("PANEL"), PANEL, "face/index.html 的 PANEL 变了？")
        self.assertEqual(found.get("EYE"), EYE, "face/index.html 的 EYE 变了？")

    def test_halo_colors_match_the_mood_palette(self):
        found = {
            mood: (int(r), int(g), int(b))
            for mood, r, g, b in self._HALO_RE.findall(self.text)
        }
        self.assertTrue(found, "没在 index.html 里找到 HALO 表，正则要跟着改")
        for mood, rgb in found.items():
            with self.subTest(mood=mood):
                self.assertEqual(rgb, MOOD_COLORS[mood])

    @staticmethod
    def _hex_to_rgb(value: str) -> tuple[int, int, int]:
        return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))


class BaseFaceTest(unittest.TestCase):
    def test_base_is_a_neutral_expression(self):
        """基准脸的定义：不表达情绪，但看得出是张脸。
        所以它不该有眼皮、腮红、眉毛，嘴是那条最浅的 soft。"""
        self.assertEqual(BASE.lid_top, 0.0)
        self.assertEqual(BASE.lid_bottom, 0.0)
        self.assertEqual(BASE.lid_tilt, 0.0)
        self.assertEqual(BASE.brow_angle, 0.0)
        self.assertEqual(BASE.brow_lift, 0.0)
        self.assertEqual(BASE.blush, 0.0)
        self.assertEqual(BASE.mouth, "soft")


if __name__ == "__main__":
    unittest.main()
