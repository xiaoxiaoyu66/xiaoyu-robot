"""主控：把各个模块串起来。

两种跑法：
    python -m xiaoyu --check    只做环境自检，不启动硬件
    python -m xiaoyu --text     键盘模式：打字 -> 它出声（不需要麦克风，调试最快）
    python -m xiaoyu            完整模式：唤醒 -> 说话 -> 回答

关于"半双工"（重要）：
    这个循环是严格串行的 —— 听唤醒词时开麦，说话时就关麦，
    所以它不会听到自己的声音、不会自己唤醒自己。
    S5 已实现"触屏打断"（点脸即停，走 WebSocket，不涉及回声）；
    语音打断（说话时喊唤醒词）要先解决回声问题，以后再说。
"""

from __future__ import annotations

import argparse
import platform
import sys
import time

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
        # 注意别在这里教用户直接写汉字 —— 汉字会让 sherpa-onnx 在 C++ 层崩掉，
        # 连 Python 异常都抓不到。只能让他们跑脚本生成。
        return "跑脚本生成：python scripts\\make_keywords.py 小柚子 --write"
    if name == "性格文件":
        return "新建 config\\persona.md，写它的人设"
    if name == "DeepSeek API Key":
        return "复制 .env.example 为 .env，填入 DEEPSEEK_API_KEY"
    if name == "语音合成模型":
        return "python scripts\\download_models.py --prefix https://gh-proxy.com/"
    return ""


def attach_face(settings: Settings, state: StateMachine, synthesizer):
    """把表情脸接上：状态 -> 广播，音量 -> 口型，点脸 -> 打断。

    返回 None 表示没启用（或 websockets 没装 / 服务起不来）——
    脸永远是可选件，任何一步失败都不影响语音主流程。
    键盘模式和语音模式都能用，所以先在 --text 里把脸调通再上语音。
    """
    if not settings.face.enabled:
        return None
    try:
        from .face.server import FaceServer
    except Exception:
        logger.exception("表情脸模块加载失败，本次不启用")
        return None

    face = FaceServer(settings.face)
    face.on_interrupt = synthesizer.interrupt
    try:
        face.start()
    except Exception:
        logger.exception("表情脸服务启动失败，本次不启用")
        return None

    state.on_change(lambda old, new: face.publish_state(new.value))
    synthesizer.speaker.on_level(face.publish_mouth)
    logger.info("表情脸已接上：状态 -> 广播 | 音量 -> 口型 | 点脸 -> 打断")
    return face


def attach_console_face(settings: Settings, state: StateMachine, synthesizer):
    """控制台表情脸：脸在浏览器里够不着的时候，终端里有个字符画分身。

    和 WebSocket 脸互不依赖 —— 浏览器脸没开 / websockets 没装，
    这里照样工作（它只订阅状态机和播放器的本机事件）。
    """
    if not settings.face.enabled or not settings.face.console:
        return None
    from .face.console import ConsoleFace

    console_face = ConsoleFace()
    console_face.attach(state, synthesizer)
    logger.info("控制台表情脸已启用（不想要就设 XIAOYU_FACE_CONSOLE=0）")
    return console_face


def attach_vision(settings: Settings, state: StateMachine, face) -> None:
    """眼睛跟随（S6a）：摄像头读人头位置 -> 脸的眼睛盯着人看。

    只在待机 / 听话时跑（说话时眼睛忙，不抢 CPU）；
    依赖没装、摄像头打不开、脸没启用 —— 一律静默不启用。
    """
    if not settings.vision.enabled or face is None:
        return None
    try:
        from .vision.tracker import GazeTracker
    except Exception:
        logger.exception("眼睛跟随模块加载失败，本次不启用")
        return None

    tracker = GazeTracker(settings.vision)
    tracker.on_gaze = face.publish_gaze
    try:
        if not tracker.start():
            return None
    except Exception:
        logger.exception("眼睛跟随启动失败，本次不启用")
        return None

    # 只在 IDLE / LISTENING 时看；状态机里先置一次当前状态
    state.on_change(
        lambda old, new: tracker.set_active(new in (State.IDLE, State.LISTENING))
    )
    tracker.set_active(True)  # 主循环起手就是 IDLE
    logger.info("眼睛跟随已接上：待机/听话时盯着人看，说话时休息")
    return tracker


def _caption_emitter(*handlers):
    """把多个字幕接收方（浏览器脸 + 控制台脸）合成一个回调。"""
    handlers = [h for h in handlers if h is not None]
    if not handlers:
        return None

    def emit(text: str) -> None:
        for handler in handlers:
            try:
                handler(text)
            except Exception:
                logger.exception("字幕接收方执行失败")

    return emit


def run_text_mode(settings: Settings) -> None:
    """键盘模式：不用麦克风，直接打字验证"大脑 + 嘴"这一段。"""
    from .llm.client import DeepSeekClient
    from .memory.store import MemoryStore
    from .tts.synthesizer import Synthesizer

    logger.info("键盘模式：直接打字聊天，输入 退出 / exit 结束")
    memory = MemoryStore(settings)
    synthesizer = Synthesizer(settings)
    client = DeepSeekClient(settings, memory=memory)
    # 先把连接建好，省得第一句话白等 1.5 秒
    client.warmup()

    state = StateMachine()
    face = attach_face(settings, state, synthesizer)
    console_face = attach_console_face(settings, state, synthesizer)
    attach_vision(settings, state, face)
    on_caption = _caption_emitter(
        face.publish_caption if face else None,
        console_face.on_caption if console_face else None,
    )
    on_emotion = _caption_emitter(face.publish_emotion if face else None)
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
                synthesizer.speak_stream(
                    client.stream_reply(text, on_emotion=on_emotion),
                    on_sentence=on_caption,
                )
                if face is not None and face.interrupted():
                    face.clear_interrupt()
                    logger.info("被触屏打断")
            except Exception:
                logger.exception("这一轮处理失败，继续下一轮")
            finally:
                state.set(State.IDLE)
    finally:
        memory.close()
        logger.info("对话结束，再见")


def play_cue(synthesizer, kind: str, settings: Settings) -> None:
    """播一声提示音。

    唤醒命中后要**立刻**播一声。因为从"听见唤醒词"到"它开口说话"，
    中间还要等识别 + 大模型 + 合成，实测 3 秒起步 ——
    这段时间一点动静都没有的话，人会以为它没听见，于是重复喊唤醒词。

    提示音纯属锦上添花：没配好、播不出来、设备正被占用，都不该影响主流程。
    """
    if not settings.audio.cue_enabled:
        return
    try:
        synthesizer.speaker.play_cue(kind)
    except Exception:
        logger.debug("提示音没播出来，忽略", exc_info=True)


# 主循环自愈（S5.5）：单轮异常不退出，连续失败这么多轮才放弃、
# 交回给进程守护（run_forever.cmd / systemd）重启。退出原因写进 logs\error.log。
MAX_CONSECUTIVE_FAILURES = 5


def run_voice_loop(settings: Settings) -> None:
    """完整链路：唤醒 -> 录音 -> 识别 -> 回答 -> 出声。"""
    from .asr.recognizer import SpeechRecognizer
    from .audio import sfx
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
    face = attach_face(settings, state, synthesizer)
    console_face = attach_console_face(settings, state, synthesizer)
    attach_vision(settings, state, face)
    on_caption = _caption_emitter(
        face.publish_caption if face else None,
        console_face.on_caption if console_face else None,
    )
    on_emotion = _caption_emitter(face.publish_emotion if face else None)

    # S5.5：开机采一段环境噪声，把静音阈值改成自适应的。
    # 笔记本和 N100 的麦克风噪声底完全不同，固定值搬过去会失效。
    recorder.calibrate_noise_floor()
    vad_model = (
        str(settings.paths.vad_model) if settings.paths.vad_model.exists() else None
    )

    # 后台把对话连接建好。待机可能几十分钟，连接早被回收了，
    # 不预热的话每次“第一句话”都要多等 1.5 秒。
    client.warmup_async()

    logger.info("全部就绪，开始待机。按 Ctrl+C 退出。")
    consecutive_failures = 0
    try:
        while True:
            try:
                _one_conversation(
                    settings, state, detector, recorder, recognizer,
                    synthesizer, client, face, on_caption, on_emotion, vad_model,
                )
                consecutive_failures = 0  # 完整跑完一轮，失败计数清零
            except KeyboardInterrupt:
                raise
            except Exception:
                # 自愈（v3 §4）：记日志 -> 等 2 秒 -> 回待机。
                # 拔插 USB 设备、网络闪断这类一次性的毛刺不该杀掉常驻进程。
                consecutive_failures += 1
                logger.exception(
                    "本轮对话异常（连续第 {} 次）", consecutive_failures
                )
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    logger.error(
                        "连续失败 {} 次，程序退出交给进程守护重启",
                        consecutive_failures,
                    )
                    _write_error_log(
                        settings,
                        f"连续 {consecutive_failures} 轮对话失败，"
                        "程序主动退出，等待进程守护重启",
                    )
                    return
                time.sleep(2.0)
                state.set(State.IDLE, "异常恢复，回待机")
    except KeyboardInterrupt:
        logger.info("收到 Ctrl+C，退出")
    finally:
        play_cue(synthesizer, sfx.DONE, settings)
        memory.close()


def _one_conversation(
    settings, state, detector, recorder, recognizer,
    synthesizer, client, face, on_caption, on_emotion, vad_model,
) -> None:
    """唤醒后的一整段对话（最多 FOLLOW_UP_ROUNDS 轮）。

    从主循环里拆出来，只为让"自愈"有一个清晰的边界：
    这个函数抛任何异常，外层都会接住、回待机。
    """
    from .audio import sfx

    state.set(State.IDLE, "等待唤醒")
    detector.listen_once()
    play_cue(synthesizer, sfx.ACK, settings)      # 先应一声，别让人干等
    client.warmup_async()   # 接下来要录音+识别，正好拿这段时间把连接建好

    for round_index in range(1, FOLLOW_UP_ROUNDS + 1):
        state.set(State.LISTENING, f"第 {round_index} 轮")
        audio = recorder.record_until_silence()
        if not recorder.contains_speech(audio, vad_model):
            logger.info("没有听到内容，回去待机")
            break

        state.set(State.THINKING, "识别中")
        text = recognizer.transcribe(audio)
        if not text:
            logger.info("没听清，回去待机")
            break

        state.set(State.SPEAKING, "开始回答")
        try:
            synthesizer.speak_stream(
                client.stream_reply(text, on_emotion=on_emotion),
                on_sentence=on_caption,
            )
        except Exception:
            logger.exception("回答失败，回到待机")
            break
        if face is not None and face.interrupted():
            # 触屏打断：声音已经停了，等余音散掉再开麦 ——
            # 半双工铁律不能破，麦开早了它会听见自己的回音。
            face.clear_interrupt()
            logger.info("被触屏打断，停下来听你说")
            time.sleep(0.2)
            state.set(State.LISTENING, "被打断，继续听")
            continue

    state.set(State.IDLE, "本轮结束")


def _write_error_log(settings: Settings, reason: str) -> None:
    """自愈放弃时的退出原因，落一份小文件给守护进程/主人看。"""
    try:
        path = settings.paths.logs / "error.log"
        path.write_text(
            time.strftime("%Y-%m-%d %H:%M:%S") + " | " + reason + "\n",
            encoding="utf-8",
        )
    except OSError:
        logger.warning("写 error.log 失败", exc_info=True)


# 唤醒事件行的匹配串。格式在 wake/kws.py 里定死，两边要一起改。
_WAKE_EVENT_PATTERN = r"WAKE_EVENT \| ts=(\d{4}-\d{2}-\d{2}) (\d{2}):"

# 凌晨这几点都算"你多半在睡觉"。半夜自己醒是误唤醒最硬的证据。
NIGHT_HOURS = frozenset({"00", "01", "02", "03", "04", "05"})


def count_wake_events(logs_dir: Path) -> tuple[Counter[str], Counter[str]]:
    """扫 logs_dir 下的 xiaoyu_*.log，返回（每天唤醒次数, 每天凌晨唤醒次数）。

    纯读文件 + 计数：不打印、不写日志、不联网，好单测。
    只认 `WAKE_EVENT` 行，不认"听到唤醒词"那行 —— 两者是同一件事的两条日志，
    都数会翻倍。
    """
    import re
    from collections import Counter

    pattern = re.compile(_WAKE_EVENT_PATTERN)
    per_day: Counter[str] = Counter()
    night: Counter[str] = Counter()
    for path in sorted(logs_dir.glob("xiaoyu_*.log")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            logger.warning("读日志失败，跳过：{}", path.name, exc_info=True)
            continue
        for match in pattern.finditer(text):
            day, hour = match.group(1), match.group(2)
            per_day[day] += 1
            if hour in NIGHT_HOURS:
                night[day] += 1
    return per_day, night


def wake_report(settings: Settings) -> int:
    """误唤醒率有数（S5.5）：统计日志里的唤醒事件，按天列出次数。

    用法：python -m xiaoyu --wake-report
    阈值合不合适不靠感觉：没人说话的日子应该接近 0 次，
    超过 3 次/天就调高 config/keywords.txt 里的阈值。

    单独把"凌晨 0~6 点"拎出来，是因为那是最硬的证据 ——
    那时候你多半在睡觉，它醒了就是纯误唤醒，不用跟"我喊的"混在一起算。
    """
    from collections import Counter

    logs_dir: Path = settings.paths.logs
    per_day: Counter[str]
    night: Counter[str]
    per_day, night = count_wake_events(logs_dir)

    if not per_day:
        logger.info("日志里还没有唤醒事件（WAKE_EVENT）。先正常跑一天再来。")
        return 0
    logger.info("误唤醒统计（按天）：")
    for day in sorted(per_day):
        count = per_day[day]
        note = "  偏高：考虑调高唤醒阈值" if count > 3 else ""
        logger.info(
            "{}  唤醒 {} 次（其中凌晨 0~6 点 {} 次）{}",
            day, count, night.get(day, 0), note,
        )
    logger.info("判断标准：没人说话的日子应该接近 0；凌晨次数应该长期是 0；持续 >3 次/天就调阈值。")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="xiaoyu", description="XiaoYu Robot 桌面陪伴机器人")
    parser.add_argument("--check", action="store_true", help="只做环境自检，不启动硬件")
    parser.add_argument("--text", action="store_true", help="键盘模式（不需要麦克风）")
    parser.add_argument("--wake-report", action="store_true", help="统计每天的唤醒次数（误唤醒率有数）")
    parser.add_argument("--log-level", default=None, help="覆盖日志级别，如 DEBUG")
    args = parser.parse_args(argv)

    settings = Settings.load()
    setup_logging(level=args.log_level)
    log_banner(settings)

    # 只读日志，不需要硬件就绪 —— 放在环境自检之前，坏了也能查误唤醒。
    if args.wake_report:
        return wake_report(settings)

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