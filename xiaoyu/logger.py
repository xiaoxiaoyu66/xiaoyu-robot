"""日志模块 —— 全项目唯一的日志出口。

约定（照着用就行）：
    1. 任何模块都不要 print()，改成 logger.info("...")
    2. 每个模块顶部写：logger = get_logger(__name__)
    3. 日志同时进 控制台（带颜色）和 文件（可回溯、按天切分）

安全：所有出口都挂了脱敏过滤器（见 _redact_record）。
密钥一旦写进日志文件就是永久留痕，所以在落盘前就替换掉。
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from loguru import logger as _logger

from .text import sanitize

DEFAULT_LOG_DIR = Path(__file__).resolve().parent.parent / "logs"

# 测试进程不要把日志写进生产 logs/。
# 原因：get_logger() 会在 import 时就把文件 sink 建起来，跑一次测试
# 就往 logs/error.log 灌一堆测试堆栈（实测 527 行、跑 3 个用例涨 7KB），
# 会淹掉真实故障，也让 `python -m xiaoyu --wake-report` 的误唤醒统计失真。
# 放在这里而不是 tests/conftest.py：unittest discover 是把测试模块当顶层模块
# 导入的，tests/__init__.py 根本不会被执行。
if "pytest" in sys.modules or "unittest" in sys.modules:
    import tempfile

    os.environ.setdefault(
        "XIAOYU_LOG_DIR", str(Path(tempfile.gettempdir()) / "xiaoyu-tests-logs")
    )

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

# 长得像密钥的东西，一律在写盘前替换掉
_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9_\-\.]{8,}"),
    re.compile(r"(?i)\b(api[_-]?key|token|secret|password)\b\s*[=:]\s*\S{8,}"),
)

_REDACTED = "<已脱敏>"

_configured = False


def _redact(text: str) -> str:
    """抹掉密钥，顺带清掉无法编码的字符。

    两件事必须一起做：
        1. 密钥不能落盘；
        2. 落盘的字符串必须真的能编码成 utf-8 —— 否则
           文件 sink 那一层会抛 UnicodeEncodeError，
           整条日志丢掉不说，还会往控制台喷一堆堆栈。
    """
    text = sanitize(text)
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(_REDACTED, text)
    return text


def _redact_record(record) -> bool:
    """loguru 过滤器：把密钥从日志里抹掉。

    两条铁律：
        1. 返回值必须是 True，否则这条日志会被整个丢掉。
        2. 里面绝不允许抛异常 —— 过滤器一崩，日志系统就整体瘫痪。
           所以整个函数体包在 try 里，脱敏失败也必须让日志照常输出。
    """
    try:
        record["message"] = _redact(record.get("message") or "")

        # 注意：record 里不一定有 args / exception 这两个键，必须用 get
        args = record.get("args")
        if args:
            record["args"] = tuple(
                _redact(item) if isinstance(item, str) else item for item in args
            )

        exception = record.get("exception")
        if exception is not None:
            etype, evalue, etb = exception
            if evalue is not None:
                original = str(evalue)
                redacted = _redact(original)
                if redacted != original:
                    try:
                        record["exception"] = type(exception)(
                            etype, type(evalue)(redacted), etb
                        )
                    except Exception:
                        # 有些异常类需要多个构造参数，退化处理，至少把原文遮掉
                        record["exception"] = type(exception)(
                            etype, RuntimeError(redacted), etb
                        )
    except Exception:
        # 宁可少脱敏一次，也不能让日志系统崩掉
        pass
    return True


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
            # 只给真终端上色：输出被重定向到文件时（常驻守护就是这么干的）
            # 颜色码会变成一堆 [32m 噪声，把日志搞得没法读
            colorize=sys.stderr.isatty(),
            backtrace=False,
            diagnose=False,
            filter=_redact_record,      # 脱敏
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
        errors="backslashreplace",   # 编码兜底：再出现怪字符也只影响这一行，不会丢日志
        enqueue=True,            # 多线程 / 多进程安全
        backtrace=True,
        diagnose=False,          # 打开会把变量值写进日志，容易泄密，别开
        filter=_redact_record,   # 脱敏
    )

    # 只记错误的日志：出问题时第一个看这个
    _logger.add(
        log_dir / "error.log",
        level="ERROR",
        format=_FILE_FMT,
        rotation="10 MB",
        retention="90 days",
        encoding="utf-8",
        errors="backslashreplace",   # 编码兜底：再出现怪字符也只影响这一行，不会丢日志
        enqueue=True,
        backtrace=True,
        diagnose=False,
        filter=_redact_record,   # 脱敏
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