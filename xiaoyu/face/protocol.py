"""表情脸协议（S5）——主控和脸之间唯一的契约。

事件（主控 -> 脸，JSON 文本帧）：

    {"type": "state",   "state": "idle"}        # idle / listening / thinking / speaking / error
    {"type": "mouth",   "level": 0.37}          # 播放音量包络，约 50ms 一帧，口型跟随
    {"type": "caption", "text": "今天也要加油"}  # 正在说的这句话（字幕）
    {"type": "gaze",    "x": -0.4, "y": 0.2}    # 眼睛跟随（S6a）：人往哪，眼往哪，x/y ∈ [-1,1]
    {"type": "emotion", "mood": "happy", "intensity": 0.8}
                                                # 情绪系统：happy/sad/angry/surprised/neutral

命令（脸 -> 主控）：

    {"type": "interrupt"}                        # 点了一下脸：打断当前说话/思考

安全边界（v3 §3.1）：只在家里 WiFi 用，不映射公网；
连接必须带 token（?token=xxx），防局域网里别的设备误连。

本模块是纯逻辑（json + urllib），不 import websockets ——
测试环境没装那个包也能 import。
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

# 和 state.py 的 State 取值保持一致；写在这里而不是 import
# 是为了让这个纯协议模块不反向依赖主控的状态机。
VALID_STATES = frozenset({"idle", "listening", "thinking", "speaking", "error"})

# 和 llm/emotion.py 的 VALID_MOODS 保持一致；写两遍是为了让
# 纯协议模块不反向依赖大模型那侧。
VALID_MOODS = frozenset({"happy", "sad", "angry", "surprised", "neutral"})


def state_message(state: str) -> dict:
    if state not in VALID_STATES:
        raise ValueError(f"未知状态：{state}")
    return {"type": "state", "state": state}


def mouth_message(level: float) -> dict:
    """level 夹到 0~1 并取整 —— 脸那边拿到的永远是干净值。"""
    return {"type": "mouth", "level": round(max(0.0, min(1.0, float(level))), 3)}


def caption_message(text: str) -> dict:
    return {"type": "caption", "text": text.strip()}


def emotion_message(mood: str, intensity: float) -> dict:
    """情绪事件。不认识的 mood 一律降为 neutral —— 脸是消费端，不能被主控带崩。"""
    if mood not in VALID_MOODS:
        mood = "neutral"
    try:
        level = float(intensity)
    except (TypeError, ValueError):
        level = 0.5
    level = round(max(0.0, min(1.0, level)), 3)
    return {"type": "emotion", "mood": mood, "intensity": level}


def gaze_message(x: float, y: float) -> dict:
    """眼睛跟随（S6a）。x/y 各自夹到 [-1,1] 并取整 —— 与 mouth 同款洁癖。"""
    clamp = lambda v: round(max(-1.0, min(1.0, float(v))), 3)
    return {"type": "gaze", "x": clamp(x), "y": clamp(y)}


def encode(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)


def parse_command(raw: str | bytes) -> dict | None:
    """解析脸发回来的命令。

    不认识 / 解析失败一律返回 None —— 脸是外围，不能让它带崩主控。
    """
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8", errors="replace")
        except Exception:
            return None
    try:
        msg = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if isinstance(msg, dict) and msg.get("type") == "interrupt":
        return {"type": "interrupt"}
    return None


def token_from_path(path: str) -> str | None:
    """从 WebSocket 握手路径里取 token，例如 "/?token=xiaoyu"。"""
    try:
        values = parse_qs(urlparse(path).query).get("token")
    except ValueError:
        return None
    return values[0] if values else None


def token_ok(path: str, expected: str) -> bool:
    """校验握手 token。expected 为空 = 不校验（只该在调试时这么干）。"""
    if not expected:
        return True
    return token_from_path(path) == expected
