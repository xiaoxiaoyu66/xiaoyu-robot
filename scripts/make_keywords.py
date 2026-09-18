"""把中文/英文唤醒词转成 KWS 模型认识的音素格式。

用法（在项目根目录下跑）：
    python scripts\\make_keywords.py 小宇                # 只打印，看看长什么样
    python scripts\\make_keywords.py 小宇 --write         # 直接写进 config/keywords.txt
    python scripts\\make_keywords.py hey --write          # 英文也行（查模型词典）
    python scripts\\make_keywords.py 二娃 --write --threshold 3.0

为什么需要它：
    唤醒模型的词表是**音素**级的，不是汉字也不是英文单词：
        中文 -> 「声母 + 带声调韵母」，如  x iǎo y ǔ
        英文 -> CMU 音素，如  HH EY1 K IH1 M IY1
    直接写汉字或英文单词会让 sherpa-onnx 在 C++ 层报错退出，
    连 Python 异常都抓不到。所以用这个脚本生成，别手写。

    英文怎么发音，靠的是模型自带的 models/kws/en.phone
    （CMUdict 格式的发音词典，12 万条），不用额外装 g2p_en / nltk。
    词典里查不到的词（一般是自己造的名字，比如 kimi）会明确报错。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from xiaoyu.config import PROJECT_ROOT
from xiaoyu.logger import get_logger
from xiaoyu.wake.models import load_vocab

logger = get_logger("scripts.make_keywords")

DEFAULT_THRESHOLD = 2.5
DEFAULT_BOOST = 0.5

# 模型自带的英文发音词典（CMUdict 格式：单词 音素 音素 ...）
EN_PHONE = PROJECT_ROOT / "models" / "kws" / "en.phone"
_EN_TABLE: dict[str, list[str]] | None = None


def _is_english(text: str) -> bool:
    """看起来是不是英文（全 ASCII 字母 + 空格/连字符/撇号）。"""
    s = text.strip()
    return bool(s) and all(c.isascii() and (c.isalpha() or c in " -'") for c in s)


def _load_en_table() -> dict[str, list[str]]:
    """读模型自带的英文发音词典。只读一次，之后走缓存。"""
    global _EN_TABLE
    if _EN_TABLE is not None:
        return _EN_TABLE
    if not EN_PHONE.exists():
        logger.error("找不到英文发音词典 {}，英文唤醒词用不了", EN_PHONE)
        raise SystemExit(1)
    table: dict[str, list[str]] = {}
    for line in EN_PHONE.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        # 一个单词可能有多种发音，取第一条（和 CMUdict 惯例一致）
        if len(parts) >= 2:
            table.setdefault(parts[0].lower(), parts[1:])
    logger.debug("英文发音词典加载完成：{} 条", len(table))
    _EN_TABLE = table
    return table


def en_to_tokens(text: str) -> list[str]:
    """英文 -> CMU 音素序列。词典里查不到就抛 KeyError(word)。"""
    table = _load_en_table()
    tokens: list[str] = []
    for word in text.lower().split():
        phones = table.get(word)
        if not phones:
            raise KeyError(word)
        tokens.extend(phones)
    return tokens


def to_tokens(text: str) -> list[str]:
    """中文 -> 音素序列（声母 + 带声调韵母）。"""
    try:
        from pypinyin import Style, pinyin
    except ImportError:
        logger.error("缺少 pypinyin，先装：pip install pypinyin")
        raise SystemExit(1) from None

    initials = pinyin(text, style=Style.INITIALS, strict=False)
    finals = pinyin(text, style=Style.FINALS_TONE, strict=False)

    tokens: list[str] = []
    for (initial,), (final,) in zip(initials, finals):
        if initial:
            tokens.append(initial)
        if final:
            tokens.append(final)
    return tokens


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成唤醒词配置行")
    parser.add_argument("words", nargs="+", help="中文唤醒词，可以写多个")
    parser.add_argument("--write", action="store_true", help="写进 config/keywords.txt")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                        help=f"唤醒阈值，越大越不灵敏（默认 {DEFAULT_THRESHOLD}）")
    parser.add_argument("--boost", type=float, default=DEFAULT_BOOST,
                        help=f"增强系数（默认 {DEFAULT_BOOST}）")
    args = parser.parse_args(argv)

    tokens_file = PROJECT_ROOT / "models" / "kws" / "tokens.txt"
    if not tokens_file.exists():
        logger.error("找不到模型词表 {}，先跑：python scripts\\download_models.py", tokens_file)
        return 1
    vocab = load_vocab(tokens_file)

    lines: list[str] = []
    failed = False
    for word in args.words:
        if _is_english(word):
            try:
                tokens = en_to_tokens(word)
            except KeyError as exc:
                logger.error(
                    "英文「{}」不在模型词典 en.phone 里（自造名基本都查不到），"
                    "换一个常见英文词，或改用中文唤醒词",
                    exc.args[0],
                )
                failed = True
                continue
        else:
            tokens = to_tokens(word)

        missing = [t for t in tokens if t not in vocab]
        if missing:
            logger.error("「{}」里有词表没有的音素：{}", word, " ".join(missing))
            failed = True
            continue
        line = f"{' '.join(tokens)} :{args.threshold} #{args.boost} @{word}"
        logger.info("「{}」->  {}", word, line)
        lines.append(line)

    if failed:
        logger.error("有唤醒词转不出来：中文多半是多音字/生僻字，英文多半是词典里没有的词")
        return 1

    if args.write:
        target = PROJECT_ROOT / "config" / "keywords.txt"
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        logger.info("已写入 {}（{} 个唤醒词）", target, len(lines))
    else:
        logger.info("加 --write 就能直接写进 config/keywords.txt")

    return 0


if __name__ == "__main__":
    sys.exit(main())