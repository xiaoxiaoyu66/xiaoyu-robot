"""纯文本工具。

这个模块刻意不 import 项目里任何东西，也不依赖第三方库 ——
所以它可以被单独测试，也可以在环境没装全时安全使用。
"""

from __future__ import annotations

# 句末标点：见到这些就断句送去念
HARD_ENDERS = "。！？!?…\n"
# 软断点：一句话太长时在这里断开，降低"首句出声"的等待
SOFT_BREAKS = "，,、；;：:"
# 缓冲区超过这个长度就必须切一刀，否则延迟不可控
MAX_BUFFER = 50


def split_sentences(buffer: str, max_buffer: int = MAX_BUFFER) -> tuple[list[str], str]:
    """从流式缓冲区里切出可以朗读的句子。

    返回 (切好的句子列表, 剩下的尾巴)。

    切分优先级：
        1. 句末标点（。。！？!?…换行）—— 最自然，切完就送去合成
        2. 软断点（，、；：）—— 一句话太长时在这里断开
        3. 都没有就硬切 —— 宁可念得断一点，也不能让缓冲无限累积

    第 3 条很重要：模型偶尔会吐出一长串没有标点的内容，
    如果不在长度上兜底，首句就永远出不来。

    >>> split_sentences("你好。今天不错。")
    (['你好。', '今天不错。'], '')
    >>> split_sentences("还没说完")
    ([], '还没说完')
    """
    sentences: list[str] = []
    while True:
        cut = -1

        # 1) 优先找句末标点
        for index, char in enumerate(buffer):
            if char in HARD_ENDERS:
                cut = index
                break

        # 2) 太长了就在软断点切开
        if cut < 0 and len(buffer) >= max_buffer:
            window = min(len(buffer), max_buffer + 10)
            for index in range(window - 1, -1, -1):
                if buffer[index] in SOFT_BREAKS:
                    cut = index
                    break
            # 3) 连软断点都没有，硬切
            if cut < 0:
                cut = window - 1

        if cut < 0:
            break

        sentence = buffer[: cut + 1].strip()
        buffer = buffer[cut + 1 :]
        if sentence:
            sentences.append(sentence)
    return sentences, buffer


def clean_for_speech(text: str) -> str:
    """去掉不该念出来的东西（markdown 符号、列表前缀等）。"""
    cleaned = text.strip()
    for prefix in ("- ", "* ", "#", "> "):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix) :].strip()
    return cleaned