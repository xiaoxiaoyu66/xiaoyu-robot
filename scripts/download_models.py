"""下载运行所需的模型。

跑法（在项目根目录下）：
    python scripts\\download_models.py

总共约 200MB，都是官方发布地址，不需要翻墙也不需要注册。

下载慢 / 失败时的办法：
    python scripts\\download_models.py --prefix https://ghfast.top/
    （--prefix 会在原始地址前面加一段代理前缀，换几个镜像试试）
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tarfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from xiaoyu.config import PROJECT_ROOT
from xiaoyu.logger import get_logger

logger = get_logger("scripts.download_models")

_RELEASE = "https://github.com/k2-fsa/sherpa-onnx/releases/download"


@dataclass(frozen=True)
class ModelItem:
    name: str
    url: str
    dest: Path
    archive: bool          # True = tar.bz2 压缩包，False = 单个文件
    hint: str              # 已存在时用来判断"下没下过"的标志


MODELS: list[ModelItem] = [
    ModelItem(
        name="唤醒模型（中文关键词检测）",
        url=f"{_RELEASE}/kws-models/sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20.tar.bz2",
        dest=PROJECT_ROOT / "models" / "kws",
        archive=True,
        hint="encoder*.onnx",
    ),
    ModelItem(
        name="识别模型（SenseVoice int8）",
        url=f"{_RELEASE}/asr-models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09.tar.bz2",
        dest=PROJECT_ROOT / "models" / "sense-voice",
        archive=True,
        hint="model*.onnx",
    ),
    ModelItem(
        name="断句模型（silero VAD）",
        url=f"{_RELEASE}/asr-models/silero_vad.onnx",
        dest=PROJECT_ROOT / "models" / "silero_vad.onnx",
        archive=False,
        hint="",
    ),
    ModelItem(
        name="语音合成模型（Matcha 中文，默认）",
        url=f"{_RELEASE}/tts-models/matcha-icefall-zh-baker.tar.bz2",
        dest=PROJECT_ROOT / "models" / "tts" / "matcha-icefall-zh-baker",
        archive=True,
        hint="*.onnx",
    ),
    ModelItem(
        name="声码器（Matcha 必备，单独一个 release）",
        url=f"{_RELEASE}/vocoder-models/vocos-22khz-univ.onnx",
        dest=PROJECT_ROOT / "models" / "tts" / "vocos-22khz-univ.onnx",
        archive=False,
        hint="",
    ),
    ModelItem(
        name="语音合成模型（Piper 中文女声，更快的备选）",
        url=f"{_RELEASE}/tts-models/vits-piper-zh_CN-huayan-medium.tar.bz2",
        dest=PROJECT_ROOT / "models" / "tts" / "vits-piper-zh_CN-huayan-medium",
        archive=True,
        hint="*.onnx",
    ),
]


def _already_there(item: ModelItem) -> bool:
    if item.archive:
        return any(item.dest.glob(item.hint))
    return item.dest.exists()


def _download(url: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    logger.info("开始下载 {}", url.rsplit("/", 1)[-1])
    started = time.perf_counter()
    last_report = -1.0

    # 必须带 User-Agent：国内几个 GitHub 加速镜像会直接拒掉没有 UA 的请求，
    # 表现出来就是"连接建立了但永远不下数据"。
    request = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (XiaoYu Robot model downloader)"}
    )
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
        total = int(response.headers.get("Content-Length") or 0)
        downloaded = 0
        with open(target, "wb") as fh:
            while True:
                chunk = response.read(1024 * 256)
                if not chunk:
                    break
                fh.write(chunk)
                downloaded += len(chunk)
                if total:
                    percent = downloaded * 100 / total
                    if percent - last_report >= 10:
                        last_report = percent
                        logger.info(
                            "  进度 {:.0f}%  ({:.1f} MB / {:.1f} MB)",
                            percent,
                            downloaded / 1048576,
                            total / 1048576,
                        )

    logger.info(
        "下载完成 {:.1f} MB，用时 {:.1f} 秒",
        target.stat().st_size / 1048576,
        time.perf_counter() - started,
    )


def _extract(archive: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    logger.info("解压 {}", archive.name)
    with tarfile.open(archive, "r:bz2") as tar:
        try:
            tar.extractall(path=dest, filter="data")   # Python 3.12+
        except TypeError:
            tar.extractall(path=dest)

    # 压缩包里通常套了一层同名目录，把它的内容提上来
    subdirs = [p for p in dest.iterdir() if p.is_dir()]
    if len(subdirs) == 1:
        inner = subdirs[0]
        for item in inner.iterdir():
            shutil.move(str(item), str(dest / item.name))
        inner.rmdir()
        logger.debug("已把 {} 的内容提到 {}", inner.name, dest.name)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="下载 XiaoYu Robot 需要的模型")
    parser.add_argument("--prefix", default="", help="在下载地址前加的代理前缀，用于加速")
    parser.add_argument("--force", action="store_true", help="已存在也重新下载")
    parser.add_argument(
        "--only",
        default="",
        help="只处理名字或网址里含这个字串的模型（调试用）",
    )
    args = parser.parse_args(argv)

    logger.info("=" * 58)
    logger.info("模型下载 —— 目标目录 {}", PROJECT_ROOT / "models")
    logger.info("=" * 58)

    failed: list[str] = []
    for item in MODELS:
        if args.only and args.only not in item.name and args.only not in item.url:
            continue
        logger.info("-" * 58)
        logger.info("【{}】", item.name)

        if _already_there(item) and not args.force:
            logger.info("  已存在，跳过（要重下加 --force）")
            continue

        url = f"{args.prefix}{item.url}"
        try:
            if item.archive:
                tmp = PROJECT_ROOT / "models" / f"_{item.dest.name}.tar.bz2"
                _download(url, tmp)
                _extract(tmp, item.dest)
                tmp.unlink(missing_ok=True)
            else:
                _download(url, item.dest)
        except (urllib.error.URLError, OSError, tarfile.TarError) as exc:
            logger.error("  失败：{}", exc)
            failed.append(item.name)

    logger.info("=" * 58)
    if failed:
        logger.error("有 {} 项没下成功：{}", len(failed), "、".join(failed))
        logger.error("试试：python scripts\\download_models.py --prefix https://ghfast.top/")
        return 1

    logger.info("全部就绪。下一步：python -m xiaoyu --check")
    return 0


if __name__ == "__main__":
    sys.exit(main())