"""主控：把各个模块串起来。

两种跑法：
    python -m xiaoyu --check    只做环境自检，不启动硬件
    python -m xiaoyu --text     键盘模式：打字 -> 它出声（不需要麦克风，调试最快）
    python -m xiaoyu            完整模式：唤醒 -> 说话 -> 回答

关于"半双工"（重要）：
    这个循环是严格串行的 —— 听唤醒词时开麦，说话时就关麦，
    所以它不会听到自己的声音、不会自己唤醒自己。
    等以后要做"打断"功能时，再单独处理回声问题。
"""

from __future__ import annotations

import argparse
import platform
import sys

from . import __version__
from .config import CheckItem, Settings
from .logger import get_logger, setup_logging
from .state import State, StateMachine
from .text import sanitize

logger = get_logger(__name__)

# 一次唤醒后最多能连续聊几轮，不说话就回去待机
FOLLOW_UP_ROUNDS = 3


def log_banner(settings: Settings) -> None:
    logger.info("=" * 62)
    logger.info("  XiaoYu Robot v{}  —— 桌面陪伴机器人", __version__)
    logger.info("=" * 62)
    logger.info("Python {} | {} | 项目根目录 {}", platform.python_version(), platform.system(), settings.paths.root)
    logger.info("日志级别 {}（改 .env 里的 XIAOYU_LOG_LEVEL）", _level())
    logger.info("-" * 62)


def _level() -> str:
    import os

    return os.environ.get("XIAOYU_LOG_LEVEL", "INFO").upper()


def log_diagnose(settings: Settings) -> bool:
    """打印环境自检清单，返回是否全部通过。"""
    logger.info("环境自检：")
    items: list[CheckItem] = settings.diagnose()
    for item in items:
        if item.ok:
            logger.info("  {}", item.render())
        else:
            logger.warning("  {}", item.render())

    failed = [i for i in items if not i.ok]
    hard_required = {"Python 版本", "依赖 loguru", "依赖 sounddevice", "依赖 numpy"}
    critical = [i for i in failed if i.name in hard_required]

    logger.info("-" * 62)
    if not failed:
        logger.info("自检通过，全部就绪。按提示继续下一步。")
        return True
    if critical:
        logger.error("缺少必需组件：{}", ", ".join(i.name for i in critical))
        logger.error("先装依赖：pip install -r requirements.txt")
        return False

    logger.warning("有 {} 项待补齐，但不影响先跑起来：", len(failed))
    for item in failed:
        hint = _next_step_hint(item.name)
        if hint:
            logger.warning("  · {} -> {}", item.name, hint)
    return True


def _next_step_hint(name: str) -> str:
    if name.startswith("依赖 sherpa_onnx"):
        return "pip install sherpa-onnx"
    if name.startswith("依赖 edge_tts"):
        return "pip install edge-tts"
    if name.startswith("依赖 soundfile"):
        return "pip install soundfile"
    if name.startswith("依赖 openai"):
        return "pip install openai"
    if name.startswith("依赖 sentence_transformers"):
        return "S4 阶段再装：pip install sentence-transformers"
    if name in {"唤醒模型", "识别模型", "VAD 模型"}:
        return "python scripts\\download_models.py"
    if name == "唤醒词文件":
        return "新建 config\\keywords.txt，写：小宇 :2.5 #0.5 @小宇"
    if name == "性格文件":
        return "新建 config\\persona.md，写它的人设"
    if name == "DeepSeek API Key":
        return "复制 .env.example 为 .env，填入 DEEPSEEK_API_KEY"
    return ""


def run_text_mode(settings: Settings) -> None:
    """键盘模式：不用麦克风，直接打字验证"大脑 + 嘴"这一段。"""
    from .llm.client import DeepSeekClient
    from .memory.store import MemoryStore
    from .tts.synthesizer import Synthesizer

    logger.info("键盘模式：直接打字聊天，输入 退出 / exit 结束")
    memory = MemoryStore(settings)
    synthesizer = Synthesizer(settings)
    client = DeepSeekClient(settings, memory=memory)

    state = StateMachine()
    try:
        while True:
            try:
                text = sanitize(input("你说 > ").strip())
            except (EOFError, KeyboardInterrupt):
                logger.info("收到退出信号")
                break
            if text.lower() in {"exit", "quit", "退出", "bye"}:
                break
            if not text:
                continue

            state.set(State.THINKING, "键盘输入")
            state.set(State.SPEAKING, "开始回答")
            try:
                synthesizer.speak_stream(client.stream_reply(text))
            except Exception:
                logger.exception("这一轮处理失败，继续下一轮")
            finally:
                state.set(State.IDLE)
    finally:
        memory.close()
        logger.info("对话结束，再见")


def run_voice_loop(settings: Settings) -> None:
    """完整链路：唤醒 -> 录音 -> 识别 -> 回答 -> 出声。"""
    import numpy as np

    from .asr.recognizer import SpeechRecognizer
    from .audio.recorder import Recorder
    from .llm.client import DeepSeekClient
    from .memory.store import MemoryStore
    from .tts.synthesizer import Synthesizer
    from .wake.kws import WakeWordDetector

    state = StateMachine()
    memory = MemoryStore(settings)
    recorder = Recorder(settings.audio)
    recognizer = SpeechRecognizer(settings)
    synthesizer = Synthesizer(settings)
    client = DeepSeekClient(settings, memory=memory)
    detector = WakeWordDetector(settings)

    logger.info("全部就绪，开始待机。按 Ctrl+C 退出。")
    try:
        while True:
            state.set(State.IDLE, "等待唤醒")
            detector.listen_once()

            for round_index in range(1, FOLLOW_UP_ROUNDS + 1):
                state.set(State.LISTENING, f"第 {round_index} 轮")
                audio = recorder.record_until_silence()
                if audio.size == 0 or float(np.abs(audio).max()) < 1e-4:
                    logger.info("没有听到内容，回去待机")
                    break

                state.set(State.THINKING, "识别中")
                text = recognizer.transcribe(audio)
                if not text:
                    logger.info("没听清，回去待机")
                    break

                state.set(State.SPEAKING, "开始回答")
                try:
                    synthesizer.speak_stream(client.stream_reply(text))
                except Exception:
                    logger.exception("回答失败，回到待机")
                    break

            state.set(State.IDLE, "本轮结束")
    except KeyboardInterrupt:
        logger.info("收到 Ctrl+C，退出")
    finally:
        memory.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="xiaoyu", description="XiaoYu Robot 桌面陪伴机器人")
    parser.add_argument("--check", action="store_true", help="只做环境自检，不启动硬件")
    parser.add_argument("--text", action="store_true", help="键盘模式（不需要麦克风）")
    parser.add_argument("--log-level", default=None, help="覆盖日志级别，如 DEBUG")
    args = parser.parse_args(argv)

    settings = Settings.load()
    setup_logging(level=args.log_level)
    log_banner(settings)

    ready = log_diagnose(settings)
    if args.check:
        return 0 if ready else 1
    if not ready:
        logger.error("环境没准备好，先按上面的提示补齐，然后再跑。")
        return 1

    try:
        if args.text:
            run_text_mode(settings)
        else:
            run_voice_loop(settings)
    except Exception:
        logger.exception("运行出错")
        return 1
    return 0