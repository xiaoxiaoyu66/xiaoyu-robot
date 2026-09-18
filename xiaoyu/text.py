"""纯文本工具。

这个模块刻意不 import 项目里任何东西，也不依赖第三方库 ——
所以它可以被单独测试，也可以在环境没装全时安全使用。
"""

from __future__ import annotations

from datetime import datetime
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


# ---------- 时间感 ----------
#
# 放在这里的理由和 read_text / sanitize 一样：纯函数、零依赖、能单独测。
# 记忆库里存的是 "YYYY-MM-DD HH:MM:SS" 字符串，要变成"昨天 21:30"
# 这种能直接塞进提示词的人话。写错一个日期，机器人就会一本正经地胡说八道。

_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

# 记忆库里的时间戳格式（memory/store.py 的 _now() 写的就是第一个）
_TIMESTAMP_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d")


def parse_timestamp(value: str | None) -> datetime | None:
    """解析记忆库里的时间戳，认不出来就返回 None（不要抛异常）。"""
    if not value:
        return None
    text = str(value).strip()
    for fmt in _TIMESTAMP_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def describe_now(now: datetime | None = None) -> str:
    """把"现在"说成人话，例如 2026-09-18 周五 14:19。

    必须每轮现取，不能启动时算一次存着 —— 聊一晚上会一直报错时间。
    """
    now = now or datetime.now()
    return f"{now:%Y-%m-%d} {_WEEKDAYS[now.weekday()]} {now:%H:%M}"


def describe_last_seen(value: str | None, now: datetime | None = None) -> str:
    """把"上次聊天的时间"说成人话。

    为什么值得单独写一个函数：
        机器人要说出"上次你不是说在忙作业吗"，前提是它知道自己和主人
        隔了多久没说话。直接甩一个 "2026-09-17 21:30" 给模型，
        它经常会当成"现在"；说成"昨天 21:30"就稳了。

    解析失败、没传值 —— 一律返回空串，让调用方跳过这一段，
    绝不能让一个坏时间戳把整轮对话带崩。

    >>> from datetime import datetime
    >>> describe_last_seen("2026-09-17 21:30:00", datetime(2026, 9, 18, 14, 19))
    '昨天 21:30'
    """
    stamp = parse_timestamp(value)
    if stamp is None:
        return ""

    now = now or datetime.now()
    days = (now.date() - stamp.date()).days
    clock = stamp.strftime("%H:%M")

    if days < 0:
        # 时钟被改过（或者数据是未来的），不猜，原样说
        return stamp.strftime("%Y-%m-%d %H:%M")
    if days == 0:
        minutes = int((now - stamp).total_seconds() // 60)
        if minutes < 2:
            return "刚才"
        if minutes < 60:
            return f"{minutes} 分钟前"
        return f"今天 {clock}"
    if days == 1:
        return f"昨天 {clock}"
    if days < 7:
        return f"{days} 天前"
    if days < 30:
        return f"大概 {days // 7} 周前"
    return stamp.strftime("%Y-%m-%d")

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
