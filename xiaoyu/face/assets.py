"""设备端表情资产的分档与命名 —— 主控和设备共用的那一张表。

为什么单独有这一层：
    设备端（ESP32）跑的不是代码，是**预渲染的图**，图的名字就是表情名 ——
    上游 packer 拿文件 stem 当 name（spiffs_assets/build.py::
    process_emoji_collection），固件里 EmoteDisplay::SetEmotion(name) 拿这个名字
    直接查资源。所以「发哪个名字」必须只有一处出处，就是这里。
    PC / 平板那套实时渲染（face/index.html）不需要这张表 —— 它按连续强度自己画。

为什么要分档：
    设备端只能整张整张地换图，没有插值。想让（生气 0.3）和（生气 1.0）长得不一样，
    只能出成两张图。用户那 7 张设计稿其实就是「同一套形状的几个强度」+「几个独立
    情绪」，所以取 0.3 / 0.6 / 1.0 三档。0.3 那一档正是设计稿里那张「不爽」
    （形状怎么来的见 expression.py 的文件头）。

命名规则：
    1.0 -> "happy"      满强度**不带后缀**：只认裸情绪名的客户端也能拿到最典型的
                        那张脸，不会因为查不到资源而空屏。
    0.6 -> "happy_06"
    0.3 -> "happy_03"
    neutral 只有一张，永远叫 "neutral" —— 基准脸没有强度可言
    （expression_for("neutral", k) 对任何 k 都等于 BASE，出三张是一模一样的图）。

谁在用：
    - scripts/build_face_assets.py：名字 -> 图（离线出资产）
    - xiaozhi 服务端发 emotion 字段时用 asset_name_for() 选档
    - 名字会直接变成文件名，所以有用例钉着「只能是 ASCII、不含分隔符、不超长」
      （上游 packer 的 config 里 name_length 是 32）
"""

from __future__ import annotations

import math

from .expression import VALID_MOODS, Expression, expression_for

# 由弱到强。顺序也是出资产的顺序（预览图就按这个排）。
INTENSITY_STEPS: tuple[float, ...] = (0.3, 0.6, 1.0)
FULL_STEP: float = INTENSITY_STEPS[-1]
NEUTRAL: str = "neutral"


def _known_mood(mood: object) -> str:
    """把任意输入收敛成一个合法情绪名，认不出来的一律 neutral。

    先判 isinstance 是必要的：mood 可能来自 JSON，理论上是个 list / dict，
    而 `x in frozenset` 遇到不可哈希的类型会抛 TypeError（有用例钉这条）。
    """
    if isinstance(mood, str) and mood in VALID_MOODS:
        return mood
    return NEUTRAL


def nearest_step(intensity: object) -> float:
    """任意强度 -> 最近的一档。坏输入 -> 满强度。

    为什么坏输入给满强度、而不是最低档：满强度那张是**裸名字**（见文件头），
    是「这个情绪最典型的样子」。查不到强度时给一张最像的，比给一张几乎没表情的
    更不容易让人以为坏了。

    数学上正好落在两档中间的输入（0.45）由浮点比较决定落到哪边 ——
    这里不承诺平局规则，调用方别依赖它（有用例钉着 0.44 / 0.46 这两侧）。
    """
    try:
        value = float(intensity)        # type: ignore[arg-type]
    except (TypeError, ValueError):
        return FULL_STEP
    if not math.isfinite(value):
        return FULL_STEP
    return min(INTENSITY_STEPS, key=lambda step: (abs(step - value), step))


def asset_name(mood: object, step: float = FULL_STEP) -> str:
    """(情绪, 档位) -> 资产名。也是文件名的主体（不含扩展名）。

    step 只认 INTENSITY_STEPS 里的值，不在表里就给裸名字。
    **故意不做「就近取档」** —— 要连续强度请用 asset_name_for()。
    调用方直接传了 0.42 这种数，说明它没意识到设备端只有三张图；这时候悄悄替它
    选一档，不如明确退到裸名字（那也是 asset_table() 里真实存在的一张）。
    """
    known = _known_mood(mood)
    if known == NEUTRAL or step not in INTENSITY_STEPS or step == FULL_STEP:
        return known
    return f"{known}_{int(round(step * 10)):02d}"


def asset_name_for(mood: object, intensity: object = FULL_STEP) -> str:
    """连续强度版：先就近归档再起名。服务端填 emotion 字段时走这个。"""
    return asset_name(mood, nearest_step(intensity))


def asset_table() -> dict[str, Expression]:
    """全部资产：名字 -> 这张脸长什么样。出资产脚本唯一读的东西。

    neutral 排第一（它是基准脸，也是任何认不出来的输入的落点）。
    """
    return {name: expression_for(mood, step) for name, mood, step in asset_specs()}


def asset_specs() -> list[tuple[str, str, float]]:
    """[(名字, 情绪, 档位)]，顺序就是出图顺序。

    单独留一个「名字 -> 从哪来」的入口，是因为清单 / 预览 / 出资产都要这份对应
    关系（只有名字是推不回情绪的：neutral 没档位，裸名字也没有）。让它们都读
    这里，就不会出现「脚本里又抄了一遍命名规则」那种漂移。
    """
    specs: list[tuple[str, str, float]] = [(NEUTRAL, NEUTRAL, FULL_STEP)]
    for mood in VALID_MOODS:
        if mood == NEUTRAL:
            continue
        for step in INTENSITY_STEPS:
            specs.append((asset_name(mood, step), mood, step))
    return specs
