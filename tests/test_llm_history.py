"""4a「重启不失忆」的测试。

这一层以前完全没有测试。改造前的行为是：

    DeepSeekClient.__init__ 里 self._history = []
    —— 空的开局，而 _build_messages() 只用 self._history。
    所以库里明明存着 26 条对话，关掉程序再开，它对你一无所知。

下面这组测试锁的就是这个行为不再发生。不联网、不花钱：
只构造客户端 + 读本地 SQLite，从不真的发请求。

    python -m unittest discover tests -v
"""

from __future__ import annotations

import dataclasses
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

try:
    import openai  # noqa: F401  （只是为了确认依赖在，构造客户端需要它）
except ImportError:                                   # pragma: no cover
    openai = None

try:
    from xiaoyu.config import LlmConfig, Paths, Settings
    from xiaoyu.llm.client import DeepSeekClient, _speakable_error, _trim_dangling
    from xiaoyu.memory.store import MemoryStore
except ImportError:                                   # pragma: no cover
    DeepSeekClient = None
    _speakable_error = None
    _trim_dangling = None
    MemoryStore = None
    LlmConfig = None
    Paths = None
    Settings = None

_SKIP = "未安装 openai / loguru，跳过"


def _settings(db: Path):
    """只改数据库路径的配置。api_key 是假值，只为了过构造函数那一关。"""
    return Settings(
        paths=dataclasses.replace(Paths(), db=db),
        llm=dataclasses.replace(LlmConfig(), api_key="test"),
    )


@unittest.skipIf(_trim_dangling is None, _SKIP)
class TestTrimDangling(unittest.TestCase):
    """读回来的历史里，收尾说不通的部分要清掉。"""

    def test_empty_in_empty_out(self):
        self.assertEqual(_trim_dangling([]), [])

    def test_drops_messages_with_blank_content(self):
        rows = [
            {"role": "user", "content": "正常"},
            {"role": "assistant", "content": "   "},
            {"role": "user", "content": ""},
            {"role": "assistant", "content": "也有内容"},
        ]
        self.assertEqual([r["content"] for r in _trim_dangling(rows)], ["正常", "也有内容"])

    def test_drops_a_trailing_user_message_without_a_reply(self):
        """上一轮聊到一半程序被关了 —— 留着它会变成连着两条 user。"""
        rows = [
            {"role": "user", "content": "完整的一轮"},
            {"role": "assistant", "content": "嗯"},
            {"role": "user", "content": "这句没等到回复"},
        ]
        self.assertEqual(
            [r["content"] for r in _trim_dangling(rows)], ["完整的一轮", "嗯"]
        )

    def test_keeps_a_trailing_assistant_message(self):
        rows = [
            {"role": "user", "content": "早"},
            {"role": "assistant", "content": "早啊"},
        ]
        self.assertEqual(len(_trim_dangling(rows)), 2)

    def test_never_wipes_the_whole_history(self):
        """数据异常（整段都是 user）时，只丢结尾一条。

        如果这里用循环丢弃，一段脏数据就会让"记忆"整个消失 ——
        静默清空比留着一条脏数据糟糕得多。
        """
        rows = [{"role": "user", "content": f"第 {i} 句"} for i in range(5)]
        self.assertEqual(len(_trim_dangling(rows)), 4)

    def test_does_not_touch_the_middle(self):
        rows = [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
            {"role": "user", "content": "c"},
            {"role": "assistant", "content": "d"},
        ]
        self.assertEqual(_trim_dangling(rows), rows)


@unittest.skipIf(_speakable_error is None, _SKIP)
class TestSpeakableError(unittest.TestCase):
    """认得出原因的失败，要能说成人话，而不是一声不吭。"""

    def test_authentication_failure(self):
        self.assertIn("钥匙", _speakable_error(Exception("Error code: 401 - invalid api key")))

    def test_exception_class_name_alone_is_enough(self):
        class AuthenticationError(Exception):
            pass

        self.assertIn("钥匙", _speakable_error(AuthenticationError("boom")))

    def test_connection_failure(self):
        class APIConnectionError(Exception):
            pass

        self.assertIn("连不上", _speakable_error(APIConnectionError("boom")))

    def test_timeout_by_message(self):
        self.assertIn("连不上", _speakable_error(Exception("Request timed out")))

    def test_rate_limit(self):
        self.assertIn("喘口气", _speakable_error(Exception("Error code: 429")))

    def test_running_out_of_money(self):
        self.assertIn("欠费", _speakable_error(Exception("Insufficient Balance")))

    def test_unknown_error_is_not_papered_over(self):
        """认不出来就是真 bug，返回 None 让上层记堆栈，别用一句好话盖住。"""
        self.assertIsNone(_speakable_error(KeyError("'foo'")))


@unittest.skipIf(DeepSeekClient is None, _SKIP)
class LlmTestCase(unittest.TestCase):
    """每个用例一个临时记忆库，客户端是真构造的（但从不发请求）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="xiaoyu_llm_")
        self.db = Path(self._tmp.name) / "xiaoyu.db"
        self.store = MemoryStore(_settings(self.db))

    def tearDown(self) -> None:
        try:
            self.store.close()
        except Exception:                             # pragma: no cover
            pass
        self._tmp.cleanup()

    def _client(self, memory: bool = True) -> "DeepSeekClient":
        return DeepSeekClient(_settings(self.db), memory=self.store if memory else None)

    @staticmethod
    def _system_text(client) -> str:
        messages = client._build_messages("在吗")
        return "\n".join(m["content"] for m in messages if m["role"] == "system")


class TestRestoreHistory(LlmTestCase):
    def test_empty_database_starts_clean(self):
        self.assertEqual(self._client().history, [])

    def test_works_without_a_memory_object(self):
        """memory=None 是调试路径，不能因为读不到库就崩。"""
        self.assertEqual(self._client(memory=False).history, [])

    def test_restart_remembers_the_previous_session(self):
        """4a 的验收核心。"""
        self.store.remember_exchange("我下周三要交简历", "记下了，别忘了。")
        restarted = self._client()
        self.assertEqual(
            [m["content"] for m in restarted.history],
            ["我下周三要交简历", "记下了，别忘了。"],
        )

    def test_restored_history_keeps_the_original_order(self):
        self.store.remember_exchange("第一句", "回第一句")
        self.store.remember_exchange("第二句", "回第二句")
        contents = [m["content"] for m in self._client().history]
        self.assertEqual(contents, ["第一句", "回第一句", "第二句", "回第二句"])

    def test_only_the_recent_window_is_restored(self):
        """历史不能无限读回来，读多少由 max_history 决定。"""
        for i in range(25):
            self.store.remember_exchange(f"第 {i} 句", f"第 {i} 答")
        client = self._client()
        self.assertEqual(len(client.history), client._cfg.max_history)
        self.assertEqual(client.history[-1]["content"], "第 24 答")

    def test_a_dangling_last_turn_is_dropped_on_restore(self):
        self.store.add_message("user", "完整的一轮")
        self.store.add_message("assistant", "嗯")
        self.store.add_message("user", "这句没等到回复就关了")
        self.assertEqual(
            [m["content"] for m in self._client().history], ["完整的一轮", "嗯"]
        )

    def test_restored_history_actually_reaches_the_prompt(self):
        """读回 _history 还不够，得真的出现在发给模型的 messages 里。"""
        self.store.remember_exchange("我下周三要交简历", "记下了，别忘了。")
        messages = self._client()._build_messages("我最近有啥事")

        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[-1], {"role": "user", "content": "我最近有啥事"})
        contents = [m["content"] for m in messages]
        self.assertIn("我下周三要交简历", contents)
        self.assertIn("记下了，别忘了。", contents)

    def test_history_grows_within_a_session(self):
        client = self._client()
        client._history.append({"role": "user", "content": "本次会话第一句"})
        client._history.append({"role": "assistant", "content": "回一句"})
        contents = [m["content"] for m in client._build_messages("接着聊")]
        self.assertIn("本次会话第一句", contents)


class TestTimeSense(LlmTestCase):
    def test_prompt_says_what_time_it_is_now(self):
        system = self._system_text(self._client())
        self.assertIn("现在是", system)
        self.assertIn(f"{datetime.now():%Y-%m-%d}", system)

    def test_fresh_database_does_not_claim_a_previous_chat(self):
        self.assertNotIn("上一次聊天", self._system_text(self._client()))

    def test_previous_chat_time_is_mentioned(self):
        self.store.remember_exchange("早", "早啊")
        system = self._system_text(self._client())
        self.assertIn("上一次聊天", system)
        self.assertIn("刚才", system, "刚刚写完的对话，应该说成「刚才」而不是一个日期")

    def test_clock_is_read_fresh_on_every_turn(self):
        """时间必须每轮现取。

        启动时算一次存着的话，下午开机、晚上聊天，
        它会一直坚持说"现在是 14:00"。
        """
        client = self._client()

        with mock.patch("xiaoyu.llm.client.datetime") as fake:
            fake.now.return_value = datetime(2026, 1, 1, 9, 0)
            first = client._time_sense_message()["content"]

        with mock.patch("xiaoyu.llm.client.datetime") as fake:
            fake.now.return_value = datetime(2026, 1, 2, 21, 30)
            second = client._time_sense_message()["content"]

        self.assertIn("2026-01-01", first)
        self.assertIn("2026-01-02", second)
        self.assertNotEqual(first, second)

    def test_time_sense_never_crashes_on_a_broken_timestamp(self):
        """库里时间戳坏了，最多少一句时间感，不能让整轮对话崩掉。"""
        self.store.add_message("user", "时间戳坏掉的那种")
        client = self._client()
        client._last_seen_at = "不是时间"
        system = self._system_text(client)
        self.assertIn("现在是", system)
        self.assertNotIn("上一次聊天", system)


if __name__ == "__main__":
    unittest.main()
