"""延迟测量：把“反应慢”拆成能看的数字。

跑法（在项目根目录下）：
    python scripts\\bench_latency.py
    python scripts\\bench_latency.py --no-llm    # 只测本地 TTS，不联网、不花钱

为什么要有这个脚本：
    “慢”是一种感觉，感觉没法修。但拆开之后它就只是几个数字：
    录音等静音、识别、大模型首句、合成。哪一段占大头，
    就修哪一段。

    实测结论（Ryzen 5 5600H，2026-09）：
        edge-tts      首块音频 0.88~11.76 秒  <- 当时的元凶
        本地 TTS      每句 0.10~0.35 秒（RTF 0.06）
        DeepSeek      冷启动 2.0~2.3 秒 / 预热后 0.4~0.8 秒
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from xiaoyu.config import Settings
from xiaoyu.logger import get_logger, setup_logging

logger = get_logger("scripts.bench_latency")

# 长短不一的句子，看看耗时是否随长度线性增长
_SENTENCES = (
    "好的",
    "我在听呢，你慢慢说",
    "今天的天气看起来不错，要不要出去走走？",
    "我在想一个问题，如果机器人也有情绪的话，那它会因为什么事情开心呢？",
)

_QUESTIONS = ("你好呀", "今天有点累", "你觉得我现在该去写作业吗")


def bench_tts(settings: Settings) -> None:
    """测本地合成。重点看 RTF（合成耗时 / 音频时长）。

    RTF 小于 1 才意味着“说话跟得上”：
    0.06 的意思是 1 秒说话只需 0.06 秒计算，提前合成下一句绰绰有余。
    """
    from xiaoyu.tts.engine import build_engine

    logger.info("=" * 62)
    logger.info("一、本地语音合成")
    logger.info("=" * 62)

    started = time.perf_counter()
    engine = build_engine(settings)
    load = time.perf_counter() - started
    logger.info("引擎 {} | 加载耗时 {:.2f} 秒（启动时一次性，不算在回复里）", engine.name, load)

    for text in _SENTENCES:
        times = []
        speech = None
        for _ in range(3):
            mark = time.perf_counter()
            speech = engine.synthesize(text)
            times.append(time.perf_counter() - mark)
        best = min(times)
        logger.info(
            "  {:>2} 字 -> 音频 {:>4.2f} 秒 | 合成 {:.3f} 秒 | RTF {:.3f}",
            len(text), speech.duration, best, best / speech.duration,
        )


def bench_llm(settings: Settings) -> None:
    """测大模型首句。关键是对比“冷”和“热”。"""
    from xiaoyu.llm.client import DeepSeekClient

    logger.info("=" * 62)
    logger.info("二、对话模型首句")
    logger.info("=" * 62)

    client = DeepSeekClient(settings)

    # 先不预热直接问：这就是“没做优化之前”的体验
    cold = _first_sentence(client, _QUESTIONS[0])
    logger.info("冷连接首句（未预热）      {:.2f} 秒", cold)

    warmup_cost = client.warmup()
    logger.info("预热本身耗时            {:.2f} 秒（发生在启动阶段）", warmup_cost)

    warmed = [_first_sentence(client, q) for q in _QUESTIONS]
    logger.info(
        "预热后首句              {:.2f} 秒（三次：{}）",
        statistics.median(warmed), " / ".join(f"{t:.2f}" for t in warmed),
    )
    logger.info(
        "结论：预热省下约 {:.2f} 秒 —— 这就是“第一句话”最难受的那段",
        cold - statistics.median(warmed),
    )


def _first_sentence(client, question: str) -> float:
    """问一句，返回第一句回答到手的耗时。"""
    mark = time.perf_counter()
    for _ in client.stream_reply(question):
        return time.perf_counter() - mark
    return float("nan")


def main() -> int:
    parser = argparse.ArgumentParser(description="小雨机器人延迟测量")
    parser.add_argument("--no-llm", action="store_true", help="只测本地 TTS，不调大模型")
    args = parser.parse_args()

    settings = Settings.load()
    setup_logging()

    logger.info("延迟测量开始（请把电脑插上电源，省电模式会把 CPU 降频，数字会难看）")

    bench_tts(settings)

    if args.no_llm:
        logger.info("已跳过大模型部分（--no-llm）")
        return 0

    bench_llm(settings)
    logger.info("=" * 62)
    logger.info("一轮完整对话的预算：静音等待 0.5 + 识别 ~0.3 + 首句 ~0.6 + 合成 ~0.2 = 1.6 秒左右")
    logger.info("=" * 62)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
