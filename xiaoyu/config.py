"""配置中心。

原则：路径、参数、密钥全部从这里取，业务代码里不许写死。

配置来源优先级（后面的覆盖前面的）：
    1. 本文件里的默认值
    2. 项目根目录的 .env 文件
    3. 系统环境变量

改唤醒词 -> config/keywords.txt
改性格   -> config/persona.md
改密钥   -> .env
"""

from __future__ import annotations

import importlib.util
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .logger import get_logger

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

    for raw in path.read_text(encoding="utf-8").splitlines():
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
    silence_seconds: float = 0.8      # 连续安静多久算"说完了"
    silence_threshold: float = 0.015  # 音量低于这个值算静音
    max_record_seconds: float = 15.0  # 单次最长录音，防止卡死


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
    voice: str = "zh-CN-XiaoxiaoNeural"
    rate: str = "+0%"                 # 语速，例如 "+10%"
    volume: str = "+0%"


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
    top_k: int = 3                    # 每轮检索几条相关记忆
    summarize_every: int = 20         # 每多少轮总结一次"关于主人的事实"
    model_name: str = "BAAI/bge-small-zh-v1.5"


@dataclass
class Settings:
    paths: Paths = field(default_factory=Paths)
    audio: AudioConfig = field(default_factory=AudioConfig)
    wake: WakeConfig = field(default_factory=WakeConfig)
    asr: AsrConfig = field(default_factory=AsrConfig)
    tts: TtsConfig = field(default_factory=TtsConfig)
    llm: LlmConfig = field(default_factory=LlmConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)

    @classmethod
    def load(cls) -> "Settings":
        _load_dotenv(PROJECT_ROOT / ".env")

        settings = cls(
            audio=AudioConfig(
                mic_device=_env_int("XIAOYU_MIC_DEVICE"),
                speaker_device=_env_int("XIAOYU_SPK_DEVICE"),
                silence_threshold=_env_float("XIAOYU_SILENCE_THRESHOLD", 0.015),
            ),
            llm=LlmConfig(api_key=os.environ.get("DEEPSEEK_API_KEY") or None),
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
            ("pygame", "播放 mp3"),
            ("openai", "大模型对话"),
            ("sentence_transformers", "记忆（S4 才需要）"),
        ]:
            found = importlib.util.find_spec(pkg) is not None
            items.append(CheckItem(f"依赖 {pkg}", found, purpose))

        items.append(self._check_kws())
        items.append(self._check_asr())
        items.append(
            CheckItem(
                "VAD 模型",
                self.paths.vad_model.exists(),
                str(self.paths.vad_model.relative_to(self.paths.root)),
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
                ".env 里的 DEEPSEEK_API_KEY",
            )
        )
        return items

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