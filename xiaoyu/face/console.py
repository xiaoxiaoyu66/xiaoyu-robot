"""控制台表情脸：在 PowerShell 里也能"看见"它（S5 的伴生品）。

脸的主力是 `face/index.html`（浏览器），但调试时人盯着终端，
为看一眼状态再切浏览器很烦。状态机本来就有 on_change 监听器、
播放器本来就在推音量包络 —— 这个模块把**同一批事件**翻译成几行
字符画打进日志流，浏览器脸和终端脸互不依赖，可以只开一个。

设计取舍：
- 用 logger 而不是 print（全项目规矩），所以这些"脸"会同时进日志文件，
  事后排查还能回放它当时是什么表情。
- 口型不打 50ms 实时刷新（会刷屏刷爆），而是把**一整句说话期间**的
  音量采样压成一条 8 字符 sparkline，句子说完随字幕一起打出来。
- 全部用单色字符（- ◕ ◉ × ▽ ▁▂▃▄▅▆▇█），不依赖 PowerShell 的
  emoji 渲染能力，老终端也不会花屏。
"""

from __future__ import annotations

from ..logger import get_logger
from ..state import State

logger = get_logger(__name__)

# 状态 -> 字符画。speaking 没有固定行：它由 caption + sparkline 呈现。
_FACES = {
    "idle": "(-_-) 待机",
    "listening": "(O_O) 听你说话",
    "thinking": "(=_=) 在想",
    "error": "(x_x) 出错了",
}

# 情绪 -> 字符画。全 ASCII，不赌终端的 emoji 渲染。
_MOODS = {
    "happy": "(^_^) 开心",
    "sad": "(;_;) 难过",
    "angry": "(>_<) 生气",
    "surprised": "(o_o) 惊讶",
    "neutral": "(-_-) 平静",
}

_BAR = "▁▂▃▄▅▆▇█"
_SAMPLES = 8


def sparkline(levels: list[float], width: int = _SAMPLES) -> str:
    """把一串 0~1 的音量压成 width 个块字符。

    长了均匀抽、短了居中补零 —— 任何输入都得到定宽的嘴型条，方便人眼对比。
    """
    if not levels:
        return " " * width
    if len(levels) > width:
        step = len(levels) / width
        picked = [levels[int(i * step)] for i in range(width)]
    else:
        pad = width - len(levels)
        picked = [0.0] * (pad // 2) + list(levels) + [0.0] * (pad - pad // 2)
    top = len(_BAR) - 1
    return "".join(_BAR[min(top, max(0, int(round(v * top))))] for v in picked)


class ConsoleFace:
    """状态行 + 逐句口型 sparkline。不打断日志流 —— 它就是日志流的一部分。"""

    def __init__(self) -> None:
        self._levels: list[float] = []

    def on_state(self, old: State, new: State) -> None:
        face = _FACES.get(new.value)
        if face:
            logger.info("脸 | {}", face)

    def on_emotion(self, mood: str, intensity: float) -> None:
        """情绪同样要能在终端里看见。

        浏览器脸是主力，但调试时人盯着终端 —— 情绪只在浏览器里变，
        终端一点痕迹没有，就会变成"到底触发了没有"的扯皮。
        neutral 也照打：不打的话，人分不清"没触发"和"触发了但是平静"。
        """
        logger.info("心情 | {} · 强度 {:.1f}", _MOODS.get(mood, _MOODS["neutral"]), intensity)

    def on_level(self, level: float) -> None:
        # 只收集正音量；播放器每句结束会推一个 0，正好当句子边界
        if level > 0:
            self._levels.append(level)

    def on_caption(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        bar = sparkline(self._levels)
        self._levels.clear()
        logger.info("嘴 |{}| {}", bar, text)

    def attach(self, state, synthesizer) -> None:
        state.on_change(self.on_state)
        synthesizer.speaker.on_level(self.on_level)
