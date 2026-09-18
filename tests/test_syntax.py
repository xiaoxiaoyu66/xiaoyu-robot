"""全项目语法检查。

为什么需要这个：
    scripts/ 下的脚本平时不会被执行，很容易藏语法错误。
    真实案例：check_audio.py 里中文引号误写成了 ASCII 双引号，

        logger.error("  4. ...并关掉"自动增益"试试")

    字符串被提前截断，直到第一次运行才炸出来。
    这个测试把项目里每个 .py 都解析一遍，让这类错误不可能溜过去。
"""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# .scratch 是记了 gitignore 的草稿区（冒烟脚本、一次性探针），
# 不该让一个写了一半的实验文件把整个项目卡红。
# 真实案例：验证情绪回调时在里面留了个 app.py 的备份副本，
# 它自己的 DeprecationWarning 就跑进了测试输出里，看着像生产代码在告警。
SKIP_DIRS = {
    ".git", ".venv", "venv", "__pycache__", "node_modules",
    "models", "data", "logs", ".scratch", ".pytest_cache",
}


class TestSyntax(unittest.TestCase):
    def test_every_python_file_parses(self):
        broken: list[str] = []
        checked = 0

        for path in sorted(ROOT.rglob("*.py")):
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            checked += 1
            try:
                ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except SyntaxError as exc:
                broken.append(f"{path.relative_to(ROOT)}:{exc.lineno}  {exc.msg}")

        self.assertGreater(checked, 10, f"只扫到 {checked} 个文件，路径可能不对")
        self.assertEqual(broken, [], "以下文件有语法错误：\n  " + "\n  ".join(broken))

    def test_scripts_are_importable(self):
        """脚本至少能被 import —— 语法对但 import 不存在的东西也要拦住。"""
        import importlib.util

        scripts_dir = ROOT / "scripts"
        if not scripts_dir.exists():
            self.skipTest("没有 scripts 目录")

        failures: list[str] = []
        for path in sorted(scripts_dir.glob("*.py")):
            if path.name == "make_keywords.py":
                continue          # 它依赖可选的 pypinyin，单独测

            name = f"_probe_{path.stem}"
            spec = importlib.util.spec_from_file_location(name, path)
            module = importlib.util.module_from_spec(spec)
            # 必须先注册到 sys.modules：@dataclass 会通过 cls.__module__
            # 反查 sys.modules，查不到就抛 AttributeError
            sys.modules[name] = module
            try:
                spec.loader.exec_module(module)
            except SystemExit:
                pass
            except Exception as exc:
                failures.append(f"{path.name}: {type(exc).__name__}: {exc}")
            finally:
                sys.modules.pop(name, None)

        self.assertEqual(failures, [], "脚本导入失败：\n  " + "\n  ".join(failures))


if __name__ == "__main__":
    unittest.main()