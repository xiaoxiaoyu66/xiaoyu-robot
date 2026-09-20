"""把 Expression 画成一张图 —— 设备端表情资产的出品车间。

为什么是 Pillow 而不是复用 face/index.html 那套 Canvas：
    - 能单测（不用起浏览器、不用板子），这个项目里"能测"比"少写一份"重要
    - repo 已经有 Pillow 生态（出资产是离线活儿，快慢无所谓）
    代价是**渲染实现有两份**（浏览器一份、这里一份）。所以配色和情绪表
    只允许有一个出处：颜色在 expression.py，几何常量在本文件顶部。
    face/index.html 那边改颜色时，这里要跟着改 —— 这是已知的重复，
    比"两份参数表各说各话"要好查。

画什么（对着用户那 7 张设计稿定的，见 docs/design/）：
    面板（深色圆角） / 两只眼（豆形，带瞳孔） / 眼皮（上、下、可倾斜）
    / 眉毛（只在生气/难过/惊讶出现） / 嘴 / 腮红 / 底部情绪色条

不画什么：星星、问号、火星、电池图标 —— 真屏只有 240x320，
实测这些装饰缩下去就是一团糊（docs/design/表情_真屏240x320_验证.png）。

超采样：先在 4 倍大的画布上画，再 LANCZOS 缩回 240x320。
Pillow 的圆角矩形/多边形**不做抗锯齿**，直接按 240x320 画边缘会是锯齿。
缩回来之后边缘是灰阶过渡 —— 那正是设备上该看到的样子。

size / opaque 都要出两版：emote 组件到底怎么把图铺到屏上（居中？拉伸？
带不带自己的边框？透明底认不认？）还没验证过，等板子到才能定。
所以两个变体都生成，刷一次就知道了。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..logger import get_logger
from .expression import Expression, mood_color

if TYPE_CHECKING:                       # 只给类型检查看，运行时不强依赖 Pillow
    from PIL.Image import Image as PILImage

logger = get_logger(__name__)

FACE_SIZE = (240, 320)      # 设备屏 2.4" ST7789 的真实像素。脸就是整块屏。
SUPERSAMPLE = 4             # 抗锯齿用，见文件头

PANEL = (38, 38, 36)        # 和 face/index.html 的 PANEL 同值 —— 别各定各的
EYE = (241, 239, 232)       # 同上，EYE
BLUSH_COLOR = (233, 120, 120)
PANEL_RADIUS = 26.0

EYE_CX = (78.0, 162.0)      # 两只眼的中心 x
EYE_CY = 148.0
EYE_W = 46.0
EYE_H = 78.0
PUPIL_RATIO = 0.30          # 瞳孔半径 = 眼宽 x 这个比例
LID_SLOPE = 0.45            # 眼皮倾斜时，边缘上下差多少（相对眼高）

BROW_GAP = 20.0             # 眉毛到眼睛上缘的距离
BROW_PAD = 6.0              # 眉毛比眼睛每边宽出多少
BROW_THICK = 5.0
BROW_ANGLE_DEG = 0.35       # brow_angle 满偏时眉毛两端差多少（相对眼高）
BROW_LIFT_PX = 12.0         # brow_lift 满偏时整条眉毛升降多少

MOUTH_CX = 120.0
MOUTH_CY = 232.0
MOUTH_W = 30.0
MOUTH_H = 16.0
MOUTH_THICK = 6.0

STRIPE_BOX = (84.0, 284.0, 156.0, 292.0)
STRIPE_RADIUS = 4.0

BLUSH_AT = ((44.0, 202.0), (196.0, 202.0))
BLUSH_RX, BLUSH_RY = 15.0, 9.0


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


class _Pen:
    """把"设计尺寸"换算成超采样画布坐标的小包装。

    几何常量全按 240x320 写，乘法集中在这里 —— 不然每个数后面都要跟着 * S，
    改一个尺寸就要全文找一遍。
    """

    def __init__(self, image, scale: int) -> None:
        from PIL import ImageDraw

        self._draw = ImageDraw.Draw(image)
        self.s = float(scale)

    def _box(self, box):
        return [v * self.s for v in box]

    def _pts(self, points):
        return [(x * self.s, y * self.s) for x, y in points]

    def rr(self, box, radius: float, fill) -> None:
        self._draw.rounded_rectangle(
            self._box(box), radius=radius * self.s, fill=fill
        )

    def ell(self, box, fill) -> None:
        self._draw.ellipse(self._box(box), fill=fill)

    def poly(self, points, fill) -> None:
        self._draw.polygon(self._pts(points), fill=fill)

    def line(self, points, fill, width: float) -> None:
        self._draw.line(
            self._pts(points), fill=fill, width=max(1, int(width * self.s)),
            joint="curve",
        )

    def arc(self, box, start: float, end: float, fill, width: float) -> None:
        self._draw.arc(
            self._box(box), start=start, end=end, fill=fill,
            width=max(1, int(width * self.s)),
        )

    def ring(self, box, fill, width: float) -> None:
        self._draw.ellipse(
            self._box(box), outline=fill, width=max(1, int(width * self.s))
        )

    def pie(self, box, start: float, end: float, fill) -> None:
        self._draw.pieslice(self._box(box), start=start, end=end, fill=fill)


def _quad(p0, p1, p2, steps: int = 16):
    """二次贝塞尔采样成折线。Pillow 没有曲线，但它有折线。"""
    out = []
    for i in range(steps + 1):
        t = i / steps
        u = 1.0 - t
        x = u * u * p0[0] + 2 * u * t * p1[0] + t * t * p2[0]
        y = u * u * p0[1] + 2 * u * t * p1[1] + t * t * p2[1]
        out.append((x, y))
    return out


def _eye_box(cx: float, exp: Expression):
    """眼睛的外框（设计尺寸）。eye_shape 往 1 走时，高度收拢成正方形 = 正圆。"""
    w = EYE_W * exp.eye_scale
    h = _lerp(EYE_H, EYE_W, exp.eye_shape) * exp.eye_scale
    return cx - w / 2, EYE_CY - h / 2, cx + w / 2, EYE_CY + h / 2, w, h


def _lid_edge_y(edge_y: float, tilt: float, h: float, *, inner: bool, top: bool) -> float:
    """眼皮边缘在"内侧/外侧"两点上的 y。

    tilt > 0 表示"内侧压得更低"（锐眼/生气）：上眼皮内侧更低，
    下眼皮内侧更高 —— 合起来剩下一条往内收的斜缝。
    """
    offset = tilt * h * LID_SLOPE
    if not inner:
        offset = -offset
    if not top:
        offset = -offset
    return edge_y + offset


def _draw_eye(canvas, cx: float, side: str, exp: Expression, s: int) -> None:
    """单独画一只眼：眼白 + 瞳孔 + 上下眼皮，最后整体裁进眼睛形状里。

    为什么要走"层 + 蒙版"而不是直接画：瞳孔和眼皮都必须被限制在眼白范围内，
    直接画会溢出眼框（眼皮会盖到脸上）。蒙版是唯一干净的做法。
    """
    from PIL import Image, ImageChops

    left, top, right, bottom, w, h = _eye_box(cx, exp)
    radius = w / 2

    # 眼皮把眼睛盖成一条缝时，形状已经退化了，但底下的几何还在算 ——
    # 这里不特判，交给蒙版收尾（少一个分支就少一处能出错的地方）。
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    mask = Image.new("L", canvas.size, 0)

    pen_layer = _Pen(layer, s)
    pen_mask = _Pen(mask, s)

    eye_fill = EYE + (255,)
    pen_layer.rr((left, top, right, bottom), radius, eye_fill)
    pen_mask.rr((left, top, right, bottom), radius, 255)

    # 瞳孔：在眼白里偏移，留出瞳孔半径的余量，所以永远不会贴到边
    pupil_r = w * PUPIL_RATIO
    max_dx = max(0.0, w / 2 - pupil_r)
    max_dy = max(0.0, h / 2 - pupil_r)
    px = cx + exp.pupil_x * max_dx
    py = EYE_CY + exp.pupil_y * max_dy          # 正 y = 往下看（屏幕坐标往下是正）
    pen_layer.ell(
        (px - pupil_r, py - pupil_r, px + pupil_r, py + pupil_r), PANEL + (255,)
    )

    top_lid = exp.lid_top > 0.001
    bottom_lid = exp.lid_bottom > 0.001
    if top_lid or bottom_lid:
        y_top_in = _lid_edge_y(top + exp.lid_top * h, exp.lid_tilt, h, inner=True, top=True)
        y_top_out = _lid_edge_y(top + exp.lid_top * h, exp.lid_tilt, h, inner=False, top=True)
        y_bot_in = _lid_edge_y(bottom - exp.lid_bottom * h, exp.lid_tilt, h, inner=True, top=False)
        y_bot_out = _lid_edge_y(bottom - exp.lid_bottom * h, exp.lid_tilt, h, inner=False, top=False)
        if side == "left":
            top_left, top_right = y_top_out, y_top_in
            bot_left, bot_right = y_bot_out, y_bot_in
        else:
            top_left, top_right = y_top_in, y_top_out
            bot_left, bot_right = y_bot_in, y_bot_out

        pad = 6.0      # 往外多画一点，免得边缘留缝
        if top_lid:
            pen_layer.poly(
                [(left - pad, top - pad), (right + pad, top - pad),
                 (right + pad, top_right), (left - pad, top_left)],
                PANEL + (255,),
            )
        if bottom_lid:
            pen_layer.poly(
                [(left - pad, bottom + pad), (right + pad, bottom + pad),
                 (right + pad, bot_right), (left - pad, bot_left)],
                PANEL + (255,),
            )

    layer.putalpha(ImageChops.multiply(layer.getchannel("A"), mask))
    canvas.alpha_composite(layer)


def _draw_brows(pen: _Pen, exp: Expression) -> None:
    """眉毛。只在 angle 或 lift 明显时才画 —— 这正是现在 face/index.html 的行为
    （生气/难过/惊讶有眉毛，开心/平静没有）。"""
    if abs(exp.brow_angle) < 0.05 and abs(exp.brow_lift) < 0.30:
        return
    lift = -exp.brow_lift * BROW_LIFT_PX      # 正 = 往上抬
    color = EYE + (255,)
    for cx in EYE_CX:
        _, top, _, _, w, h = _eye_box(cx, exp)
        inner_x = cx + w / 2 + BROW_PAD if cx < MOUTH_CX else cx - w / 2 - BROW_PAD
        outer_x = cx - w / 2 - BROW_PAD if cx < MOUTH_CX else cx + w / 2 + BROW_PAD
        # brow_angle > 0 = 内低外高（倒眉/生气）
        inner_y = top - BROW_GAP + lift + exp.brow_angle * h * BROW_ANGLE_DEG
        outer_y = top - BROW_GAP + lift - exp.brow_angle * h * BROW_ANGLE_DEG
        pen.line([(outer_x, outer_y), (inner_x, inner_y)], color, BROW_THICK)


def _draw_mouth(pen: _Pen, exp: Expression) -> None:
    """嘴。mouth 是离散档，mouth_scale 控制大小。"""
    color = EYE + (255,)
    w = MOUTH_W * exp.mouth_scale
    h = MOUTH_H * exp.mouth_scale
    cx, cy = MOUTH_CX, MOUTH_CY
    kind = exp.mouth

    if kind == "big_smile":
        # 填满的半圆（设计稿里"开心"那张）：平的上沿 + 圆的下沿
        pen.pie((cx - w, cy - h, cx + w, cy + h), 0, 180, color)
    elif kind == "o":
        pen.ring((cx - w * 0.42, cy - h * 0.62, cx + w * 0.42, cy + h * 0.62),
                 color, MOUTH_THICK * 0.8)
    elif kind == "flat":
        pen.line([(cx - w * 0.7, cy), (cx + w * 0.7, cy)], color, MOUTH_THICK)
    elif kind == "frown":
        pen.line(_quad((cx - w, cy + h * 0.35), (cx, cy - h * 0.6), (cx + w, cy + h * 0.35)),
                 color, MOUTH_THICK)
    elif kind == "wave":
        # 撇嘴：先往下再往上，两端错开 —— 比单纯的下弯"更像有情绪"
        pen.line(_quad((cx - w, cy - h * 0.1), (cx - w * 0.3, cy + h * 0.7), (cx + w * 0.15, cy - h * 0.1)),
                 color, MOUTH_THICK)
        pen.line(_quad((cx + w * 0.15, cy - h * 0.1), (cx + w * 0.6, cy - h * 0.6), (cx + w, cy - h * 0.2)),
                 color, MOUTH_THICK)
    elif kind == "smile":
        pen.line(_quad((cx - w, cy - h * 0.35), (cx, cy + h * 0.7), (cx + w, cy - h * 0.35)),
                 color, MOUTH_THICK)
    else:
        # soft（含所有不认识的嘴型）：静止时的一条浅弧，只求"有张脸"
        pen.line(_quad((cx - w * 0.55, cy - h * 0.2), (cx, cy + h * 0.55), (cx + w * 0.55, cy - h * 0.2)),
                 EYE + (110,), MOUTH_THICK * 0.7)


def render_face(
    expression: Expression,
    *,
    mood: str = "neutral",
    size: tuple[int, int] = FACE_SIZE,
    opaque: bool = False,
) -> "PILImage":
    """画一帧，返回 RGBA 图。

    opaque=False（默认）：圆角面板 + 四周透明，让设备自己的背景透出来
    opaque=True：整块铺满，不留透明像素
    两种都出，是因为 emote 组件怎么铺图还没验证过（见文件头）。
    """
    from PIL import Image

    if size[0] <= 0 or size[1] <= 0:
        raise ValueError(f"尺寸不合法：{size}")

    width, height = size
    s = SUPERSAMPLE
    canvas = Image.new("RGBA", (width * s, height * s), (0, 0, 0, 0))
    pen = _Pen(canvas, s)

    # 面板
    if opaque:
        pen.rr((-1, -1, width + 1, height + 1), 0, PANEL + (255,))
    else:
        pen.rr((0, 0, width - 1, height - 1), PANEL_RADIUS, PANEL + (255,))

    # 底部情绪色条：一块纯色，真屏上完全不糊，性价比最高的一个情绪出口
    if expression.stripe_level > 0.01:
        rgb = mood_color(mood)
        alpha = int(255 * min(1.0, expression.stripe_level))
        pen.rr(STRIPE_BOX, STRIPE_RADIUS, rgb + (alpha,))

    # 腮红要压在眼睛下面，所以先画
    if expression.blush > 0.01:
        alpha = int(255 * 0.35 * min(1.0, expression.blush))
        for bx, by in BLUSH_AT:
            pen.ell((bx - BLUSH_RX, by - BLUSH_RY, bx + BLUSH_RX, by + BLUSH_RY),
                    BLUSH_COLOR + (alpha,))

    for cx, side in zip(EYE_CX, ("left", "right")):
        _draw_eye(canvas, cx, side, expression, s)

    _draw_brows(pen, expression)
    _draw_mouth(pen, expression)

    return canvas.resize(size, Image.LANCZOS)


def blink_frames(
    expression: Expression, *, mood: str = "neutral", frames: int = 5,
    size: tuple[int, int] = FACE_SIZE, opaque: bool = False,
) -> list:
    """眨眼用的几帧：闭 -> 开。

    GIF 在设备上是能播的（packer 的 support_format 里有 .gif），
    所以"会眨眼"这件事在板子上不用改固件就能做到。
    返回的帧列表直接喂给 PIL 存 GIF（见 scripts/build_face_assets.py）。
    """
    import dataclasses

    if frames < 2:
        raise ValueError("frames 至少 2")
    out = []
    for i in range(frames):
        # 0 -> 1 -> 0 的三角波：闭一次再睁开
        t = 1.0 - abs(2.0 * i / (frames - 1) - 1.0)
        closed = dataclasses.replace(expression, lid_top=1.0)
        out.append(render_face(
            _mix(expression, closed, t), mood=mood, size=size, opaque=opaque
        ))
    return out


# 眨眼循环的默认节奏。两个数都是「手感」不是物理量：眨太快像抽搐，
# 太久看不出是活的。3.2 秒睁着 + 0.5 秒眨完，是卡通脸的常见做法。
BLINK_HOLD_FRAMES = 32
BLINK_FRAME_MS = 100


def blink_loop(
    expression: Expression, *, mood: str = "neutral", frames: int = 5,
    hold_frames: int = BLINK_HOLD_FRAMES, frame_ms: int = BLINK_FRAME_MS,
    size: tuple[int, int] = FACE_SIZE, opaque: bool = False,
) -> tuple[list, int]:
    """眨眼循环：(帧列表, 每帧停留毫秒)。

    为什么要「睁着不动」那一段：只有闭-开两帧的 GIF 会**一直眨**，像抽搐。
    先睁着几帧、眨一下、再睁回去，才像「活着」。

    为什么每帧时长都一样（而不是给某一帧单独一个 3200 毫秒的 duration）：
    有的播放器会忽略 per-frame duration，拿一个全局延时播所有帧 —— 那样 3.2 秒
    那一帧会变成 100 毫秒，眨眼直接变抽搐。统一时长 + 多复制几帧「睁着」的画面，
    两种播放器下都是对的，代价只是文件大一点。

    返回值直接喂 PIL：
        frames[0].save(path, save_all=True, append_images=frames[1:],
                       duration=ms, loop=0)
    """
    if hold_frames < 0:
        raise ValueError("hold_frames 不能是负数")
    if frame_ms <= 0:
        raise ValueError("frame_ms 要正数")
    opened = render_face(expression, mood=mood, size=size, opaque=opaque)
    # 复制的是同一个 Image 对象：PIL 存 GIF 时只是逐帧读它，不会改内容
    images = [opened] * hold_frames + blink_frames(
        expression, mood=mood, frames=frames, size=size, opaque=opaque
    )
    return images, frame_ms


def _mix(a: Expression, b: Expression, t: float) -> Expression:
    import dataclasses

    values = {}
    for field in dataclasses.fields(Expression):
        name = field.name
        if name == "mouth":
            values[name] = b.mouth if t > 0.5 else a.mouth
            continue
        values[name] = _lerp(getattr(a, name), getattr(b, name), t)
    return Expression(**values)
