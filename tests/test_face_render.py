"""表情渲染（xiaoyu/face/render.py）的测试 —— 真的画图，然后数像素。

跑法：python -m unittest discover tests
要 Pillow（没装就整类跳过）；不碰硬件、不起浏览器。

为什么要数像素而不是"能跑通就行"：这一层是**设备端资产的出品车间**，
它错了没有任何东西会报错 —— 出来就是一张看着不对劲的图，要刷进板子才发现。
「瞳孔跟着 pupil_x 动了没」「上眼皮盖下来没有」这类事，只有看像素才问得出来。
"""

from __future__ import annotations

import unittest

from xiaoyu.face.assets import asset_table
from xiaoyu.face.expression import BASE, MOOD_COLORS, MOUTHS, Expression

try:
    from PIL import Image
except ImportError:                                     # pragma: no cover
    Image = None

from xiaoyu.face.render import (
    BLINK_FRAME_MS,
    BLINK_HOLD_FRAMES,
    EYE,
    EYE_CX,
    EYE_CY,
    EYE_H,
    EYE_W,
    PANEL,
    STRIPE_BOX,
    _lid_edge_y,
    blink_frames,
    blink_loop,
    render_face,
)

_SKIP = "没装 Pillow（py -3.11 -m pip install Pillow），跳过渲染测试"

# 两只眼睛都在里面的区域，用来数"有多少眼白" / 找瞳孔重心
EYE_REGION = (50, 100, 190, 200)

# 眼睛中间那条横带：宽 = 整个眼宽，高取 12（小于圆角半径 23 的一半以上，
# 留足余量）—— 整条带一定被眼白盖满，所以带里偏暗的只可能是瞳孔。
_BAND = (EYE_CX[0] - EYE_W / 2, EYE_CY - 12, EYE_CX[0] + EYE_W / 2, EYE_CY + 12)
# 眼睛中间那条竖带：同理，用来看瞳孔的上下移动
_COLUMN = (EYE_CX[0] - 12, EYE_CY - EYE_H / 2, EYE_CX[0] + 12, EYE_CY + EYE_H / 2)

# 底部那条情绪色条（用它的中心当采样点）
STRIPE_CENTER = (int((STRIPE_BOX[0] + STRIPE_BOX[2]) / 2), int((STRIPE_BOX[1] + STRIPE_BOX[3]) / 2))


def _scan(image, box, predicate):
    """把 box 里的像素过一遍，返回命中的坐标列表。

    用 image.load() 而不是 getdata()：后者在 Pillow 12.3 起已经
    弃用（Pillow 14 会删掉），而这条测试要活很久。
    """
    left, top, right, bottom = (int(v) for v in box)
    pixels = image.load()
    return [
        (x, y)
        for y in range(top, bottom)
        for x in range(left, right)
        if predicate(pixels[x, y])
    ]


def _is_white(pixel) -> bool:
    r, g, b, a = pixel
    return a > 200 and min(r, g, b) > 200


def _is_dark(pixel) -> bool:
    r, g, b, a = pixel
    return a > 200 and max(r, g, b) < 80


def _is_ink(pixel) -> bool:
    """和面板底色差得明显的像素。

    嘴得用这个而不是 _is_white：soft 那条浅弧是按 43% 透明度画的，
    点出来是灰的 —— 用"亮不亮"去判会得出"这张脸没有嘴"的错误结论。
    """
    r, g, b, a = pixel
    return a > 200 and abs(r - PANEL[0]) + abs(g - PANEL[1]) + abs(b - PANEL[2]) > 30


def _bright_count(image, box=EYE_REGION) -> int:
    """数"眼白"那种亮像素。眼皮盖下来 / 眨眼时这个数会掉。"""
    return len(_scan(image, box, _is_white))


def _dark_count(image, box) -> int:
    return len(_scan(image, box, _is_dark))


def _ink_count(image, box) -> int:
    return len(_scan(image, box, _is_ink))


def _dark_centroid(image, box):
    """box 里偏暗（瞳孔 / 面板色）像素的重心 x, y。没有就返回 None。"""
    points = _scan(image, box, _is_dark)
    if not points:
        return None
    return (
        sum(x for x, _y in points) / len(points),
        sum(y for _x, y in points) / len(points),
    )


@unittest.skipIf(Image is None, _SKIP)
class RenderFaceTest(unittest.TestCase):
    def test_default_size_is_the_device_screen(self):
        image = render_face(BASE)
        self.assertEqual(image.size, (240, 320))
        self.assertEqual(image.mode, "RGBA")

    def test_size_is_configurable(self):
        self.assertEqual(render_face(BASE, size=(120, 160)).size, (120, 160))

    def test_invalid_size_raises(self):
        """尺寸错了要早失败：等着看一张 0 像素的图没有任何意义。"""
        for bad in ((0, 320), (240, 0), (-240, 320)):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    render_face(BASE, size=bad)

    def test_transparent_variant_leaves_the_corners_empty(self):
        image = render_face(BASE)
        for corner in ((0, 0), (239, 0), (0, 319), (239, 319)):
            with self.subTest(corner=corner):
                self.assertEqual(image.getpixel(corner)[3], 0)

    def test_opaque_variant_fills_every_pixel(self):
        image = render_face(BASE, opaque=True)
        for corner in ((0, 0), (239, 0), (0, 319), (239, 319)):
            with self.subTest(corner=corner):
                self.assertEqual(image.getpixel(corner)[3], 255)
                self.assertEqual(image.getpixel(corner)[:3], PANEL)

    def test_panel_uses_the_web_face_color(self):
        """脸上那块底色和 PC 上的 PANEL 必须是同一个数
        （PaletteMatchesWebFaceTest 另外钉了 index.html 那一侧）。"""
        self.assertEqual(render_face(BASE).getpixel((30, 60))[:3], PANEL)

    def test_eye_is_white_around_a_dark_pupil(self):
        image = render_face(BASE)
        above_pupil = image.getpixel((int(EYE_CX[0]), EYE_CY - 20))
        self.assertEqual(above_pupil[:3], EYE)
        self.assertEqual(image.getpixel((int(EYE_CX[0]), EYE_CY))[:3], PANEL)

    def test_pupil_follows_pupil_x(self):
        """按正 x 看 = 往屏幕右边看。反了的话"看着你"会变成"瞟着别人"。"""
        center = _dark_centroid(render_face(Expression()), _BAND)
        right = _dark_centroid(render_face(Expression(pupil_x=1.0)), _BAND)
        left = _dark_centroid(render_face(Expression(pupil_x=-1.0)), _BAND)
        for point in (center, right, left):
            self.assertIsNotNone(point, "眼睛中间应该有瞳孔")
        self.assertGreater(right[0], center[0])
        self.assertLess(left[0], center[0])

    def test_pupil_follows_pupil_y(self):
        """正 y = 往下看（屏幕坐标往下是正）。难过时视线下垂用的就是这个。"""
        up = _dark_centroid(render_face(Expression(pupil_y=-1.0)), _COLUMN)
        down = _dark_centroid(render_face(Expression(pupil_y=1.0)), _COLUMN)
        self.assertLess(up[1], down[1])

    def test_pupil_is_never_clipped_by_the_eye(self):
        """喂满偏移时瞳孔必须**整个**还在眼里。渲染器靠"留一个瞳孔半径的余量"
        做到这一点 —— 去掉余量不会报错，只会让瞳孔被眼框切掉半个，
        表现是"一往边上看，眼睛就变成月牙"。

        量的是瞳孔的暗像素面积：全在眼里时它跟偏移无关（上下被采样带切掉是
        对称的），被切掉就是几十个百分点的落差。留 5% 给抗锯齿的颗粒。
        """
        counts = [
            _dark_count(render_face(Expression(pupil_x=offset)), _BAND)
            for offset in (-1.0, -0.5, 0.0, 0.5, 1.0)
        ]
        self.assertTrue(all(counts), "眼睛中间应该有瞳孔")
        self.assertLessEqual(
            max(counts) - min(counts), max(counts) * 0.05,
            f"瞳孔被眼框切了：{counts}",
        )

    def test_eye_shape_makes_the_eye_round(self):
        """eye_shape 往 1 走 = 从竖长的豆眼收成正圆（惊讶用）。"""
        tall = _bright_count(render_face(Expression()))
        round_eye = _bright_count(render_face(Expression(eye_shape=1.0)))
        self.assertLess(round_eye, tall)

    def test_lids_cover_the_eye(self):
        full = _bright_count(render_face(Expression()))
        half = _bright_count(render_face(Expression(lid_top=0.5)))
        shut = _bright_count(render_face(Expression(lid_top=1.0)))
        self.assertLess(half, full)
        self.assertLess(shut, half)
        self.assertEqual(shut, 0, "眼皮全盖下来时不该还剩眼白")

    def test_bottom_lid_makes_the_crescent_eye(self):
        """开心那张脸的"弯月眼"就是下眼皮盖上来 —— 上面盖不出这个效果。"""
        full = _bright_count(render_face(Expression()))
        crescent = _bright_count(render_face(Expression(lid_bottom=0.58)))
        self.assertLess(crescent, full)

    def test_stripe_is_the_mood_color(self):
        image = render_face(Expression(stripe_level=1.0), mood="happy")
        self.assertEqual(image.getpixel(STRIPE_CENTER)[:3], MOOD_COLORS["happy"])

    def test_stripe_disappears_at_zero_level(self):
        image = render_face(Expression(stripe_level=0.0), mood="happy")
        self.assertEqual(image.getpixel(STRIPE_CENTER)[:3], PANEL)

    def test_mood_only_changes_the_stripe(self):
        """情绪色**只**走底部那条色条。这条钉的是"配色不许溢出去"：
        哪天有人给情绪加了别的视觉出口（比如脸本身变色），会发现这里挂了，
        然后必须明确决定"要不要让设备端也多一个出口"。"""
        expression = Expression(stripe_level=1.0)
        happy = render_face(expression, mood="happy")
        angry = render_face(expression, mood="angry")
        width, height = happy.size
        # 色条那一条横带以外，两个情绪必须像素级一样（各留 3 像素给抗锯齿）
        for box in (
            (0, 0, width, int(STRIPE_BOX[1]) - 3),
            (0, int(STRIPE_BOX[3]) + 4, width, height),
        ):
            with self.subTest(box=box):
                self.assertEqual(happy.crop(box).tobytes(), angry.crop(box).tobytes())

    def test_same_input_renders_identical_pixels(self):
        """出资产要可复现：同一张脸两次出图必须一个字节不差，
        否则"上周刷进去的图和今天出的不一样"会变成查不出来的悬案。"""
        expression = Expression(lid_top=0.3, pupil_x=0.4)
        first = render_face(expression, mood="sad")
        second = render_face(expression, mood="sad")
        self.assertEqual(first.tobytes(), second.tobytes())

    def test_every_shipped_asset_renders(self):
        """出的每一张都得画得出来 —— 这是最省事的"整张表扫一遍"。"""
        for name, expression in asset_table().items():
            mood = name.split("_")[0] if "_" in name else name
            with self.subTest(name=name):
                image = render_face(expression, mood=mood)
                self.assertEqual(image.size, (240, 320))
                self.assertGreater(_bright_count(image), 0, "这张脸上没有眼睛？")

    def test_unhashable_mood_does_not_raise(self):
        for bad in (["happy"], {"mood": "happy"}, None, 42):
            with self.subTest(bad=bad):
                self.assertEqual(render_face(BASE, mood=bad).size, (240, 320))


@unittest.skipIf(Image is None, _SKIP)
class MouthTest(unittest.TestCase):
    def test_every_known_mouth_renders(self):
        """MOUTHS 是参数层和渲染层之间的方言表：名字对不上不会报错，
        渲染器会安静地退回 soft（一张脸永远不换嘴型）。"""
        for mouth in MOUTHS:
            with self.subTest(mouth=mouth):
                image = render_face(Expression(mouth=mouth, mouth_scale=1.0))
                self.assertGreater(_ink_count(image, (80, 210, 160, 260)), 0)

    def test_mouths_look_different_from_each_other(self):
        seen: dict[bytes, str] = {}
        for mouth in MOUTHS:
            image = render_face(Expression(mouth=mouth, mouth_scale=1.0))
            mouth_area = image.crop((80, 210, 160, 260)).tobytes()
            with self.subTest(mouth=mouth):
                self.assertNotIn(mouth_area, seen, f"{mouth} 和 {seen.get(mouth_area)} 长得一样")
            seen[mouth_area] = mouth

    def test_unknown_mouth_falls_back_instead_of_raising(self):
        """嘴型名字写错了只是画得不像，绝不能让整张脸出不来。"""
        image = render_face(Expression(mouth="definitely-not-a-mouth"))
        self.assertEqual(image.size, (240, 320))
        self.assertGreater(_ink_count(image, (80, 210, 160, 260)), 0)


@unittest.skipIf(Image is None, _SKIP)
class LidGeometryTest(unittest.TestCase):
    """眼皮倾角的符号约定。反了的话生气会看着像难过，而且不会报错。"""

    def test_positive_tilt_squeezes_the_inner_corner_from_above(self):
        """正 = 内低外高（锐眼 / 生气）：上眼皮内侧压得更低。"""
        inner = _lid_edge_y(120.0, 0.5, 80.0, inner=True, top=True)
        outer = _lid_edge_y(120.0, 0.5, 80.0, inner=False, top=True)
        self.assertGreater(inner, outer, "y 越大越低")

    def test_positive_tilt_raises_the_inner_corner_from_below(self):
        """同一个正倾角，下眼皮内侧抬得更高 —— 上下一起收，剩一条往内收的斜缝。"""
        inner = _lid_edge_y(120.0, 0.5, 80.0, inner=True, top=False)
        outer = _lid_edge_y(120.0, 0.5, 80.0, inner=False, top=False)
        self.assertLess(inner, outer)

    def test_negative_tilt_droops_outwards(self):
        """负 = 外低内高（垂眼 / 难过）：上眼皮外侧垂下来。"""
        inner = _lid_edge_y(120.0, -0.5, 80.0, inner=True, top=True)
        outer = _lid_edge_y(120.0, -0.5, 80.0, inner=False, top=True)
        self.assertLess(inner, outer)

    def test_zero_tilt_is_flat(self):
        self.assertEqual(
            _lid_edge_y(120.0, 0.0, 80.0, inner=True, top=True),
            _lid_edge_y(120.0, 0.0, 80.0, inner=False, top=True),
        )


@unittest.skipIf(Image is None, _SKIP)
class BlinkFramesTest(unittest.TestCase):
    def test_first_and_last_frames_are_the_resting_face(self):
        """三角波的 0 -> 1 -> 0：头和尾都必须是睁着的，
        否则 GIF 会从"半闭"开始、循环处跳一下。"""
        frames = blink_frames(BASE, frames=5)
        resting = render_face(BASE).tobytes()
        self.assertEqual(frames[0].tobytes(), resting)
        self.assertEqual(frames[-1].tobytes(), resting)

    def test_middle_frame_is_closed(self):
        frames = blink_frames(BASE, frames=5)
        self.assertLess(_bright_count(frames[2]), _bright_count(frames[0]))
        self.assertEqual(_bright_count(frames[2]), 0)

    def test_frame_count_and_size(self):
        frames = blink_frames(BASE, frames=7, size=(60, 80))
        self.assertEqual(len(frames), 7)
        for frame in frames:
            with self.subTest(size=frame.size):
                self.assertEqual(frame.size, (60, 80))

    def test_one_frame_is_a_mistake(self):
        with self.assertRaises(ValueError):
            blink_frames(BASE, frames=1)

    def test_opaque_variant_is_passed_through(self):
        frame = blink_frames(BASE, frames=3, opaque=True)[0]
        self.assertEqual(frame.getpixel((0, 0))[3], 255)


@unittest.skipIf(Image is None, _SKIP)
class BlinkLoopTest(unittest.TestCase):
    def test_loop_is_hold_plus_blink(self):
        frames, ms = blink_loop(BASE, frames=5, hold_frames=4)
        self.assertEqual(len(frames), 9)
        self.assertEqual(ms, BLINK_FRAME_MS)

    def test_default_nods_at_a_readable_pace(self):
        """默认节奏：睁着 3.2 秒、眨 0.5 秒、总循环一秒多一点以上。
        这两个数是手感，但"总时长"太短会变抽搐 —— 留个下限。"""
        frames, ms = blink_loop(BASE)
        self.assertGreaterEqual(len(frames) * ms, 2000)

    def test_first_frame_is_the_resting_face(self):
        frames, _ms = blink_loop(BASE, hold_frames=2, frames=5)
        self.assertEqual(frames[0].tobytes(), render_face(BASE).tobytes())

    def test_third_from_last_is_the_fully_closed_frame(self):
        """出预览图时取 frames[-3] 当"闭眼"那一格（blink_frames 是
        0, 0.5, 1, 0.5, 0，所以倒数第三个正好是完全闭上）。这里把它钉住，
        免得改了 blink_frames 的帧数之后预览图安静地开始骗人。"""
        frames, _ms = blink_loop(BASE, hold_frames=3, frames=5)
        brightest = _bright_count(frames[0])
        self.assertEqual(_bright_count(frames[-3]), 0)
        self.assertGreater(brightest, 0)
        # 睁着不动的那几帧 + 眨之前 t=0 那一帧 + 收尾那一帧，像素必须完全一样
        for index in list(range(4)) + [len(frames) - 1]:
            with self.subTest(index=index):
                self.assertEqual(
                    _bright_count(frames[index]), brightest, "睁着的那几帧应该一模一样"
                )

    def test_zero_hold_still_blinks(self):
        frames, _ms = blink_loop(BASE, hold_frames=0, frames=5)
        self.assertEqual(len(frames), 5)
        self.assertEqual(_bright_count(frames[2]), 0)

    def test_bad_arguments_raise(self):
        with self.assertRaises(ValueError):
            blink_loop(BASE, hold_frames=-1)
        with self.assertRaises(ValueError):
            blink_loop(BASE, frame_ms=0)
        with self.assertRaises(ValueError):
            blink_loop(BASE, frames=1)

    def test_hold_frames_are_the_same_image(self):
        """睁着的几帧必须是同一张图（不是重画几遍）——
        37 帧里 32 帧一样，GIF 才能压到几 KB。"""
        frames, _ms = blink_loop(BASE, hold_frames=4, frames=3)
        for frame in frames[:4]:
            self.assertEqual(frame.tobytes(), frames[0].tobytes())

    def test_default_hold_is_long_enough_to_look_alive(self):
        self.assertGreater(BLINK_HOLD_FRAMES * BLINK_FRAME_MS, 2000)


@unittest.skipIf(Image is None, _SKIP)
class ContactSheetTest(unittest.TestCase):
    """出图脚本里的预览拼图。只给眼睛看，但它错了会让人照着一张假图做决定。"""

    def setUp(self):
        from scripts.build_face_assets import contact_sheet

        self.contact_sheet = contact_sheet
        self.cell = render_face(BASE, size=(24, 32))

    def test_grid_size_follows_rows_and_columns(self):
        sheet = self.contact_sheet(
            [("a", [self.cell, self.cell]), ("b", [self.cell, self.cell])],
            cell_size=(24, 32), label_width=100, gap=8,
        )
        self.assertEqual(sheet.size, (100 + 2 * 32 + 8, 2 * 40 + 8))

    def test_title_adds_a_header(self):
        without = self.contact_sheet([("a", [self.cell])], cell_size=(24, 32), gap=8)
        with_title = self.contact_sheet(
            [("a", [self.cell])], cell_size=(24, 32), gap=8, title="hello",
        )
        self.assertEqual(with_title.size[1] - without.size[1], 40)

    def test_empty_rows_raise(self):
        with self.assertRaises(ValueError):
            self.contact_sheet([], cell_size=(24, 32))

    def test_transparent_cells_keep_the_sheet_background(self):
        """透明底那版要能看得见（用图的 alpha 当遮罩贴），
        否则 240x320 的圆角脸会在预览图上糊成一个黑方块。"""
        sheet = self.contact_sheet([("a", [self.cell])], cell_size=(24, 32), label_width=0, gap=0)
        self.assertNotEqual(sheet.getpixel((0, 0)), (0, 0, 0))


if __name__ == "__main__":
    unittest.main()
