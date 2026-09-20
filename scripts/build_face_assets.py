"""出设备端表情资产：Expression -> PNG / GIF（+ 眨眼循环、预览图、清单）。

用法（在项目根目录下跑）：
    py -3.11 scripts\\build_face_assets.py                      # 出到 build/face_assets/
    py -3.11 scripts\\build_face_assets.py --preview            # 顺手出一张外观检查图
    py -3.11 scripts\\build_face_assets.py --blink              # 出会眨眼的 GIF 版
    py -3.11 scripts\\build_face_assets.py --check              # 只核对磁盘上齐没齐
    py -3.11 scripts\\build_face_assets.py --pack <xiaozhi仓库>  # 看打包成 assets.bin 的命令

产物（默认 build/face_assets/）：
    emoji/<名字>.png          透明底：圆角面板 + 四周透明
    emoji_opaque/<名字>.png   铺满整屏，不留透明
    manifest.json             这次出了哪些、各多大（--check 就靠它对照）
    preview.png               --preview 才有

为什么两个变体都出：emote 组件是**托管组件**（不在主仓库里），它到底怎么把图铺到
屏上 —— 居中？拉伸？留白？认不认透明底？—— 板子到货前没法规避。
两个都出，刷一次固件看一眼就有答案，比猜快。

为什么 --blink 是**替换**而不是新增：上游 packer 拿文件名当表情名，同一个目录里
同时有 happy.png 和 happy.gif，会在它的 index.json 里产生两条同名记录。
所以开了眨眼就只出 GIF（第一帧还是那张静态脸，静态信息一点没丢）。

为什么产物不写进仓库：一张 240x320 PNG 约 1.6~1.8KB、13 张 + 两个变体 + GIF
就到几百 KB，而它随时能重出（几分钟）。build/ 已经在 .gitignore 里。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from xiaoyu.config import PROJECT_ROOT
from xiaoyu.face.assets import INTENSITY_STEPS, asset_specs, asset_table
from xiaoyu.face.render import FACE_SIZE, blink_loop, render_face
from xiaoyu.logger import get_logger

logger = get_logger("scripts.build_face_assets")

DEFAULT_OUT = PROJECT_ROOT / "build" / "face_assets"
MANIFEST_NAME = "manifest.json"

# 变体：目录名 -> opaque。透明底那份放在 packer 默认读的 emoji/ 下。
ALPHA_DIR = "emoji"
OPAQUE_DIR = "emoji_opaque"

# 预览图上的中文名（目录名给机器看，这个给人看）
VARIANT_LABELS = {ALPHA_DIR: "透明底", OPAQUE_DIR: "铺满"}

# 上游 packer 的位置（相对 xiaozhi-esp32 仓库根）
PACKER_REL = Path("scripts") / "spiffs_assets" / "build.py"

# 上游要的三个参数，缺一个都会打出残废的 assets.bin
PACK_INPUTS = (
    ("--wakenet_model", "唤醒词模型（缺了它，喊「小柚子」没反应）"),
    ("--text_font", "中文字体（缺了它，设备上中文是空白）"),
    ("--emoji_collection", "我们的表情目录（就是上面出的 emoji/）"),
)

# 板子到货当天最容易踩的一脚
ASSETS_SIZE_TRAP = (
    "上游 build.py 里 assets_size 写死 \"0x400000\"（4MB），"
    "我们 16m 分区表里 assets 是 0x800000（8MB）：打包前先改那一个数字，"
    "否则大一点的资产包会被截断。"
)


def parse_size(text: str) -> tuple[int, int]:
    """'240x320' -> (240, 320)。写错了**直接报错**。

    静默退回默认值会让人以为成功了，而尺寸错了这一批资产全是废的。
    """
    parts = text.lower().replace("*", "x").split("x")
    if len(parts) != 2:
        raise ValueError(f"尺寸要写成 240x320 这样：{text!r}")
    try:
        width, height = (int(part) for part in parts)
    except ValueError:
        raise ValueError(f"尺寸里只能是整数：{text!r}") from None
    if width <= 0 or height <= 0:
        raise ValueError(f"尺寸要正数：{text!r}")
    return width, height


def pack_command(
    repo: str | Path, collection: str | Path, *,
    wakenet: str | Path, text_font: str | Path, python: str | None = None,
) -> list[str]:
    """拼出调上游 packer 的命令。只拼不跑（好单测）。"""
    return [
        python or sys.executable,
        str(Path(repo) / PACKER_REL),
        "--wakenet_model", str(Path(wakenet)),
        "--text_font", str(Path(text_font)),
        "--emoji_collection", str(Path(collection)),
    ]


def check_xiaozhi_repo(repo: str | Path) -> list[str]:
    """上游仓库能不能用来打包。返回问题列表，空 = 可以。"""
    root = Path(repo)
    packer = root / PACKER_REL
    if not packer.exists():
        return [f"这不是 xiaozhi-esp32 仓库（找不到 {packer}）"]
    problems = []
    srmodel_dir = root / "managed_components" / "espressif__esp-sr" / "model" / "wakenet_model"
    if not srmodel_dir.exists():
        problems.append(
            f"找不到唤醒词模型目录 {srmodel_dir} —— 先让 idf.py 拉一次依赖"
            "（managed_components 是构建时下载的）"
        )
    if not (root / "components" / "noto-fonts").exists():
        problems.append(f"找不到字体目录 {root / 'components' / 'noto-fonts'}")
    return problems


def _font(size: int = 14):
    """预览图用的字体。找不到就用 PIL 默认的（默认字体不认中文，标题会变方框）。"""
    from PIL import ImageFont

    for path in ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf"):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def contact_sheet(
    rows: list[tuple[str, list]], *, cell_size: tuple[int, int],
    label_width: int = 200, gap: int = 8, title: str = "",
):
    """把 (标签, 图列表) 拼成一张检查图。只给眼睛看，不进设备。"""
    from PIL import Image, ImageDraw

    columns = max((len(images) for _, images in rows), default=0)
    if not rows or columns == 0:
        raise ValueError("没有图可拼")
    header = 40 if title else 0
    width = label_width + columns * (cell_size[0] + gap) + gap
    height = header + len(rows) * (cell_size[1] + gap) + gap
    sheet = Image.new("RGB", (width, height), (18, 18, 18))
    draw = ImageDraw.Draw(sheet)
    font = _font()
    if title:
        draw.text((gap * 2, gap + 6), title, fill=(232, 232, 224), font=font)
    y = header + gap
    for label, images in rows:
        draw.text((gap * 2, y + gap), label, fill=(206, 206, 196), font=font)
        x = label_width + gap
        for image in images:
            # 用图自己的 alpha 当遮罩贴，透明底那版才不会糊成一块黑方块
            sheet.paste(image, (x, y), image.convert("RGBA"))
            x += cell_size[0] + gap
        y += cell_size[1] + gap
    return sheet


def save_gif(frames: list, ms: int, path: Path) -> None:
    """存 GIF。每帧 100 毫秒左右、循环播（参数怎么定的见 render.blink_loop）。"""
    frames[0].save(
        path, save_all=True, append_images=frames[1:],
        duration=ms, loop=0,
    )


def build(
    out: Path, size: tuple[int, int], variants: tuple[str, ...],
    *, blink: bool = False, preview: bool = False,
) -> dict[str, object]:
    """出资产。返回清单（也写进 manifest.json）。"""
    table = asset_table()
    entries: list[dict[str, object]] = []
    # (变体, 情绪) -> 按强度排好的图，只有 --preview 才攒
    shown: dict[tuple[str, str], list] = {}

    for directory in variants:
        opaque = directory == OPAQUE_DIR
        target = out / directory
        target.mkdir(parents=True, exist_ok=True)
        for name, mood, step in asset_specs():
            expression = table[name]
            frames = None
            if blink:
                frames, ms = blink_loop(expression, mood=mood, size=size, opaque=opaque)
                filename = f"{name}.gif"
                save_gif(frames, ms, target / filename)
            else:
                image = render_face(expression, mood=mood, size=size, opaque=opaque)
                filename = f"{name}.png"
                image.save(target / filename)
            if preview:
                # 眨眼版取「睁着」和「闭死」两帧：末尾是 blink_frames 的
                # 0, 0.5, 1, 0.5, 0 -> 倒数第三个正好是完全闭上那张
                shown.setdefault((directory, mood), []).extend(
                    [frames[0], frames[-3]] if frames else [image]
                )
            entries.append({
                "name": name,
                "mood": mood,
                "intensity": step,
                "variant": directory,
                "file": f"{directory}/{filename}",
                "bytes": (target / filename).stat().st_size,
            })
            logger.info("{} [{}] -> {}", name, directory, filename)

    if preview:
        # 一行一个情绪、一列一档强度 —— 挨着看才知道强度是不是真的在变
        sheet = contact_sheet(
            [
                (f"{VARIANT_LABELS.get(directory, directory)} / {mood}", images)
                for (directory, mood), images in shown.items()
            ],
            cell_size=size,
            title=(
                f"{size[0]}x{size[1]}  行=情绪，列=强度 "
                f"{INTENSITY_STEPS[0]:g} -> {INTENSITY_STEPS[-1]:g}"
            ),
        )
        sheet.save(out / "preview.png")
        logger.info("外观检查图：{}", out / "preview.png")

    manifest: dict[str, object] = {
        "size": list(size),
        "blink": blink,
        "variants": list(variants),
        "assets": entries,
        "total_bytes": sum(int(e["bytes"]) for e in entries),
    }
    out.mkdir(parents=True, exist_ok=True)
    (out / MANIFEST_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def check(out: Path, variants: tuple[str, ...], *, blink: bool = False) -> list[str]:
    """核对磁盘上跟资产表对不对得上。返回问题列表，空 = 齐了。"""
    problems: list[str] = []
    manifest = out / MANIFEST_NAME
    if not manifest.exists():
        return [f"还没有出过资产（找不到 {manifest}）：先不带 --check 跑一次"]
    for name, _mood, _step in asset_specs():
        for directory in variants:
            expected = out / directory / f"{name}.{'gif' if blink else 'png'}"
            if not expected.exists():
                problems.append(f"缺 {expected}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="出设备端表情资产（PNG / 眨眼 GIF / 预览图 / 清单）",
    )
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="输出目录")
    parser.add_argument("--size", default=f"{FACE_SIZE[0]}x{FACE_SIZE[1]}", help="屏幕像素，如 240x320")
    parser.add_argument(
        "--variant", choices=("both", ALPHA_DIR, OPAQUE_DIR), default="both",
        help="出哪个变体（默认两个都出）",
    )
    parser.add_argument("--blink", action="store_true", help="出会眨眼的 GIF（替换同名的 PNG）")
    parser.add_argument("--preview", action="store_true", help="顺带出一张外观检查图")
    parser.add_argument("--check", action="store_true", help="只核对磁盘上是齐的，不写文件")
    parser.add_argument("--pack", metavar="REPO", help="看打包成 assets.bin 的命令（给 xiaozhi-esp32 仓库路径）")
    parser.add_argument("--run", action="store_true", help="--pack 时真的执行（默认只打印命令）")
    args = parser.parse_args(argv)

    out = Path(args.out)
    variants = (ALPHA_DIR, OPAQUE_DIR) if args.variant == "both" else (args.variant,)

    if args.check:
        problems = check(out, variants, blink=args.blink)
        for problem in problems:
            logger.warning("{}", problem)
        if problems:
            return 1
        logger.info("资产齐了：{} 个名字 x {} 个变体", len(asset_specs()), len(variants))
        return 0

    try:
        size = parse_size(args.size)
    except ValueError as exc:
        logger.error("{}", exc)
        return 2

    manifest = build(out, size, variants, blink=args.blink, preview=args.preview)
    logger.info(
        "出完：{} 个文件，共 {} KB -> {}",
        len(manifest["assets"]), int(manifest["total_bytes"]) // 1024, out,
    )
    logger.info(
        "刷进设备前：emote 组件怎么铺图还没验证 → 两个变体都留着，刷一次看哪个对"
    )

    if args.pack:
        problems = check_xiaozhi_repo(args.pack)
        for problem in problems:
            logger.warning("{}", problem)
        logger.info("⚠️ {}", ASSETS_SIZE_TRAP)
        logger.info("要跑的命令里缺一不可的参数：")
        for flag, why in PACK_INPUTS:
            logger.info("  {}  {}", flag, why)
        if not problems:
            command = pack_command(
                args.pack, out / ALPHA_DIR,
                wakenet=Path(args.pack) / "managed_components" / "espressif__esp-sr"
                        / "model" / "wakenet_model" / "wn9_nihaoxiaozhi_tts",
                text_font=Path(args.pack) / "components" / "noto-fonts" / "cbin"
                          / "font_noto_sans_common_20_4.bin",
            )
            logger.info("命令：{}", " ".join(command))
            if args.run:
                logger.info("开始打包……")
                return subprocess.call(command)
            logger.info("加 --run 才会真的执行（第一次先看清楚上面那行）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
