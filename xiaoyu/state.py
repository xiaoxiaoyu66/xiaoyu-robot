"""状态机。

机器人任何时刻只处于一个状态，表情脸、日志、日志级别都跟着它走。
后面接 Live2D 时，只要监听状态变化就能让脸做出反应。
"""

from __future__ import annotations

from enum import Enum
from typing import Callable

from .logger import get_logger

logger = get_logger(__name__)


class State(str, Enum):
    IDLE = "idle"          # 待机，只开唤醒词监听
    LISTENING = "listening"  # 正在听你说话
    THINKING = "thinking"    # 在等大模型回话
    SPEAKING = "speaking"    # 正在说话（此时必须闭麦，否则会自己唤醒自己）
    ERROR = "error"


class StateMachine:
    """极简状态机：只管状态切换 + 通知监听者。"""

    def __init__(self, initial: State = State.IDLE) -> None:
        self._state = initial
        self._listeners: list[Callable[[State, State], None]] = []
        logger.debug("状态机启动，初始状态 = {}", initial.value)

    @property
    def state(self) -> State:
        return self._state

    def on_change(self, callback: Callable[[State, State], None]) -> None:
        """注册监听器，签名 callback(old, new)。表情脸以后接在这里。"""
        self._listeners.append(callback)

    def set(self, new: State, reason: str = "") -> None:
        old = self._state
        if old is new:
            return
        self._state = new
        logger.info(
            "状态切换 {} -> {}{}", old.value, new.value, f"（{reason}）" if reason else ""
        )
        for callback in self._listeners:
            try:
                callback(old, new)
            except Exception:
                logger.exception("状态监听器执行失败")

    def is_speaking(self) -> bool:
        """说话时必须闭麦 —— 这是防止"自己唤醒自己"的关键。"""
        return self._state is State.SPEAKING

    def __repr__(self) -> str:
        return f"<StateMachine {self._state.value}>"