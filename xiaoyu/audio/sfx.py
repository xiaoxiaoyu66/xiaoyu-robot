"""提示音：不用音频文件，几个正弦波直接算出来。

为什么要提示音（这是实测出来的体感问题，不是锦上添花）：

    从"唤醒词被听见"到"它真正开口说话"，中间要等 VAD + 识别 + 大模型 + 合成，
    实测这段是 3 秒起步，网络不好的时候能拖到十几秒。
    这段时间里如果**一点动静都没有**，人会以为它没听见，
    于是重复喊唤醒词 —— 体验反而比"慢"更糟。

    一个 0.14 秒的短音就能把这段空白填上，主观等待感能少一大半。
    这是最便宜的一处体验优化：不需要更快，只需要"有反应"。

为什么不用音频文件：
    文件要管路径、管格式、管会不会被杀毒软件拦、管进不进 git。
    几个正弦波算出来的音最省事，也最好改。
    代价是没有音色可言 —— 以后想换成好听的就直接加载 wav 替换掉。
"""

from __future__ import annotations

import numpy as np

# 三个场景，音高走向不一样，闭着眼也能听出是哪个
ACK = "ack"          # 上行  -> "听到了，你说吧"
DONE = "done"        # 下行  -> "这轮结束了，我歇着"
ERROR = "error"      # 低沉  -> "出问题了"

# 每个音 70 毫秒，两个音加起来约 0.14 秒 —— 短到不烦人，长到能听清
_TONE_SECONDS = 0.07

# 淡入淡出 5 毫秒。不做这个，每个音的首尾都会"啪"一声爆音，
# 这是数字音频的常识，但第一次做的人基本都会踩。
_FADE_SECONDS = 0.005

_CUES: dict[str, tuple[float, ...]] = {
    ACK: (880.0, 1318.5),      # A5 -> E6，往上走，听着"积极"
    DONE: (1318.5, 880.0),     # E6 -> A5，往下走，听着"结束"
    ERROR: (440.0, 330.0),     # A4 -> E4，低沉，听着"不对劲"
}

_VOLUME = 0.25


def kinds() -> tuple[str, ...]:
    """所有可用的提示音名字。"""
    return tuple(_CUES)


def make_cue(kind: str = ACK, samplerate: int = 16000) -> np.ndarray:
    """生成一段提示音，返回 float32 单声道波形。

    kind 不认识时退回 ACK —— 提示音出问题绝不该让主流程挂掉。
    """
    tones = _CUES.get(kind) or _CUES[ACK]

    samples = max(2, int(_TONE_SECONDS * samplerate))
    fade = max(1, min(samples // 2, int(_FADE_SECONDS * samplerate)))
    ramp_up = np.linspace(0.0, 1.0, fade)
    ramp_down = np.linspace(1.0, 0.0, fade)

    pieces: list[np.ndarray] = []
    for frequency in tones:
        t = np.arange(samples, dtype=np.float64) / samplerate
        wave = np.sin(2 * np.pi * frequency * t)
        wave[:fade] *= ramp_up
        wave[-fade:] *= ramp_down
        pieces.append(wave)

    return (np.concatenate(pieces) * _VOLUME).astype(np.float32)