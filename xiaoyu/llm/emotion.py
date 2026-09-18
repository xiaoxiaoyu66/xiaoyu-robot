"""情绪标记的剥离与解析（情绪系统）——纯逻辑，可单测。

约定：模型在每轮回复的**最后**输出一个情绪标记，例如

    今天我也好开心呀！[emotion]happy,0.8[/emotion]

这样设计的原因：
    - 零额外延迟、零额外调用 —— 标记跟着流式正文一起到，不占用首句时间
    - 正文和情绪同一次生成，语义必然一致
    - 标记在结尾，正文先切句先出声，情绪解析完时话差不多也快说完了，
      脸刚好来得及在"说完前后"变表情

剥离是增量的（流式场景用）：任何时候文本里出现完整的标记就摘掉；
结尾残缺的半个标记（"[emotion]hap…"）先按住不显示，等它长完整或流结束。
解析不出来一律 neutral —— 情绪是表演，不是主流程，绝不能带崩对话。
"""

from __future__ import annotations

import re

VALID_MOODS = frozenset({"happy", "sad", "angry", "surprised", "neutral"})

_OPEN = "[emotion]"
_MARK_RE = re.compile(r"\[emotion\]([^\[]*?)\[/emotion\]")
_OPEN_RE = re.compile(re.escape(_OPEN))


def _holdback_index(text: str) -> int | None:
    """尾巴该从哪个下标开始按住？没有就返回 None。

    两种尾巴都算：
      1. 开标记已经完整出现、但还没等到 [/emotion]；
      2. 结尾处一个**可能正在长成开标记的残片** —— "[", "[e", "[em", ...

    第 2 种是关键，别删。流式里 token 边界会切在 "[emotion]" 中间
    （"[", "emotion", "]" 这种切法很常见），只认完整的开标记就会把那个 "["
    当成正文发出去，接着 "emotion]happy,0.9[/emotion]" 也就跟着漏了。
    实测逐字喂必现，真 DeepSeek 冒烟也复现过 —— 标记会被念出来、上字幕、
    还会写进历史。
    """
    match = _OPEN_RE.search(text)
    if match:
        return match.start()
    # 只看结尾最多 len(_OPEN)-1 个字符，再长就不可能是开标记的前缀了
    for i in range(len(text) - 1, max(-1, len(text) - len(_OPEN)), -1):
        if _OPEN.startswith(text[i:]):
            return i
    return None


def split_visible(text: str) -> tuple[str, str]:
    """把文本拆成（可以显示的干净文本, 暂时按住不显示的尾巴）。

    完整标记从显示文本里摘掉；结尾那段还没长完的标记（哪怕只来了一个 "["）
    连同它后面的内容一起进尾巴，等更多字符来了再重新拆。
    调用方负责在流结束时把尾巴丢掉 —— 那时候它已经不是正常文本了。
    """
    visible = _MARK_RE.sub("", text)
    index = _holdback_index(visible)
    if index is None:
        return visible, ""
    return visible[:index], visible[index:]


def has_emotion_mark(text: str) -> bool:
    """文本里有没有**完整**的情绪标记。

    给日志用的。模型有时候就是不给标记（短回复尤其常见），那是正常的，
    不该和"给了但值不认识"混为一谈 —— 以前两者都静默归成 neutral，
    日志里看不出任何区别，人在终端前只能干着急（用户 2026-09-18 就卡在这）。
    """
    return _MARK_RE.search(text) is not None


def parse_emotion(text: str) -> tuple[str, float]:
    """从（可能含标记的）文本里解析出 (mood, intensity)。

    取第一个完整标记。解析失败 / 强度越界 /  mood 不认识 -> neutral, 0.5。
    """
    match = _MARK_RE.search(text)
    if not match:
        return "neutral", 0.5
    raw = match.group(1).strip()
    mood, _, num = raw.partition(",")
    mood = mood.strip().lower()
    if mood not in VALID_MOODS:
        return "neutral", 0.5
    try:
        intensity = max(0.0, min(1.0, float(num.strip())))
    except ValueError:
        intensity = 0.5
    return mood, intensity
