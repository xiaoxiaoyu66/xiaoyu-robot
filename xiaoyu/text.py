"""纯文本工具。

这个模块刻意不 import 项目里任何东西，也不依赖第三方库 ——
所以它可以被单独测试，也可以在环境没装全时安全使用。
"""

from __future__ import annotations

from pathlib import Path

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

# ---------- 编码兜底 ----------

# 读配置文件时按这个顺序试。
# utf-8-sig 排第一：它和 utf-8 完全兼容，还能顺手吃掉记事本留下的 BOM。
_TEXT_ENCODINGS = ("utf-8-sig", "gbk", "utf-16")


def sanitize(text: str) -> str:
    """把无法编码的字符换成可读的转义写法。

    从管道、文件、系统 API 拿到的字符串里可能出现"孤立代理字符"
    （解码失败留下的残渣，长得像 '\udc80'）。这种东西一旦流进 json 编码
    或日志落盘，就会抛 UnicodeEncodeError，把整条链路炸掉 ——
    实测踩过：一句坏文本能让日志文件写入失败，甚至整个对话回合挂掉。

    统一在入口处换成 '\udc80' 这种字面写法，后面怎么编码都不会炸，
    而且看日志时一眼就知道是编码坏了，不是内容本身长这样。

    >>> sanitize("正常文本")
    '正常文本'
    """
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return text.encode("utf-8", "backslashreplace").decode("utf-8")
    return text


def read_text(path: str | Path) -> str:
    """读文本文件，自动识别编码。

    为什么不能直接 read_text(encoding="utf-8")：
        中文 Windows 上，记事本和不少编辑器"另存为 ANSI"存出来的是 GBK。
        配置文件（persona.md、keywords.txt）被存成 GBK 太常见了，
        写死 utf-8 会直接抛 UnicodeDecodeError，报错信息对新手完全看不懂。

    依次尝试 utf-8-sig -> gbk -> utf-16，都不行就用 utf-8 强行解码
    （坏字节变成替换符），至少程序还能跑起来、还能看日志。
    """
    raw = Path(path).expanduser().read_bytes()
    for encoding in _TEXT_ENCODINGS:
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")
