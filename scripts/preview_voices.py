"""把所有能用的音色各合成同一句话，存成 wav 供试听。

为什么需要它：
    "换音色"以前只能改 .env 重启、听一句、再改回来，来回很折腾。
    这个脚本一次把候选音色全跑一遍，输出到同一个目录，
    按文件名顺序听下去就行。

跑法（在项目根目录下）：
    python scripts\\preview_voices.py                 # 只跑本地已有的模型
    python scripts\\preview_voices.py --edge          # 再加上微软云端的音色（要联网）
    python scripts\\preview_voices.py --list          # 只看有哪些候选，不合成
    python scripts\\preview_voices.py --text "今天天气不错"

产物在 data\\voice_preview\\，文件名形如  local_01_xxx.wav / edge_01_xxx.mp3。
这个目录被 .gitignore 忽略，不会进仓库。

听完决定用哪个之后，改 .env 两行即可（脚本结尾会把该填的值打出来）。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from xiaoyu.config import PROJECT_ROOT, Settings
from xiaoyu.logger import get_logger
from xiaoyu.tts.engine import EdgeEngine, SherpaEngine

logger = get_logger("scripts.preview_voices")

DEFAULT_TEXT = "你好呀，我是小柚子。今天过得怎么样，要不要一起聊聊天？"

# 云端音色：在国内网络下这几个中文音色最稳、特点也最分明。
# 想拿到完整列表：python -m edge_tts --list-voices
EDGE_VOICES: list[tuple[str, str]] = [
    ("zh-CN-XiaoxiaoNeural", "女声·温柔自然（默认，最像真人）"),
    ("zh-CN-XiaoyiNeural", "女声·活泼年轻"),
    ("zh-CN-YunxiNeural", "男声·青春阳光"),
    ("zh-CN-YunjianNeural", "男声·沉稳，解说腔"),
    ("zh-CN-YunyangNeural", "男声·新闻播报腔"),
    ("zh-CN-liaoning-XiaobeiNeural", "女声·东北话"),
    ("zh-CN-shaanxi-XiaoniNeural", "女声·陕西话"),
]


def list_local_models(settings: Settings) -> list[Path]:
    """models/tts/ 下每个看起来像模型的目录。"""
    root = settings.paths.tts
    if not root.is_dir():
        return []
    return sorted(
        p for p in root.iterdir() if p.is_dir() and (p / "tokens.txt").exists()
    )


def preview_local(settings: Settings, text: str, out_dir: Path) -> list[str]:
    """本地模型逐个合成。返回生成的文件名。"""
    made: list[str] = []
    models = list_local_models(settings)
    if not models:
        logger.warning("models/tts/ 下没有可用模型，先跑 python scripts\\download_models.py")
        return made

    for index, model_dir in enumerate(models, start=1):
        name = model_dir.name
        logger.info("-" * 58)
        logger.info("【本地 {}】{}", index, name)
        # 声码器：matcha 系要配一个。放哪都行，这里按 engine 的规则让它自己找，
        # 找不到再退回"目录旁边那个"。
        vocoder = None
        sibling = sorted(
            p for p in model_dir.parent.glob("*.onnx") if "vocos" in p.name.lower()
        )
        if sibling:
            vocoder = sibling[0]

        try:
            engine = SherpaEngine(model_dir, vocoder=vocoder)
        except Exception as exc:  # noqa: BLE001
            logger.error("  加载失败：{}", exc)
            continue

        try:
            started = time.perf_counter()
            speech = engine.synthesize(text)
            cost = time.perf_counter() - started
        except Exception as exc:  # noqa: BLE001
            logger.error("  合成失败：{}", exc)
            continue

        target = out_dir / f"local_{index:02d}_{name}.wav"
        _write_wav(target, speech.samples, speech.samplerate)
        made.append(target.name)
        logger.info(
            "  -> {} | 音频 {:.2f}s | 合成 {:.2f}s | RTF {:.3f} | {} 音色",
            target.name,
            speech.duration,
            cost,
            cost / speech.duration if speech.duration else 0.0,
            engine.num_speakers,
        )
        if engine.num_speakers > 1:
            logger.info(
                "     这个模型有 {} 个音色，用 XIAOYU_TTS_SID=0~{} 逐个试",
                engine.num_speakers,
                engine.num_speakers - 1,
            )
    return made


def preview_edge(settings: Settings, text: str, out_dir: Path) -> list[str]:
    """微软云端音色逐个合成。要联网，每个音色一次握手（本来就慢）。"""
    made: list[str] = []
    for index, (voice, note) in enumerate(EDGE_VOICES, start=1):
        logger.info("-" * 58)
        logger.info("【云端 {}】{}  {}", index, voice, note)
        engine = EdgeEngine(
            voice=voice,
            tmp_dir=settings.paths.data / "tts_cache",
        )
        try:
            started = time.perf_counter()
            speech = engine.synthesize(text)
            cost = time.perf_counter() - started
        except Exception as exc:  # noqa: BLE001
            logger.error("  合成失败（多半是网络）：{}", exc)
            continue

        target = out_dir / f"edge_{index:02d}_{voice}.wav"
        _write_wav(target, speech.samples, speech.samplerate)
        made.append(target.name)
        logger.info("  -> {} | 音频 {:.2f}s | 合成 {:.2f}s", target.name, speech.duration, cost)
    return made


def _write_wav(path: Path, samples, samplerate: int) -> None:
    import numpy as np
    import soundfile as sf

    data = np.asarray(samples, dtype=np.float32)
    if data.ndim > 1:
        data = data.mean(axis=1)
    sf.write(str(path), data, samplerate)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="试听候选音色")
    parser.add_argument("--text", default=DEFAULT_TEXT, help="用来试听的那句话")
    parser.add_argument("--edge", action="store_true", help="连微软云端音色一起试（要联网）")
    parser.add_argument("--list", action="store_true", help="只列候选，不合成")
    parser.add_argument(
        "--out", default=str(PROJECT_ROOT / "data" / "voice_preview"), help="输出目录"
    )
    args = parser.parse_args(argv)

    settings = Settings.load()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.list:
        logger.info("本地模型（{} 个）：", len(list_local_models(settings)))
        for p in list_local_models(settings):
            logger.info("  {}", p.name)
        logger.info("云端音色（{} 个，需 --edge）：", len(EDGE_VOICES))
        for voice, note in EDGE_VOICES:
            logger.info("  {:32} {}", voice, note)
        return 0

    logger.info("试听文本：{}", args.text)
    logger.info("输出目录：{}", out_dir)

    made = preview_local(settings, args.text, out_dir)
    if args.edge:
        made += preview_edge(settings, args.text, out_dir)

    logger.info("=" * 58)
    if not made:
        logger.warning("一个都没生成出来，先确认模型和网络")
        return 1

    logger.info("生成 {} 个文件，去这个目录挨个听：", len(made))
    logger.info("  {}", out_dir)
    for name in made:
        logger.info("  {}", name)

    logger.info("=" * 58)
    logger.info("挑好之后改 .env（改完要重启程序）：")
    logger.info("  用本地模型： XIAOYU_TTS_ENGINE=local")
    logger.info("               XIAOYU_TTS_MODEL=<对应上面那个目录名>")
    logger.info("  用云端音色： XIAOYU_TTS_ENGINE=edge")
    logger.info("               XIAOYU_TTS_VOICE=<上面那个 zh-CN-xxx 音色名>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
