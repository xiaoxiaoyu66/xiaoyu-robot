"""配置中心。

原则：路径、参数、密钥全部从这里取，业务代码里不许写死。

配置来源优先级（后面的覆盖前面的）：
    1. 本文件里的默认值
    2. 项目根目录的 .env 文件
    3. 系统环境变量

改唤醒词 -> config/keywords.txt
改性格   -> config/persona.md
改密钥   -> .env（推荐）或系统环境变量

密钥绝对不会硬编码在本文件里。读取顺序：
    1. 环境变量 DEEPSEEK_API_KEY
    2. 环境变量 DEEPSEEK_API_KEY_FILE 指向的文件内容
    3. 项目根目录 .env 里的 DEEPSEEK_API_KEY

日志出口有脱敏过滤器，就算误把密钥打进日志也会被替换成 <已脱敏>。
"""

from __future__ import annotations

import importlib.util
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .logger import get_logger
from .text import read_text

logger = get_logger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent

KWS_ENCODER_PATTERN = "encoder*.onnx"
KWS_DECODER_PATTERN = "decoder*.onnx"
KWS_JOINER_PATTERN = "joiner*.onnx"
ASR_MODEL_PATTERN = "model*.onnx"


def _load_dotenv(path: Path) -> None:
    """把 .env 读进环境变量。优先用 python-dotenv，没装就用内置极简解析。"""
    if not path.exists():
        logger.debug(".env 不存在，使用默认配置：{}", path)
        return

    if importlib.util.find_spec("dotenv") is not None:
        from dotenv import load_dotenv

        load_dotenv(path, override=False)
        logger.debug("已通过 python-dotenv 加载 {}", path.name)
        return

    for raw in read_text(path).splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)
    logger.debug("已通过内置解析器加载 {}", path.name)


def _env_int(name: str) -> int | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        logger.warning("环境变量 {} 不是整数：{!r}，已忽略", name, raw)
        return None


def _env_bool(name: str, default: bool) -> bool:
    """读一个开关。写 1/true/yes/on 都算开，写 0/false/no/off 都算关。"""
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    logger.warning("环境变量 {} 不是开关值：{!r}，用默认值 {}", name, raw, default)
    return default


def _env_str(name: str) -> str | None:
    """读一个字符串环境变量，空白一律当成没设置。"""
    value = os.environ.get(name, "").strip()
    return value or None


def _read_secret(name: str) -> str | None:
    """读取密钥。绝不要把密钥写进代码里。

    优先环境变量；没有就看 <NAME>_FILE 指向的文件。

    为什么还要支持文件：
        环境变量会被子进程继承，在某些机器上 ps / 任务管理器能看到。
        放到一个只读文件里（比如以后在 N100 上跑），能少一个泄露面。
    """
    value = os.environ.get(name, "").strip()
    if value:
        return value

    path = os.environ.get(f"{name}_FILE", "").strip()
    if not path:
        return None

    try:
        content = read_text(path).strip()
    except OSError as exc:
        logger.error("读取密钥文件失败 {}：{}", path, exc)
        return None

    if not content:
        logger.error("密钥文件是空的：{}", path)
        return None

    logger.info("已从文件读取密钥（{}），内容不打印", path)
    return content


def _env_float(name: str, default: float) -> float:


    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("环境变量 {} 不是小数：{!r}，使用默认值 {}", name, raw, default)
        return default


@dataclass(frozen=True)
class Paths:
    root: Path = PROJECT_ROOT
    models: Path = PROJECT_ROOT / "models"
    kws: Path = PROJECT_ROOT / "models" / "kws"
    asr: Path = PROJECT_ROOT / "models" / "sense-voice"
    vad_model: Path = PROJECT_ROOT / "models" / "silero_vad.onnx"
    tts: Path = PROJECT_ROOT / "models" / "tts"
    vision: Path = PROJECT_ROOT / "models" / "vision"
    face_detector: Path = PROJECT_ROOT / "models" / "vision" / "blaze_face_short_range.tflite"
    data: Path = PROJECT_ROOT / "data"
    logs: Path = PROJECT_ROOT / "logs"
    config: Path = PROJECT_ROOT / "config"
    keywords: Path = PROJECT_ROOT / "config" / "keywords.txt"
    persona: Path = PROJECT_ROOT / "config" / "persona.md"
    db: Path = PROJECT_ROOT / "data" / "xiaoyu.db"

    def ensure_dirs(self) -> None:
        for p in (self.models, self.data, self.logs, self.config):
            p.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True)
class AudioConfig:
    """音频参数。16000Hz 单声道是语音模型的硬要求，别乱改。"""

    sample_rate: int = 16000
    channels: int = 1
    mic_device: int | None = None
    speaker_device: int | None = None
    # 按名字选设备，比编号稳。编号会在两次运行之间漂移（实测 [3] <-> [4] 互换过），
    # 名字基本不变。两个都填时以名字为准，编号只当备用。
    mic_device_name: str | None = None
    speaker_device_name: str | None = None
    # 连续安静多久算"说完了"。0.8 -> 0.5 是为了反应更快，
    # 代价是说话中间停顿久一点就会被当成说完（想改回去就设 XIAOYU_SILENCE_SECONDS=0.8）
    silence_seconds: float = 0.5
    silence_threshold: float = 0.015  # 音量低于这个值算静音
    max_record_seconds: float = 15.0  # 单次最长录音，防止卡死
    cue_enabled: bool = True          # 唤醒命中后播一声短提示音


@dataclass(frozen=True)
class WakeConfig:
    keywords_score: float = 1.0
    keywords_threshold: float = 0.25  # 越大越不容易误唤醒，越小越灵敏
    num_threads: int = 2


@dataclass(frozen=True)
class AsrConfig:
    language: str = "zh"
    use_itn: bool = True              # 加标点和数字规整
    num_threads: int = 4


@dataclass(frozen=True)
class TtsConfig:
    # 合成引擎：local 用本地 sherpa-onnx（快），edge 用微软云端（好听但慢）
    # 实测：edge 首块音频 0.88~11.76 秒，本地 0.1~0.3 秒
    engine: str = "local"
    # ---- edge 引擎用 ----
    voice: str = "zh-CN-XiaoxiaoNeural"
    rate: str = "+0%"                 # 语速，例如 "+10%"
    volume: str = "+0%"
    # 音调。这几个值一起决定"音色气质"：升调更亮更年轻，降调更沉稳冷静
    pitch: str = "+0Hz"               # 例如 "-10Hz"、" +10Hz"
    # ---- local 引擎用 ----
    local_model: str = "vits-piper-zh_CN-huayan-medium"   # models/tts/ 下的目录名
    vocoder: str = ""                 # 只有 matcha 系模型需要（单独的 onnx 文件名）
    speaker_id: int = 0               # 多音色模型用
    speed: float = 1.0                # 1.0 = 原速，大于 1 更快
    num_threads: int = 2

@dataclass(frozen=True)
class LlmConfig:
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-chat"
    temperature: float = 1.1
    max_history: int = 20             # 送进模型的最大轮数
    api_key: str | None = None
    timeout_seconds: float = 60.0

    @property
    def has_key(self) -> bool:
        return bool(self.api_key)


@dataclass(frozen=True)
class MemoryConfig:
    # 三个都能在 .env 里覆盖：
    #   XIAOYU_MEMORY_TOP_K / XIAOYU_MEMORY_SUMMARIZE_EVERY / XIAOYU_MEMORY_MODEL
    top_k: int = 3                    # 每轮检索几条相关记忆
    summarize_every: int = 20         # 每多少轮总结一次"关于主人的事实"
    model_name: str = "BAAI/bge-small-zh-v1.5"   # 4c 向量检索的编码模型


@dataclass(frozen=True)
class FaceConfig:
    """表情脸（S5）。脸是纯消费者：服务起不来、脸没开，都不影响主流程。"""

    enabled: bool = True
    host: str = "0.0.0.0"   # 只在家里 WiFi 用，不映射公网（v3 §3.1 安全边界）
    port: int = 8765
    token: str = "xiaoyu"   # 连接必须带 ?token=xxx，防局域网里别的设备误连
    # 控制台表情脸：把同样的状态/字幕/口型事件翻译成字符画打进日志流。
    # 调试时人盯着 PowerShell，浏览器里的脸照顾不到 —— 终端里得有分身。
    console: bool = True


@dataclass(frozen=True)
class VisionConfig:
    """眼睛跟随（S6a）。视觉是可选件：依赖没装/摄像头打不开就静默禁用。"""

    enabled: bool = True
    camera_index: int = 0
    publish_hz: int = 10          # gaze 广播节流
    smooth_alpha: float = 0.3     # EMA 平滑系数，越大越跟手
    min_confidence: float = 0.5   # BlazeFace 置信度门槛
    model_file: str = "blaze_face_short_range.tflite"  # 在 models/vision/ 下


@dataclass(frozen=True)
class BodyConfig:
    """身体（S7 准备）：舵机 / 屏幕 / 麦克风 / 喇叭的接线参数。

    **默认关**：开了也只是接上假舵机往日志里打角度，不动任何硬件。
    真舵机买回来那天，把 app.attach_body 里的 FakeServo 换成真 backend 即可，
    行为代码一行不用改（这就是"身体接口层"的意义，见 xiaoyu/body/__init__.py）。

    幅度先给小的：真舵机大角度瞬间转动像抽搐，还容易堵转发热。
    """

    enabled: bool = False
    pan_range: float = 30.0    # gaze 满偏时头左右转多少度
    tilt_range: float = 15.0   # 上下抬多少度（比左右小，抬头夸张很怪）
    deadzone: float = 0.05     # 小于这个幅度当作"正中间"，防人脸检测抖动
    invert_pan: bool = False   # 舵机左右装反了就开这个，不要改 head.py 的正负号
    invert_tilt: bool = False


@dataclass
class Settings:
    paths: Paths = field(default_factory=Paths)
    audio: AudioConfig = field(default_factory=AudioConfig)
    wake: WakeConfig = field(default_factory=WakeConfig)
    asr: AsrConfig = field(default_factory=AsrConfig)
    tts: TtsConfig = field(default_factory=TtsConfig)
    llm: LlmConfig = field(default_factory=LlmConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    face: FaceConfig = field(default_factory=FaceConfig)
    vision: VisionConfig = field(default_factory=VisionConfig)
    body: BodyConfig = field(default_factory=BodyConfig)

    @classmethod
    def load(cls) -> "Settings":
        _load_dotenv(PROJECT_ROOT / ".env")

        settings = cls(
            audio=AudioConfig(
                mic_device=_env_int("XIAOYU_MIC_DEVICE"),
                speaker_device=_env_int("XIAOYU_SPK_DEVICE"),
                mic_device_name=_env_str("XIAOYU_MIC_DEVICE_NAME"),
                speaker_device_name=_env_str("XIAOYU_SPK_DEVICE_NAME"),
                silence_seconds=_env_float("XIAOYU_SILENCE_SECONDS", 0.5),
                silence_threshold=_env_float("XIAOYU_SILENCE_THRESHOLD", 0.015),
                cue_enabled=_env_bool("XIAOYU_CUE_ENABLED", True),
            ),
            tts=TtsConfig(
                engine=(_env_str("XIAOYU_TTS_ENGINE") or "local").lower(),
                voice=_env_str("XIAOYU_TTS_VOICE") or "zh-CN-XiaoxiaoNeural",
                rate=_env_str("XIAOYU_TTS_RATE") or "+0%",
                volume=_env_str("XIAOYU_TTS_VOLUME") or "+0%",
                pitch=_env_str("XIAOYU_TTS_PITCH") or "+0Hz",
                local_model=_env_str("XIAOYU_TTS_MODEL")
                or "vits-piper-zh_CN-huayan-medium",
                vocoder=_env_str("XIAOYU_TTS_VOCODER") or "",
                speaker_id=_env_int("XIAOYU_TTS_SID") or 0,
                speed=_env_float("XIAOYU_TTS_SPEED", 1.0),
                num_threads=_env_int("XIAOYU_TTS_THREADS") or 2,
            ),
            llm=LlmConfig(api_key=_read_secret("DEEPSEEK_API_KEY")),
            memory=MemoryConfig(
                top_k=_env_int("XIAOYU_MEMORY_TOP_K") or 3,
                # 调试"事实积累"时把它调小（比如 3），聊几轮就能看到效果
                summarize_every=_env_int("XIAOYU_MEMORY_SUMMARIZE_EVERY") or 20,
                # 4c 的编码模型。换个模型等于换一套向量空间，
                # 旧向量会因维度不符被自动重算（store.py 里判的就是 dim）
                model_name=_env_str("XIAOYU_MEMORY_MODEL")
                or "BAAI/bge-small-zh-v1.5",
            ),
            face=FaceConfig(
                enabled=_env_bool("XIAOYU_FACE_ENABLED", True),
                port=_env_int("XIAOYU_FACE_PORT") or 8765,
                token=_env_str("XIAOYU_FACE_TOKEN") or "xiaoyu",
                console=_env_bool("XIAOYU_FACE_CONSOLE", True),
            ),
            vision=VisionConfig(
                enabled=_env_bool("XIAOYU_VISION_ENABLED", True),
                camera_index=_env_int("XIAOYU_VISION_CAMERA") or 0,
                publish_hz=_env_int("XIAOYU_VISION_HZ") or 10,
                smooth_alpha=_env_float("XIAOYU_VISION_ALPHA", 0.3),
                min_confidence=_env_float("XIAOYU_VISION_CONFIDENCE", 0.5),
            ),
            body=BodyConfig(
                enabled=_env_bool("XIAOYU_BODY_ENABLED", False),
                pan_range=_env_float("XIAOYU_BODY_PAN_RANGE", 30.0),
                tilt_range=_env_float("XIAOYU_BODY_TILT_RANGE", 15.0),
                deadzone=_env_float("XIAOYU_BODY_DEADZONE", 0.05),
                invert_pan=_env_bool("XIAOYU_BODY_INVERT_PAN", False),
                invert_tilt=_env_bool("XIAOYU_BODY_INVERT_TILT", False),
            ),
        )
        settings.paths.ensure_dirs()
        logger.info(
            "配置加载完成 | 唤醒词文件={} | 性格文件={} | API Key={}",
            settings.paths.keywords.name,
            settings.paths.persona.name,
            "已配置" if settings.llm.has_key else "缺失",
        )
        return settings

    # ------------------------------------------------------------------
    # 自检
    # ------------------------------------------------------------------
    def diagnose(self) -> list["CheckItem"]:
        """检查环境是否就绪，返回一份清单（供 app.py 打印）。"""
        items: list[CheckItem] = []

        py_ok = sys.version_info >= (3, 11)
        items.append(
            CheckItem(
                "Python 版本",
                py_ok,
                f"{sys.version.split()[0]}（建议 3.11+）",
            )
        )

        for pkg, purpose in [
            ("loguru", "日志"),
            ("sounddevice", "录音/播放"),
            ("numpy", "音频计算"),
            ("sherpa_onnx", "唤醒 + 语音识别"),
            ("edge_tts", "语音合成"),
            ("soundfile", "解码 mp3 / 音频文件"),
            ("openai", "大模型对话"),
            ("sentence_transformers", "记忆（S4 才需要）"),
            ("websockets", "表情脸（S5）"),
            ("mediapipe", "眼睛跟随（S6a）"),
            ("cv2", "眼睛跟随（S6a）"),
        ]:
            found = importlib.util.find_spec(pkg) is not None
            items.append(CheckItem(f"依赖 {pkg}", found, purpose))

        items.append(self._check_kws())
        items.append(self._check_asr())
        items.append(self._check_tts())
        items.append(
            CheckItem(
                "VAD 模型",
                self.paths.vad_model.exists(),
                str(self.paths.vad_model.relative_to(self.paths.root)),
            )
        )
        items.append(
            CheckItem(
                "眼睛跟随模型",
                self.paths.face_detector.exists(),
                str(self.paths.face_detector.relative_to(self.paths.root)),
            )
        )
        items.append(
            CheckItem(
                "唤醒词文件",
                self.paths.keywords.exists(),
                str(self.paths.keywords.relative_to(self.paths.root)),
            )
        )
        items.append(
            CheckItem(
                "性格文件",
                self.paths.persona.exists(),
                str(self.paths.persona.relative_to(self.paths.root)),
            )
        )
        items.append(
            CheckItem(
                "DeepSeek API Key",
                self.llm.has_key,
                ".env 或系统环境变量 DEEPSEEK_API_KEY",
            )
        )
        return items

    def _check_tts(self) -> "CheckItem":
        """检查语音合成。edge 引擎只要联网，本地引擎要模型。

        这里有意不去 import tts.engine：engine 反过来要 import
        config 里的 Settings，循环导入会直接炸掉。
        所以这里只做最粗的"文件在不在"检查。
        """
        if self.tts.engine != "local":
            return CheckItem("语音合成模型", True, "用 edge-tts（云端，不占本地）")

        model_dir = self.paths.tts / self.tts.local_model
        if not model_dir.is_dir():
            return CheckItem(
                "语音合成模型", False, f"缺少目录 models/tts/{self.tts.local_model}/"
            )
        if not list(model_dir.glob("*.onnx")) or not (model_dir / "tokens.txt").exists():
            return CheckItem(
                "语音合成模型", False, f"{self.tts.local_model}/ 里缺 .onnx 或 tokens.txt"
            )
        if self.tts.vocoder and not (self.paths.tts / self.tts.vocoder).exists():
            return CheckItem(
                "语音合成模型", False, f"缺少声码器 {self.tts.vocoder}"
            )
        return CheckItem("语音合成模型", True, self.tts.local_model)

    def _check_kws(self) -> "CheckItem":
        d = self.paths.kws
        if not d.exists():
            return CheckItem("唤醒模型", False, f"缺少目录 {d.name}/")
        missing = [
            pat
            for pat in (KWS_ENCODER_PATTERN, KWS_DECODER_PATTERN, KWS_JOINER_PATTERN, "tokens.txt")
            if not list(d.glob(pat))
        ]
        if missing:
            return CheckItem("唤醒模型", False, "缺少 " + ", ".join(missing))
        return CheckItem("唤醒模型", True, "models/kws/")

    def _check_asr(self) -> "CheckItem":
        d = self.paths.asr
        if not d.exists():
            return CheckItem("识别模型", False, f"缺少目录 {d.name}/")
        missing = [
            pat
            for pat in (ASR_MODEL_PATTERN, "tokens.txt")
            if not list(d.glob(pat))
        ]
        if missing:
            return CheckItem("识别模型", False, "缺少 " + ", ".join(missing))
        return CheckItem("识别模型", True, "models/sense-voice/")


@dataclass
class CheckItem:
    name: str
    ok: bool
    detail: str = ""

    def render(self) -> str:
        mark = "[ OK ]" if self.ok else "[缺失]"
        return f"{mark} {self.name:<26} {self.detail}"