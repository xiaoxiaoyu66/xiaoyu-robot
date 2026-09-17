"""检查 config/ 下的文本配置是否还在、格式对不对。

不需要联网，也不需要装任何依赖。
"""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class TestConfigFiles(unittest.TestCase):
    def test_keywords_file_format(self):
        path = ROOT / "config" / "keywords.txt"
        self.assertTrue(path.exists(), "config/keywords.txt 不见了")
        lines = [l.strip() for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
        self.assertTrue(lines, "keywords.txt 是空的，至少写一行唤醒词")
        for line in lines:
            self.assertIn("@", line, f"这一行缺 @显示名：{line}")

    def test_persona_file_not_empty(self):
        path = ROOT / "config" / "persona.md"
        self.assertTrue(path.exists(), "config/persona.md 不见了")
        self.assertGreater(len(path.read_text(encoding="utf-8").strip()), 20, "性格描述太短了")


if __name__ == "__main__":
    unittest.main()