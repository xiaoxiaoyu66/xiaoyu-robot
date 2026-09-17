"""把中文唤醒词转成 KWS 模型认识的音素格式。

用法（在项目根目录下跑）：
    python scripts\\make_keywords.py 小宇                # 只打印，看看长什么样
    python scripts\\make_keywords.py 小宇 --write         # 直接写进 config/keywords.txt
    python scripts\\make_keywords.py 二娃 --write --threshold 3.0

为什么需要它：
    唤醒模型的词表是**音素**级的，中文用「声母 + 带声调韵母」表示。
    直接写汉字会让 sherpa-onnx 在 C++ 层报错退出，连 Python 异常都抓不到。
    所以别手写拼音，用这个脚本生成。
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
        logger.error("有唤醒词转不出来，可能是多音字或生僻字，换一个词试试")
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