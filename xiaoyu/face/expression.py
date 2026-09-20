"""表情参数层：把 (mood, intensity) 变成一组"能画的参数"。

为什么要有这一层，而不是继续在画脸的代码里写 if：
    现在 face/index.html 的 draw() 里，情绪是一串硬编码的判断 ——
    `if (m === "angry") eyeH *= 0.7`。两个问题：
    1. mood 和 intensity 混在一起。happy 0.3 和 happy 0.9 长得几乎一样，
       因为强度只作用在嘴张多大上，眼睛没变。
    2. 想加一个自由度（比如眼皮倾角）要改每一条判断。

这里只做一件事：**mood 决定摆哪张脸，intensity 决定摆到什么程度**。
实现方式是"从基准脸线性插值到目标脸"，所以强度天然是连续的，
加一个情绪也只需要在 _TARGETS 里加一行。

它和两边的渲染是什么关系：
    - 设备端（ESP32）：跑的是**预渲染的图**，不是这段代码。
      但那些图就是用这张表**离线生成**的（scripts/build_face_assets.py）。
    - PC / 平板：face/index.html 是另一套实时渲染（要跟视线、要跟口型），
      它读的是同一套配色和同一张情绪表。
    两份渲染实现，**一份参数出处** —— 这里是唯一能被单测覆盖的地方。

设计依据：用户 2026-09-20 给的 7 张表情设计稿。那套设计真正多的自由度是
"眼皮角度"和"眼神方向"，不是装饰（星星、?、火星）—— 后者在真屏
240x320 上会糊掉，实测见 docs/design/表情_真屏240x320_验证.png。
"""

from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass

# 和 llm/emotion.py、face/protocol.py 的 VALID_MOODS 保持一致。
# 写第三遍是有意的：出资产时只想 import 这一层，不想把整个主控拉起来。
VALID_MOODS = ("happy", "sad", "angry", "surprised", "neutral")

# 情绪色。和设备端底部那条发光条、PC 上那圈光环**是同一套色**，
# 数值抄自 face/index.html 的 HALO 表。改一处要两处一起改，所以在这里当出处。
MOOD_COLORS: dict[str, tuple[int, int, int]] = {
    "happy": (224, 168, 74),      # 暖金
    "sad": (110, 126, 160),       # 蓝灰
    "angry": (196, 64, 48),       # 红
    "surprised": (72, 166, 164),  # 青
    "neutral": (150, 148, 140),   # 中灰（基准，几乎不亮）
}


def mood_color(mood: object) -> tuple[int, int, int]:
    """情绪色。认不出来的一律给 neutral 的灰。

    先判 isinstance 是必要的：mood 可能来自 JSON，理论上是个 list / dict，
    而 `dict.get(不可哈希)` 会抛 TypeError —— 渲染层不该为一个配色被带崩。
    """
    if isinstance(mood, str):
        return MOOD_COLORS.get(mood, MOOD_COLORS["neutral"])
    return MOOD_COLORS["neutral"]


# 嘴型。渲染器认这几个名字；不认识的嘴型渲染器会退回 soft，不抛异常。
MOUTHS = ("soft", "smile", "big_smile", "frown", "flat", "o", "wave")


@dataclass(frozen=True)
class Expression:
    """一张脸的参数。

    这里是**形状**，不是情绪 —— "哪个情绪长什么样"在下面的 _TARGETS 里。
    所有 float 字段都约定在 0~1（带 tilt / pupil 的那几个是 -1~1），
    渲染器可以直接信这个约定，不用再夹一遍。
    """

    # 眼型：0 = 竖圆角方（默认），1 = 正圆（惊讶）
    eye_shape: float = 0.0
    # 眼睛整体缩放（听话时睁大、说话时轻跳）
    eye_scale: float = 1.0
    # 上眼皮从上方盖下来的比例。0 = 全睁
    lid_top: float = 0.0
    # 下眼皮从下方盖上来的比例。开心时盖到 0.6 就成了弯月眼
    lid_bottom: float = 0.0
    # 眼皮倾角：正 = 内低外高（眼皮往内侧压 = 锐眼/生气），
    # 负 = 外低内高（眼皮往外侧垂 = 垂眼/难过）
    lid_tilt: float = 0.0
    # 瞳孔偏移（-1~1）。0 = 正中；正 x = 往屏幕右边看，正 y = 往下看
    pupil_x: float = 0.0
    pupil_y: float = 0.0
    # 眉毛：正 = 内低外高（倒眉，生气），负 = 内高外低（八字眉，难过）
    brow_angle: float = 0.0
    brow_lift: float = 0.0
    # 嘴
    mouth: str = "soft"
    mouth_scale: float = 0.45
    # 腮红和底部情绪色条的浓度
    blush: float = 0.0
    stripe_level: float = 0.15


# 基准脸（neutral 0 强度）：一张"有脸"但不表达情绪的脸。
# 注意 mouth="soft" —— 静止时只求看得出是张脸，不求表情，见 face/index.html 的同类注释。
BASE = Expression()


# 目标脸：intensity = 1 时的样子。
# 数值不是随手写的 —— 对着用户那 7 张设计稿定的：
#   生气 = 上眼皮压下来 + 内高外低的锐眼 + 倒眉
#   无语/不爽 = 上面那套的**低强度版本**（这就是"强度连续"白赚的两个表情）
#   开心 = 下眼皮盖上来成弯月 + 大笑 + 腮红
#   惊讶 = 眼型切成正圆 + 挑眉 + 圆嘴
#   难过 = 上眼皮垂下 + 八字眉 + 嘴角向下 + 视线下垂
_TARGETS: dict[str, Expression] = {
    "neutral": BASE,
    "happy": Expression(
        eye_shape=0.20, eye_scale=1.00,
        lid_bottom=0.58, lid_tilt=0.0,
        pupil_y=-0.05,
        brow_lift=0.25,
        mouth="big_smile", mouth_scale=0.90,
        blush=0.70, stripe_level=1.00,
    ),
    "sad": Expression(
        eye_shape=0.05, eye_scale=0.96,
        lid_top=0.46, lid_tilt=-0.38,
        pupil_y=0.28,
        brow_angle=-0.70, brow_lift=-0.12,
        mouth="frown", mouth_scale=0.60,
        blush=0.0, stripe_level=0.70,
    ),
    "angry": Expression(
        eye_shape=0.0, eye_scale=0.98,
        lid_top=0.42, lid_tilt=0.55,
        pupil_y=0.0,
        brow_angle=0.80, brow_lift=-0.28,
        mouth="frown", mouth_scale=0.72,
        blush=0.0, stripe_level=1.00,
    ),
    "surprised": Expression(
        eye_shape=1.0, eye_scale=1.15,
        lid_top=0.0, lid_bottom=0.0,
        pupil_y=-0.05,
        brow_lift=0.60,
        mouth="o", mouth_scale=0.95,
        blush=0.0, stripe_level=0.90,
    ),
}


def _clamp01(value: object, default: float = 0.0) -> float:
    """夹到 0~1。非数字、NaN、无穷一律退回 default —— 情绪是表演，不能带崩渲染。"""
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number):
        return default
    return min(1.0, max(0.0, number))


def expression_for(mood: str, intensity: float = 1.0) -> Expression:
    """(mood, intensity) -> Expression。

    intensity 是从基准脸到目标脸的插值比例，所以：
    - 强度 0 -> 就是基准脸（不管什么情绪）
    - 强度 1 -> 目标脸
    - 中间 -> 连续过渡（这就是设备端做不到、要靠多档资产近似的那条曲线）

    mood 不认识、intensity 不是数字，一律退回基准脸 —— 和
    face/protocol.py 的 emotion_message() 一个态度：消费端不许被带崩。
    """
    # 先判 isinstance：mood 从 JSON 里来，理论上可能是 list / dict，而
    # _TARGETS.get() 遇到不可哈希的类型会抛 TypeError。挡住它，
    # docstring 里承诺的「消费端不许被带崩」才是真的（有用例钉这条）。
    target = _TARGETS.get(mood) if isinstance(mood, str) else None
    if target is None:
        target = BASE
    k = _clamp01(intensity)

    # 嘴型是离散的，不能插值：强度起来了就换成目标嘴型，靠 mouth_scale 控制大小。
    # 这样 (angry, 0.25) 得到的是"一张很小的撇嘴" = 设计稿里那张"不爽"。
    mouth = target.mouth if k > 0.05 else BASE.mouth

    values: dict[str, object] = {}
    for field in dataclasses.fields(Expression):
        name = field.name
        if name == "mouth":
            values[name] = mouth
            continue
        start = getattr(BASE, name)
        end = getattr(target, name)
        values[name] = start + (end - start) * k
    return Expression(**values)  # type: ignore[arg-type]
