"""TTS 引擎：把一句话变成一段波形。

为什么要从 edge-tts 换到本地（这是实测数字，不是感觉）：

    edge-tts 出第一块音频要 0.88 / 1.13 / 3.17 / 3.23 / 5.11 / 9.33 / 10.10 / 11.76 秒，
    另有 2 次直接失败（连接超时、握手失败）。
    因为它每次合成都去连微软的服务器，**每句话重新握一次手** ——
    回你三句话，就是抽三次奖。这是"反应慢"的主因，比大模型慢得多。

    本地 sherpa-onnx 合成只要零点几秒，而且完全不依赖网络，不会失败。
    代价是音色不如晓晓自然，所以 edge-tts 没有被删掉，
    只是从"唯一选择"降级成了可切换的"高音质模式"。

接口故意做得很小，就一个方法：
    engine.synthesize("你好") -> Speech(samples, samplerate)
这样以后想换 Kokoro、GPT-SoVITS，只要再写一个类，别的代码一行都不用动。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ..config import Settings
from ..logger import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class Speech:
    """合成结果：一段波形 + 它的采样率。

    为什么把采样率一起带着走：
        edge-tts 出来是 24000Hz，Matcha 是 22050Hz，Piper 是 22050Hz。
        播放时必须用对的采样率，否则声音会变调（快放/慢放）。
        让它跟着数据走，就不会传丢。
    """

    samples: np.ndarray
    samplerate: int

    @property
    def duration(self) -> float:
        return len(self.samples) / self.samplerate if self.samplerate else 0.0


# ---------------------------------------------------------------- 本地模型定位

@dataclass(frozen=True)
class ModelFiles:
    """一个本地 TTS 模型要用到的所有文件。"""

    kind: str                  # "vits" 或 "matcha"
    model: Path                # vits 的主模型 / matcha 的声学模型
    tokens: Path
    vocoder: Path | None = None
    lexicon: Path | None = None
    dict_dir: Path | None = None
    data_dir: Path | None = None
    rule_fsts: str = ""


# 这些名字的 onnx 是声码器，不是声学模型，别认错
_VOCODER_HINTS = ("vocoder", "vocos", "hifigan_v1", "hifigan_v2", "hifigan_v3")


def _is_vocoder(path: Path) -> bool:
    name = path.name.lower()
    return any(hint in name for hint in _VOCODER_HINTS)


def _looks_like_matcha(model: Path) -> bool:
    """这个声学模型是不是 Matcha？

    为什么要单独猜一下，而不直接看“有没有声码器”：
        sherpa-onnx 官方把模型都堆在 models/tts/ 下，
        声码器 vocos-22khz-univ.onnx 就跟 piper 的目录**并排**。
        照着“找到声码器就当 matcha”去做，会把 piper 也当成 matcha
        去加载，然后报一个看不懂的错：
        'use_eos_bos' does not exist in the metadata。

        所以只能看模型自己的文件名：Matcha 的声学模型叫
        model-steps-N.onnx（N 是训练步数），而 VITS / piper 都是自己的名字。
    """
    return model.name.lower().startswith("model-steps-")


def resolve_model_files(model_dir: Path, vocoder: Path | None = None) -> ModelFiles:
    """搞清楚这个模型目录里是哪种模型、每个文件在哪。

    为什么不把文件名写死：
        不同模型的目录结构完全不一样（有的是 model.onnx，有的是
        model-steps-3.onnx，piper 还多一个 espeak-ng-data 目录）。
        写死文件名的话，换模型就等于改代码。
        所以这里按"目录里有什么"来判断，加新模型通常不用动这个函数。

    判断规则（两步，顺序不能换）：
        1. 先在目录里挑出"声学模型"：排除名字像声码器的那些，剩下的取最大的一个。
        2. 再看这个声学模型**自己的文件名**像不像 matcha（见 _looks_like_matcha）：
           像   -> matcha，另外还要找一个声码器配它
           不像 -> vits。piper 也是 vits 的一种，只是多一个 espeak-ng-data 目录

        第 2 步**不能**偷懒写成"目录旁边有 vocos 就当 matcha" ——
        那个写法会把 piper 也判成 matcha，报一个看不懂的错。原因见 _looks_like_matcha。
    """
    model_dir = Path(model_dir)
    if not model_dir.is_dir():
        raise FileNotFoundError(f"TTS 模型目录不存在：{model_dir}")

    tokens = model_dir / "tokens.txt"
    if not tokens.exists():
        raise FileNotFoundError(f"模型目录里缺少 tokens.txt：{model_dir}")

    onnx_files = sorted(p for p in model_dir.glob("*.onnx") if not _is_vocoder(p))
    if not onnx_files:
        raise FileNotFoundError(
            f"模型目录里找不到 .onnx 模型文件：{model_dir}\n"
            "重新下载：python scripts\\download_models.py"
        )
    # 声学模型永远是最大的那个（声码器一般更小）
    model = max(onnx_files, key=lambda p: p.stat().st_size)

    if _looks_like_matcha(model):
        kind = "matcha"
        if vocoder is None:
            own = sorted(p for p in model_dir.glob("*.onnx") if _is_vocoder(p))
            # 声码器常常和模型目录**并排**放在 models/tts/ 下，
            # 顺手找一下，省得用户还要去 .env 手填文件名
            sibling = sorted(p for p in model_dir.parent.glob("*.onnx") if _is_vocoder(p))
            vocoder = (own or sibling or [None])[0]
        if vocoder is None or not Path(vocoder).exists():
            # 直接把 None 拼进去会写成“没找到：None”，
            # 看起来像代码 bug 而不是文件缺失，所以分开写。
            found = (
                f"{vocoder}（文件不存在）"
                if vocoder
                else "模型目录里和旁边都没有"
            )
            raise FileNotFoundError(
                "Matcha 模型必须配一个声码器（vocoder），"
                f"但没找到：{found}\n"
                "下载：python scripts\\download_models.py --only vocos\n"
                "或在 .env 里指定：XIAOYU_TTS_VOCODER=vocos-22khz-univ.onnx"
            )
        vocoder = Path(vocoder)
    else:
        # VITS 系（包括 piper）自带声码器，不需要外部文件。
        # 这里必须把 vocoder 置空，否则会把 piper 模型当成 matcha 去加载，
        # 直接报一个看不懂的错。
        kind = "vits"
        vocoder = None

    lexicon = model_dir / "lexicon.txt"
    dict_dir = model_dir / "dict"
    data_dir = model_dir / "espeak-ng-data"

    # 中文模型靠这些 .fst 规则把数字/日期念对（2026 -> 二零二六）
    rule_fsts = ",".join(str(p) for p in sorted(model_dir.glob("*.fst")))

    return ModelFiles(
        kind=kind,
        model=model,
        tokens=tokens,
        vocoder=vocoder if kind == "matcha" else None,
        lexicon=lexicon if lexicon.exists() else None,
        dict_dir=dict_dir if dict_dir.is_dir() else None,
        data_dir=data_dir if data_dir.is_dir() else None,
        rule_fsts=rule_fsts,
    )


# ---------------------------------------------------------------- 引擎

class SherpaEngine:
    """本地合成，走 sherpa-onnx。

    第一次 synthesize 会明显慢（要加载模型），所以调用方要在启动阶段
    先"热身"一次，别把这个开销算到用户头上。见 warmup()。
    """

    name = "sherpa-onnx"

    def __init__(
        self,
        model_dir: Path,
        *,
        vocoder: Path | None = None,
        speaker_id: int = 0,
        speed: float = 1.0,
        num_threads: int = 2,
    ) -> None:
        import sherpa_onnx

        files = resolve_model_files(model_dir, vocoder)
        logger.info(
            "加载本地 TTS 模型 | 类型={} | 模型={} | 声码器={}",
            files.kind,
            files.model.name,
            files.vocoder.name if files.vocoder else "无（模型自带）",
        )

        common = {
            "tokens": str(files.tokens),
            "lexicon": str(files.lexicon) if files.lexicon else "",
            "dict_dir": str(files.dict_dir) if files.dict_dir else "",
        }
        if files.kind == "matcha":
            model_config = sherpa_onnx.OfflineTtsModelConfig(
                matcha=sherpa_onnx.OfflineTtsMatchaModelConfig(
                    acoustic_model=str(files.model),
                    vocoder=str(files.vocoder),
                    length_scale=1.0 / max(speed, 0.1),
                    **common,
                ),
                num_threads=num_threads,
                provider="cpu",
            )
        else:
            vits_kwargs = dict(common)
            if files.data_dir is not None:
                # piper 系模型靠 espeak 做音素化，少了这个目录会直接报错
                vits_kwargs["data_dir"] = str(files.data_dir)
            model_config = sherpa_onnx.OfflineTtsModelConfig(
                vits=sherpa_onnx.OfflineTtsVitsModelConfig(
                    model=str(files.model),
                    length_scale=1.0 / max(speed, 0.1),
                    **vits_kwargs,
                ),
                num_threads=num_threads,
                provider="cpu",
            )

        config = sherpa_onnx.OfflineTtsConfig(
            model=model_config,
            rule_fsts=files.rule_fsts,
            max_num_sentences=1,
        )
        self._tts = sherpa_onnx.OfflineTts(config)
        self._sid = speaker_id
        self._speed = speed
        self.samplerate = int(self._tts.sample_rate)
        self.num_speakers = int(getattr(self._tts, "num_speakers", 1))
        logger.info(
            "本地 TTS 就绪 | 采样率={}Hz | 音色数={} | 线程={}",
            self.samplerate, self.num_speakers, num_threads,
        )

    def synthesize(self, text: str) -> Speech:
        started = time.perf_counter()
        audio = self._tts.generate(text, sid=self._sid, speed=self._speed)
        samples = np.asarray(audio.samples, dtype=np.float32)
        speech = Speech(samples=samples, samplerate=int(audio.sample_rate))
        logger.debug(
            "本地合成 {:.2f} 秒音频耗时 {:.2f} 秒（RTF={:.2f}）",
            speech.duration, time.perf_counter() - started,
            (time.perf_counter() - started) / speech.duration if speech.duration else 0.0,
        )
        return speech

    def warmup(self) -> None:
        """先跑一次短的，把模型加载和内存分配的开销吃掉。

        不做这一步，第一句真实回复会莫名其妙慢好几秒 ——
        而那正是用户最在意的一次。
        """
        started = time.perf_counter()
        try:
            self.synthesize("你好")
        except Exception:
            logger.exception("本地 TTS 热身失败，第一次合成可能会慢")
            return
        logger.info("本地 TTS 热身完成，耗时 {:.2f} 秒", time.perf_counter() - started)


class EdgeEngine:
    """云端合成，走 edge-tts。音质最好，但每句话都要连一次微软的服务器。"""

    name = "edge-tts"

    def __init__(
        self,
        *,
        voice: str = "zh-CN-XiaoxiaoNeural",
        rate: str = "+0%",
        volume: str = "+0%",
        pitch: str = "+0Hz",
        tmp_dir: Path | None = None,
    ) -> None:
        self._voice = voice
        self._rate = rate
        self._volume = volume
        self._pitch = pitch
        self._tmp_dir = Path(tmp_dir) if tmp_dir else Path(".")
        self._tmp_dir.mkdir(parents=True, exist_ok=True)
        self.samplerate = 24000        # edge-tts 固定 24kHz，实际以解码结果为准
        self.num_speakers = 1

    def synthesize(self, text: str) -> Speech:
        import asyncio

        import edge_tts
        import soundfile as sf

        path = self._tmp_dir / f"edge_{int(time.time() * 1000)}.mp3"

        async def run() -> None:
            await edge_tts.Communicate(
                text,
                self._voice,
                rate=self._rate,
                volume=self._volume,
                pitch=self._pitch,
            ).save(str(path))

        try:
            asyncio.run(run())
            data, samplerate = sf.read(str(path), dtype="float32", always_2d=False)
        finally:
            path.unlink(missing_ok=True)

        if data.ndim > 1:
            data = data.mean(axis=1)
        return Speech(samples=np.asarray(data, dtype=np.float32), samplerate=int(samplerate))

    def warmup(self) -> None:
        return None        # 云端没有"加载模型"这回事


def build_engine(settings: Settings):
    """按配置造引擎。本地建不起来就退回 edge-tts，并大声提醒。

    为什么容错而不是直接报错退出：
        本地模型是 100MB 级别的下载，用户很可能还没下完就想先跑起来看看。
        这时候退回云端至少能用，总比"启动失败"友好。
    """
    cfg = settings.tts
    if cfg.engine != "local":
        logger.info("按配置使用 edge-tts（音质优先，但每句话要联网）")
        return EdgeEngine(
            voice=cfg.voice, rate=cfg.rate, volume=cfg.volume, pitch=cfg.pitch,
            tmp_dir=settings.paths.data / "tts_cache",
        )

    model_dir = settings.paths.tts / cfg.local_model
    vocoder = settings.paths.tts / cfg.vocoder if cfg.vocoder else None
    try:
        engine = SherpaEngine(
            model_dir,
            vocoder=vocoder,
            speaker_id=cfg.speaker_id,
            speed=cfg.speed,
            num_threads=cfg.num_threads,
        )
    except Exception as exc:
        logger.error("本地 TTS 用不了（{}），先退回 edge-tts", exc)
        logger.error(
            "要修的话：python scripts\\download_models.py --prefix https://gh-proxy.com/"
        )
        return EdgeEngine(
            voice=cfg.voice, rate=cfg.rate, volume=cfg.volume, pitch=cfg.pitch,
            tmp_dir=settings.paths.data / "tts_cache",
        )
    return engine