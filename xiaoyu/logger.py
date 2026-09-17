"""日志模块 —— 全项目唯一的日志出口。

约定（照着用就行）：
    1. 任何模块都不要 print()，改成 logger.info("...")
    2. 每个模块顶部写：logger = get_logger(__name__)
    3. 日志同时进 控制台（带颜色）和 文件（可回溯、按天切分）

产物：
    logs/xiaoyu_YYYY-MM-DD.log   全量日志，按天切分，保留 14 天，自动压缩
    logs/error.log               只记 ERROR 以上，保留 90 天，排查用
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from loguru import logger as _logger

DEFAULT_LOG_DIR = Path(__file__).resolve().parent.parent / "logs"

_CONSOLE_FMT = (
    "<green>{time:HH:mm:ss}</green> | "
    "<level>{level: <7}</level> | "
    "<cyan>{extra[module]}</cyan> | "
    "<level>{message}</level>"
)

_FILE_FMT = (
    "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <7} | "
    "{extra[module]} | {name}:{function}:{line} | {message}"
)

_configured = False


def setup_logging(
    level: str | None = None,
    log_dir: str | Path | None = None,
    console: bool = True,
    retention: str = "14 days",
) -> None:
    """初始化日志。幂等：重复调用只有第一次生效。"""
    global _configured
    if _configured:
        return

    level = (level or os.environ.get("XIAOYU_LOG_LEVEL", "INFO")).upper()
    log_dir = Path(log_dir or os.environ.get("XIAOYU_LOG_DIR") or DEFAULT_LOG_DIR)
    log_dir.mkdir(parents=True, exist_ok=True)

    _logger.remove()                              # 去掉 loguru 自带的 handler
    _logger.configure(extra={"module": "app"})    # 保证 {extra[module]} 永远有值

    if console:
        _logger.add(
            sys.stderr,
            level=level,
            format=_CONSOLE_FMT,
            colorize=True,
            backtrace=False,
            diagnose=False,
        )

    # 全量日志：文件里永远记 DEBUG，控制台可以用 XIAOYU_LOG_LEVEL 控制
    _logger.add(
        log_dir / "xiaoyu_{time:YYYY-MM-DD}.log",
        level="DEBUG",
        format=_FILE_FMT,
        rotation="00:00",        # 每天零点切一个新文件
        retention=retention,
        compression="zip",       # 老日志自动压缩
        encoding="utf-8",
        enqueue=True,            # 多线程 / 多进程安全
        backtrace=True,
        diagnose=False,          # 打开会把变量值写进日志，容易泄密，别开
    )

    # 只记错误的日志：出问题时第一个看这个
    _logger.add(
        log_dir / "error.log",
        level="ERROR",
        format=_FILE_FMT,
        rotation="10 MB",
        retention="90 days",
        encoding="utf-8",
        enqueue=True,
        backtrace=True,
        diagnose=False,
    )

    sys.excepthook = _handle_uncaught
    _configured = True
    _logger.debug("日志初始化完成 | level={} | dir={}", level, log_dir)


def _handle_uncaught(exc_type, exc_value, exc_tb) -> None:
    """未捕获的异常也写进日志，避免程序静默死掉。"""
    if issubclass(exc_type, KeyboardInterrupt):
        _logger.warning("收到 Ctrl+C，准备退出")
        return
    _logger.opt(exception=(exc_type, exc_value, exc_tb)).critical(
        "未捕获的异常，程序即将退出"
    )


def get_logger(name: str):
    """各模块这样用：logger = get_logger(__name__)"""
    setup_logging()
    return _logger.bind(module=name)