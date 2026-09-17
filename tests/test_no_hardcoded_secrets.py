"""兜底防线：项目里永远不许出现硬编码密钥。

每次跑测试都会扫一遍全项目，发现像真密钥的字符串就失败。
这样以后无论是你还是我写的代码，都不可能把密钥悄悄提交上去。

    python -m unittest discover tests -v
"""

from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 不扫的目录
SKIP_DIRS = {
    ".git", ".venv", "venv", "__pycache__", "node_modules",
    ".idea", ".vscode", "logs", "models", "data",
}

# 不扫的文件：.env 是本地真实密钥的存放处，本来就该有密钥
SKIP_FILES = {".env", ".env.local"}

SKIP_SUFFIX = {
    ".zip", ".bz2", ".onnx", ".db", ".mp3", ".wav", ".png",
    ".jpg", ".jpeg", ".ico", ".exe", ".pyc", ".gif",
}

# 只匹配"像真密钥"的：DeepSeek 的 key 是 sk- 加 32 位左右随机串。
# 故意写 24 位以上，避免把文档里的占位符 sk-你的key 误判成密钥。
PATTERNS = (
    ("疑似 API Key", re.compile(r"sk-[A-Za-z0-9]{24,}")),
    (
        "疑似写死的密钥",
        re.compile(
            r"(?i)\b(api[_-]?key|apikey|secret|token|password)\b\s*[=:]\s*"
            r"[\"'][A-Za-z0-9_\-]{24,}[\"']"
        ),
    ),
)

try:
    from xiaoyu.logger import _redact
except ImportError:          # 没装 loguru 时跳过相关测试
    _redact = None


def iter_project_files():
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.name in SKIP_FILES:
            continue
        if path.suffix.lower() in SKIP_SUFFIX:
            continue
        yield path


class TestNoHardcodedSecrets(unittest.TestCase):
    def test_no_secret_like_string_in_project(self):
        offenders: list[str] = []
        for path in iter_project_files():
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue          # 二进制或读不了，跳过
            for label, pattern in PATTERNS:
                for match in pattern.finditer(text):
                    line = text[: match.start()].count("\n") + 1
                    offenders.append(f"{path.relative_to(ROOT)}:{line}  {label}")

        self.assertEqual(
            offenders,
            [],
            "发现疑似硬编码密钥，请改用环境变量或 .env：\n  " + "\n  ".join(offenders),
        )

    def test_env_file_is_gitignored(self):
        gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn(".env", gitignore, ".env 必须在 .gitignore 里")

    def test_env_file_is_not_tracked_by_git(self):
        result = subprocess.run(
            ["git", "ls-files", ".env"], cwd=ROOT, capture_output=True, text=True
        )
        self.assertEqual(
            result.stdout.strip(), "", ".env 被 git 跟踪了，密钥会泄露出去"
        )

    def test_config_reads_key_from_environment(self):
        """密钥必须来自环境变量，不能在代码里写死。"""
        source = (ROOT / "xiaoyu" / "config.py").read_text(encoding="utf-8")
        self.assertIn("DEEPSEEK_API_KEY", source)
        self.assertIn("os.environ", source)

    @unittest.skipIf(_redact is None, "未安装 loguru，跳过脱敏测试")
    def test_logger_redacts_secrets(self):
        """误把密钥打进日志时，必须被脱敏而不是原样写盘。"""
        fake_key = "sk-" + "a1b2c3d4" * 4
        redacted = _redact(f"正在使用 key={fake_key} 连接")
        self.assertNotIn(fake_key, redacted)
        self.assertIn("<已脱敏>", redacted)

    @unittest.skipIf(_redact is None, "未安装 loguru，跳过脱敏测试")
    def test_logger_redacts_bearer_token(self):
        redacted = _redact("Authorization: Bearer abcdef1234567890xyz")
        self.assertNotIn("abcdef1234567890xyz", redacted)

    @unittest.skipIf(_redact is None, "未安装 loguru，跳过脱敏测试")
    def test_logger_keeps_normal_text_intact(self):
        """脱敏不能误伤正常日志。"""
        normal = "录音结束 | 时长=2.31 秒 | 峰值=0.482"
        self.assertEqual(_redact(normal), normal)




class TestRedactFilter(unittest.TestCase):
    """过滤器本身的行为测试。

    只测 _redact() 是不够的 —— 过滤器一旦抛异常，loguru 会把整条日志丢掉，
    结果就是"日志系统静默瘫痪"，比泄密更难查。这里把真实链路跑一遍。
    """

    @unittest.skipIf(_redact is None, "未安装 loguru，跳过")
    def test_filter_does_not_raise_when_args_key_missing(self):
        from xiaoyu.logger import _redact_record

        record = {"message": "普通日志", "exception": None}   # 故意不带 args 键
        self.assertTrue(_redact_record(record), "过滤器必须返回 True")

    @unittest.skipIf(_redact is None, "未安装 loguru，跳过")
    def test_filter_keeps_normal_records(self):
        import io

        from loguru import logger as raw_logger

        from xiaoyu.logger import _redact_record

        raw_logger.remove()   # 去掉 loguru 默认 handler，避免测试输出里夹带堆栈
        buffer = io.StringIO()
        sink_id = raw_logger.add(buffer, format="{message}", filter=_redact_record, level="DEBUG")
        try:
            raw_logger.info("录音结束，时长 2.31 秒")
            raw_logger.info("key={}", "sk-" + "f" * 32)
        finally:
            raw_logger.remove(sink_id)

        output = buffer.getvalue()
        self.assertIn("录音结束", output, "正常日志被过滤器吞掉了")
        self.assertNotIn("f" * 24, output, "密钥没被脱敏")
        self.assertIn("<已脱敏>", output)

    @unittest.skipIf(_redact is None, "未安装 loguru，跳过")
    def test_filter_survives_exception_with_secret(self):
        import io

        from loguru import logger as raw_logger

        from xiaoyu.logger import _redact_record

        raw_logger.remove()   # 去掉 loguru 默认 handler，避免测试输出里夹带堆栈
        buffer = io.StringIO()
        sink_id = raw_logger.add(
            buffer, format="{message}", filter=_redact_record, level="DEBUG", backtrace=False
        )
        try:
            try:
                raise RuntimeError("认证失败: " + "sk-" + "e" * 32)
            except RuntimeError:
                raw_logger.exception("连接出错")
        finally:
            raw_logger.remove(sink_id)

        self.assertNotIn("e" * 24, buffer.getvalue(), "异常信息里的密钥泄漏了")

if __name__ == "__main__":
    unittest.main()
