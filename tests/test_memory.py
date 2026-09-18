"""记忆库（SQLite）的测试。

为什么这组测试必须先于 S4 存在：

    S4 的三个台阶（4a 读回历史 / 4b 抽事实 / 4c 向量检索）全都要动
    `memory/store.py` 和 `llm/client.py`，而这一块以前**一个测试都没有**
    （全仓库搜 MemoryStore 只搜得到定义处）。没有回归网就改记忆，
    改完分不清是"新功能没生效"还是"顺手把老的路堵了"。

    4a 的核心承诺是"关掉程序再开，它还记得你" ——
    下面 TestSurvivesRestart 就是这个承诺的直接验收。

刻意做到零依赖：只用标准库 + 临时目录，不碰 .env、不碰真实的 data/。
    python -m unittest discover tests -v
"""

from __future__ import annotations

import dataclasses
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

try:
    from xiaoyu.config import Paths, Settings
    from xiaoyu.memory.store import MemoryStore
    from xiaoyu.text import parse_timestamp
except ImportError:                                   # pragma: no cover
    Paths = None
    Settings = None
    MemoryStore = None
    parse_timestamp = None

_SKIP = "未安装 loguru，跳过"


def _settings(db: Path):
    """造一份只改数据库路径的配置，别的都保持默认。"""
    return Settings(paths=dataclasses.replace(Paths(), db=db))


def _backup_path(db: Path) -> Path:
    """当天备份文件应该长什么样。"""
    return db.with_name(f"{db.name}.{datetime.now():%Y%m%d}.bak")


@unittest.skipIf(MemoryStore is None, _SKIP)
class MemoryStoreTestCase(unittest.TestCase):
    """每个用例一个全新的临时目录，互不干扰。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="xiaoyu_mem_")
        self.db = Path(self._tmp.name) / "xiaoyu.db"
        self.store = MemoryStore(_settings(self.db))

    def tearDown(self) -> None:
        self._close(self.store)
        self._tmp.cleanup()

    @staticmethod
    def _close(store) -> None:
        try:
            store.close()
        except Exception:                             # pragma: no cover
            pass


class TestMessages(MemoryStoreTestCase):
    def test_add_message_returns_increasing_id(self):
        first = self.store.add_message("user", "第一句")
        second = self.store.add_message("assistant", "第二句")
        self.assertEqual((first, second), (1, 2))

    def test_recent_messages_comes_back_in_chronological_order(self):
        """库里是按 id 倒着取的，还给模型的必须正回来，否则对话是倒放的。"""
        for i in range(3):
            self.store.add_message("user", f"第 {i} 句")
        rows = self.store.recent_messages()
        self.assertEqual([r["content"] for r in rows], ["第 0 句", "第 1 句", "第 2 句"])

    def test_recent_messages_limit_keeps_the_newest(self):
        for i in range(5):
            self.store.add_message("user", f"第 {i} 句")
        rows = self.store.recent_messages(limit=2)
        self.assertEqual([r["content"] for r in rows], ["第 3 句", "第 4 句"])

    def test_default_shape_is_exactly_role_and_content(self):
        """4a 直接把这个列表塞进 prompt。字段一旦变成别的，这里必须立刻炸。

        默认不带 created_at 是有意的：老的调用方不该被动多出一个字段。
        """
        self.store.add_message("user", "你好")
        self.assertEqual(set(self.store.recent_messages()[0]), {"role", "content"})

    def test_include_time_carries_a_parseable_created_at(self):
        self.store.add_message("user", "带时间的")
        row = self.store.recent_messages(include_time=True)[0]
        self.assertEqual(set(row), {"role", "content", "created_at"})
        self.assertIsNotNone(
            parse_timestamp(row["created_at"]), f"时间戳要能被解析：{row['created_at']!r}"
        )

    def test_remember_exchange_stores_both_sides_in_order(self):
        self.store.remember_exchange("我下周三要交简历", "记下了，别忘了。")
        rows = self.store.recent_messages()
        self.assertEqual([r["role"] for r in rows], ["user", "assistant"])
        self.assertEqual(rows[0]["content"], "我下周三要交简历")
        self.assertEqual(rows[1]["content"], "记下了，别忘了。")

    def test_content_with_quotes_and_newlines_survives(self):
        """中文 + ASCII 双引号 + 换行，一个都不能被吞（这是踩过的坑）。"""
        weird = '他说："今天「不」错"，换行\n第二行'
        self.store.add_message("user", weird)
        self.assertEqual(self.store.recent_messages()[0]["content"], weird)

    def test_empty_db_returns_nothing(self):
        self.assertEqual(self.store.recent_messages(), [])
        self.assertIsNone(self.store.last_message_at())


class TestLastMessageAt(MemoryStoreTestCase):
    def test_returns_the_newest_timestamp(self):
        self.store.add_message("user", "早")
        self.store.add_message("assistant", "嗯")
        newest = self.store.recent_messages(include_time=True)[-1]["created_at"]
        self.assertEqual(self.store.last_message_at(), newest)


class TestSurvivesRestart(MemoryStoreTestCase):
    """4a 的验收核心：进程关了再开，记忆还在，而且真的读得回来。"""

    def test_history_survives_a_restart(self):
        self.store.remember_exchange("我下周三要交简历", "记下了，别忘了。")
        self._close(self.store)

        reopened = MemoryStore(_settings(self.db))
        try:
            rows = reopened.recent_messages()
            self.assertEqual([r["role"] for r in rows], ["user", "assistant"])
            self.assertEqual(rows[0]["content"], "我下周三要交简历")
            self.assertIsNotNone(reopened.last_message_at(), "重启后要能说出上次聊天的时间")
        finally:
            self._close(reopened)

    def test_reopening_does_not_duplicate(self):
        self.store.add_message("user", "只存一次")
        self._close(self.store)

        again = MemoryStore(_settings(self.db))
        try:
            self.assertEqual(len(again.recent_messages()), 1)
        finally:
            self._close(again)

    def test_old_rows_are_kept_beyond_the_history_window(self):
        """读回 prompt 只取最近 N 条，但库里不能只留 N 条 —— 那是删记忆。"""
        for i in range(30):
            self.store.add_message("user", f"第 {i} 句")
        self._close(self.store)

        reopened = MemoryStore(_settings(self.db))
        try:
            self.assertEqual(len(reopened.recent_messages(limit=5)), 5)
            self.assertEqual(len(reopened.recent_messages(limit=999)), 30)
        finally:
            self._close(reopened)


class TestFactsAndRecall(MemoryStoreTestCase):
    def test_facts_start_empty(self):
        """4b 之前 facts 表就该是空的 —— 这条也是 4b 的出发点。"""
        self.assertEqual(self.store.recent_facts(), [])

    def test_recall_is_a_placeholder_until_4c(self):
        """现在的 recall() 只是"最近的事实"，查什么跟返回什么没关系。

        这一条是给 4c 留的标记：换成向量检索时要连这个测试一起改，
        别让它悄悄变成"看起来还在跑、其实早就不检索了"。
        """
        self.assertEqual(self.store.recall("随便问点啥"), [])
        self.store.add_fact("主人是 Java 出身的大四学生")
        self.assertEqual(self.store.recall("主人是学什么的"), ["主人是 Java 出身的大四学生"])

    def test_recall_respects_top_k_and_prefers_recent_facts(self):
        for i in range(5):
            self.store.add_fact(f"事实 {i}")
        self.assertEqual(self.store.recall("x", top_k=2), ["事实 4", "事实 3"])

    def test_facts_survive_a_restart(self):
        self.store.add_fact("主人在准备考研")
        self._close(self.store)

        reopened = MemoryStore(_settings(self.db))
        try:
            self.assertEqual(reopened.recent_facts(), ["主人在准备考研"])
        finally:
            self._close(reopened)


class TestBackup(MemoryStoreTestCase):
    """S4 之后这个库就是它全部的记忆，所以每天第一次打开时留一份。"""

    def test_first_run_creates_no_backup(self):
        """空库/新建库没什么可备份的，别在 data/ 里拉一堆垃圾。"""
        self.assertFalse(_backup_path(self.db).exists())

    def test_second_open_creates_todays_backup(self):
        self.store.add_message("user", "值得备份的一句")
        self._close(self.store)

        self._close(MemoryStore(_settings(self.db)))
        self.assertTrue(_backup_path(self.db).exists(), "第二次打开应该留一份当天的备份")

    def test_same_day_backup_is_never_overwritten(self):
        """当天第一份通常才是干净的那份 —— 覆盖它等于把好备份换成坏备份。"""
        self.store.add_message("user", "第一版")
        self._close(self.store)
        self._close(MemoryStore(_settings(self.db)))

        backup = _backup_path(self.db)
        before = backup.read_bytes()

        self.store = MemoryStore(_settings(self.db))
        self.store.add_message("user", "第二版")
        self._close(self.store)
        self.store = MemoryStore(_settings(self.db))
        self._close(self.store)

        self.assertEqual(backup.read_bytes(), before, "当天的备份不该被后来的覆盖")

    def test_backup_failure_does_not_stop_startup(self):
        """备份是保险，不是前提：拷不出来也得能正常起来。"""
        self.store.add_message("user", "随便一句")
        self._close(self.store)

        blocker = _backup_path(self.db)
        blocker.mkdir()                               # 用同名目录把备份路径堵死

        store = MemoryStore(_settings(self.db))       # 不许抛异常
        try:
            self.assertEqual(len(store.recent_messages()), 1)
        finally:
            self._close(store)


if __name__ == "__main__":
    unittest.main()
