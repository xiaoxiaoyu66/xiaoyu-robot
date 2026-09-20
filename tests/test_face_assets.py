"""设备端表情资产的分档与命名（xiaoyu/face/assets.py）+ 出图脚本的测试。

跑法：python -m unittest discover tests
命名规则这一层看着琐碎，但它同时是**文件名**和**设备查表用的 key**：
写错一个字符不会报错，只会让设备上少一张表情、悄悄退回原样。
所以这里钉死三件事 —— 名字怎么来、名字只能长什么样、跟设计表对不对得上。

同事：test_expression.py 管参数，test_face_render.py 管像素。
"""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

from xiaoyu.face.assets import (
    FULL_STEP,
    INTENSITY_STEPS,
    NEUTRAL,
    VALID_MOODS,
    asset_name,
    asset_name_for,
    asset_specs,
    asset_table,
    nearest_step,
)
from xiaoyu.face.expression import expression_for
from scripts.build_face_assets import (
    ALPHA_DIR,
    OPAQUE_DIR,
    check,
    check_xiaozhi_repo,
    main,
    pack_command,
    parse_size,
)


class NamingTest(unittest.TestCase):
    def test_full_intensity_is_the_bare_name(self):
        """满强度不带后缀：只认裸情绪名的客户端也能拿到最典型的那张脸。
        这条是给"上游固件里那个默认表情名"留的余地，别改成 happy_10。"""
        self.assertEqual(asset_name("happy", FULL_STEP), "happy")
        self.assertEqual(asset_name_for("happy", 1.0), "happy")

    def test_other_steps_carry_a_two_digit_suffix(self):
        self.assertEqual(asset_name("happy", 0.3), "happy_03")
        self.assertEqual(asset_name("happy", 0.6), "happy_06")

    def test_neutral_has_no_intensity(self):
        """基准脸没有"更强的基准脸"（expression_for("neutral", k) 恒等于 BASE），
        所以它不该出三张一模一样的图。"""
        for step in INTENSITY_STEPS:
            with self.subTest(step=step):
                self.assertEqual(asset_name("neutral", step), NEUTRAL)

    def test_unknown_mood_becomes_neutral(self):
        for bad in ("happpy", "", None, 42, 3.5, ["happy"], {"a": 1}):
            with self.subTest(bad=bad):
                self.assertEqual(asset_name(bad), NEUTRAL)

    def test_step_not_in_the_table_falls_back_to_the_bare_name(self):
        """传 0.42 这种数说明调用方没意识到设备端只有三张图 ——
        这时候明确退到裸名字（真实存在的一张），而不是悄悄替它选一档。"""
        self.assertEqual(asset_name("happy", 0.42), "happy")
        self.assertEqual(asset_name("happy", -1.0), "happy")

    def test_names_are_safe_as_filenames(self):
        """名字会直接变成 assets 分区里的文件名（上游 packer 拿 stem 当 name），
        而且上游 config 里 name_length 是 32。所以只能短、只能是 ASCII 单词。"""
        pattern = re.compile(r"^[a-z][a-z0-9_]*$")
        names = [name for name, _mood, _step in asset_specs()]
        self.assertEqual(len(names), len(set(names)), "有重名，packer 会覆盖")
        for name in names:
            with self.subTest(name=name):
                self.assertRegex(name, pattern)
                self.assertLessEqual(len(name), 32)
                self.assertNotIn("/", name)
                self.assertNotIn("\\", name)


class NearestStepTest(unittest.TestCase):
    def test_snaps_to_the_closest_step(self):
        self.assertEqual(nearest_step(0.0), 0.3)
        self.assertEqual(nearest_step(0.5), 0.6)
        self.assertEqual(nearest_step(0.9), 1.0)

    def test_the_boundary_sits_between_the_two_steps(self):
        """0.3 和 0.6 的界在 0.45。这一条只钉两侧 ——
        正好 0.45 落在哪边由二进制浮点决定，不承诺（见 nearest_step 的 docstring）。"""
        self.assertEqual(nearest_step(0.44), 0.3)
        self.assertEqual(nearest_step(0.46), 0.6)

    def test_out_of_range_is_clamped_to_an_end(self):
        self.assertEqual(nearest_step(-5.0), 0.3)
        self.assertEqual(nearest_step(5.0), 1.0)

    def test_bad_input_gets_the_full_step(self):
        """坏强度给**满强度**那张（也就是裸名字），因为它一定存在。
        给最低档反而更糟：那几乎是一张没表情的脸，看着像坏了。"""
        for bad in (None, "", "abc", float("nan"), float("inf"), ["0.5"]):
            with self.subTest(bad=bad):
                self.assertEqual(nearest_step(bad), FULL_STEP)

    def test_client_can_go_from_intensity_straight_to_a_name(self):
        self.assertEqual(asset_name_for("angry", 0.29), "angry_03")
        self.assertEqual(asset_name_for("angry", 0.85), "angry")
        self.assertEqual(asset_name_for("nonsense", 0.9), NEUTRAL)


class AssetTableTest(unittest.TestCase):
    def test_one_asset_per_expression_plus_neutral(self):
        expected = 1 + len([m for m in VALID_MOODS if m != NEUTRAL]) * len(INTENSITY_STEPS)
        self.assertEqual(len(asset_table()), expected)

    def test_table_matches_the_naming_rules(self):
        """清单是从 asset_specs() 生成的，不是又抄了一遍规则。
        这条保证"名字 -> 哪张脸"能对上：表里每个名字都必须等于按规则推出来的名字。"""
        table = asset_table()
        specs = asset_specs()
        self.assertEqual(list(table), [name for name, _m, _s in specs])
        for name, mood, step in specs:
            with self.subTest(name=name):
                self.assertEqual(name, asset_name(mood, step))
                self.assertEqual(table[name], expression_for(mood, step))

    def test_every_mood_is_covered(self):
        moods = {mood for _n, mood, _s in asset_specs()}
        self.assertEqual(moods, set(VALID_MOODS))

    def test_intensity_order_is_weak_to_strong(self):
        """档位顺序 = 出图顺序。乱了的话预览图会把 1.0 排在 0.3 前面。"""
        self.assertEqual(list(INTENSITY_STEPS), sorted(INTENSITY_STEPS))


class BuildScriptInputTest(unittest.TestCase):
    """出图脚本里的纯逻辑部分（不写文件、不需要 Pillow）。"""

    def test_parse_size_accepts_the_usual_spellings(self):
        self.assertEqual(parse_size("240x320"), (240, 320))
        self.assertEqual(parse_size("240*320"), (240, 320))
        self.assertEqual(parse_size("240X320"), (240, 320))

    def test_parse_size_rejects_garbage(self):
        """尺寸写错必须**报错**：这一批资产会全是废的（比例不对）。
        静默退回默认值最坏 —— 看着成功了，刷进设备才发现是斜的。"""
        for bad in ("240", "240x", "axb", "0x320", "-240x320", "240x-320", ""):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    parse_size(bad)

    def test_pack_command_passes_all_three_inputs(self):
        """上游 packer 三个参数缺一不可：少了唤醒词模型或字体，
        打出来的 assets.bin 会把唤醒词和中文一起弄没。"""
        command = pack_command(
            "D:/xz", "D:/xz-assets/emoji",
            wakenet="D:/xz/wakenet", text_font="D:/xz/font.bin",
            python="py",
        )
        self.assertEqual(command[0], "py")
        self.assertIn("build.py", command[1])
        for flag in ("--wakenet_model", "--text_font", "--emoji_collection"):
            with self.subTest(flag=flag):
                self.assertIn(flag, command)
        self.assertEqual(command[-1], str(Path("D:/xz-assets/emoji")))

    def test_check_xiaozhi_repo_spots_a_wrong_path(self):
        problems = check_xiaozhi_repo(Path("D:/definitely/not/here"))
        self.assertTrue(problems)
        self.assertIn("xiaozhi-esp32", problems[0])

    def test_check_xiaozhi_repo_lists_what_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            packer = root / "scripts" / "spiffs_assets" / "build.py"
            packer.parent.mkdir(parents=True)
            packer.write_text("", encoding="utf-8")
            problems = check_xiaozhi_repo(root)
            joined = "\n".join(problems)
            self.assertIn("唤醒词模型", joined)
            self.assertIn("noto-fonts", joined)

    def test_check_reports_nothing_on_an_empty_output_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            problems = check(Path(tmp), (ALPHA_DIR,))
            self.assertTrue(problems)
            self.assertIn("manifest", problems[0])

    def test_check_flag_exits_nonzero_when_assets_are_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            code = main(["--check", "--out", tmp])
            self.assertEqual(code, 1)

    def test_bad_size_exits_with_two(self):
        with tempfile.TemporaryDirectory() as tmp:
            code = main(["--out", tmp, "--size", "nope"])
            self.assertEqual(code, 2)


class BuildScriptOutputTest(unittest.TestCase):
    """真的出一遍资产（尺寸压到 32x40，几百毫秒）。"""

    SIZE = "32x40"

    def setUp(self):
        try:
            import PIL  # noqa: F401
        except ImportError:
            self.skipTest("没装 Pillow，跳过出图")

    def _build(self, tmp: str, *extra: str) -> int:
        return main(["--out", tmp, "--size", self.SIZE, *extra])

    def test_both_variants_land_where_the_packer_expects_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self._build(tmp), 0)
            specs = asset_specs()
            for directory in (ALPHA_DIR, OPAQUE_DIR):
                for name, _mood, _step in specs:
                    with self.subTest(directory=directory, name=name):
                        path = Path(tmp) / directory / f"{name}.png"
                        self.assertTrue(path.exists(), path)
                        self.assertGreater(path.stat().st_size, 0)

    def test_manifest_records_what_was_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self._build(tmp, "--variant", ALPHA_DIR), 0)
            manifest = json.loads((Path(tmp) / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["size"], [32, 40])
            self.assertEqual(manifest["variants"], [ALPHA_DIR])
            names = [entry["name"] for entry in manifest["assets"]]
            self.assertEqual(names, [name for name, _m, _s in asset_specs()])
            self.assertEqual(manifest["total_bytes"],
                             sum(entry["bytes"] for entry in manifest["assets"]))

    def test_check_passes_right_after_a_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self._build(tmp), 0)
            self.assertEqual(check(Path(tmp), (ALPHA_DIR, OPAQUE_DIR)), [])
            self.assertEqual(main(["--check", "--out", tmp]), 0)

    def test_check_catches_a_deleted_asset(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self._build(tmp), 0)
            (Path(tmp) / ALPHA_DIR / "happy_06.png").unlink()
            problems = check(Path(tmp), (ALPHA_DIR,))
            self.assertEqual(len(problems), 1)
            self.assertIn("happy_06.png", problems[0])

    def test_blink_replaces_the_still_png(self):
        """同一个目录里不能同时有 happy.png 和 happy.gif ——
        上游 packer 拿文件名当表情名，会在 index.json 里塞两条同名记录。"""
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self._build(tmp, "--variant", ALPHA_DIR, "--blink"), 0)
            for name, _mood, _step in asset_specs():
                with self.subTest(name=name):
                    self.assertTrue((Path(tmp) / ALPHA_DIR / f"{name}.gif").exists())
                    self.assertFalse((Path(tmp) / ALPHA_DIR / f"{name}.png").exists())

    def test_blink_manifest_points_at_the_gif(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self._build(tmp, "--variant", ALPHA_DIR, "--blink"), 0)
            manifest = json.loads((Path(tmp) / "manifest.json").read_text(encoding="utf-8"))
            self.assertTrue(manifest["blink"])
            for entry in manifest["assets"]:
                with self.subTest(name=entry["name"]):
                    self.assertTrue(entry["file"].endswith(".gif"))

    def test_preview_sheet_is_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self._build(tmp, "--variant", ALPHA_DIR, "--preview"), 0)
            preview = Path(tmp) / "preview.png"
            self.assertTrue(preview.exists())
            # 5 个情绪行（其中 neutral 一行只有一格）+ 表头，总比一屏高
            self.assertGreater(preview.stat().st_size, 1000)


if __name__ == "__main__":
    unittest.main()
