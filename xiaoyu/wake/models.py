"""唤醒模型的选型与唤醒词文件校验。

这个模块只用标准库 —— 不 import numpy / sherpa_onnx / sounddevice。
好处是：可以被零依赖地测试，也能在环境没装全时安全使用。
"""

from __future__ import annotations

from pathlib import Path

from ..logger import get_logger

logger = get_logger(__name__)

_PREFIXES = ("encoder", "decoder", "joiner")


def pick_model_triple(directory: Path) -> tuple[Path, Path, Path]:
    """挑一组配套的 encoder / decoder / joiner。

    官方压缩包里同时放了 int8 和非 int8 的变体，而且**有的变体缺件**：
    int8 只有 encoder 和 joiner，没有 decoder。
    如果按文件名排序随手取第一个，就会拼出「int8 encoder + 非 int8 decoder」
    这种混搭组合，加载直接失败。

    所以规则是：只认后缀一致、三件齐全的那一组；优先 int8（小且快）。

    返回 (encoder, decoder, joiner)。
    """
    variants: dict[str, dict[str, Path]] = {}
    for prefix in _PREFIXES:
        for path in sorted(directory.glob(f"{prefix}*.onnx")):
            key = path.name[len(prefix) :]     # 例如 -epoch-13-...-16-left-64.onnx
            variants.setdefault(key, {})[prefix] = path

    complete = [(key, parts) for key, parts in variants.items() if len(parts) == len(_PREFIXES)]
    if not complete:
        raise FileNotFoundError(
            f"{directory} 里找不到配套的 encoder/decoder/joiner，"
            "请重新跑 python scripts\\download_models.py"
        )

    complete.sort(key=lambda item: (".int8." not in item[0], item[0]))
    key, parts = complete[0]
    logger.debug("选用唤醒模型组合 {}", key)
    return parts["encoder"], parts["decoder"], parts["joiner"]


def load_vocab(tokens_file: Path) -> set[str]:
    """读取模型词表（音素集合）。"""
    vocab: set[str] = set()
    for line in tokens_file.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if parts:
            vocab.add(parts[0])
    return vocab


def parse_keyword_line(line: str) -> tuple[list[str], str]:
    """拆一行唤醒词配置，返回 (音素列表, 显示名)。

    格式：  音素序列 :阈值 #增强 @显示名
    例如：  x iǎo y ǔ :2.5 #0.5 @小宇
    """
    head = line.split("@")[0]              # 去掉 @显示名
    spec = head.split(":")[0].strip()      # 去掉 :阈值 部分
    name = line.split("@")[-1].strip() if "@" in line else spec
    return spec.split(), name


def prepare_keywords_file(
    source: Path, vocab: set[str], out_dir: Path
) -> tuple[Path, list[str]]:
    """校验唤醒词文件，并生成一份"干净"的副本给模型用。

    做两件事：
        1. 逐行检查音素在不在词表里 —— 不在就报错说清楚是哪一行、哪个音素。
           必须在这里拦住，因为把汉字直接喂给 sherpa-onnx 会让它在 C++ 层
           报错退出，连 Python 异常都抓不到。
        2. 去掉空行和注释行。

    返回 (干净文件路径, 唤醒词显示名列表)。
    """
    if not source.exists():
        raise FileNotFoundError(
            f"唤醒词文件不存在：{source}\n"
            "先用脚本生成：python scripts\\make_keywords.py 小宇 --write"
        )

    kept: list[str] = []
    names: list[str] = []
    for lineno, raw in enumerate(source.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        tokens, name = parse_keyword_line(line)
        if not tokens:
            raise ValueError(f"keywords.txt 第 {lineno} 行没有音素：{line}")

        missing = [t for t in tokens if t not in vocab]
        if missing:
            raise ValueError(
                f"keywords.txt 第 {lineno} 行的音素不在模型词表里：{' '.join(missing)}\n"
                f"  这一行是：{line}\n"
                "  中文唤醒词必须写成「声母 + 带声调韵母」，不能直接写汉字。\n"
                "  别手写拼音，用脚本生成：python scripts\\make_keywords.py 小宇 --write"
            )

        kept.append(line)
        names.append(name)

    if not kept:
        raise ValueError(f"唤醒词文件里没有有效内容：{source}")

    out_dir.mkdir(parents=True, exist_ok=True)
    cleaned = out_dir / "keywords_active.txt"
    cleaned.write_text("\n".join(kept) + "\n", encoding="utf-8")
    return cleaned, names