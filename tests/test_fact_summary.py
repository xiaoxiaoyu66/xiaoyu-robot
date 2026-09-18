"""4b「事实积累」的测试。

改造前的情况：`facts` 表建好了、`add_fact()` 也写好了，
但**一个调用方都没有** —— 所以它永远记不住"你是谁"，
`recall()` 也永远返回空。

下面锁三件事：
    1. 模型吐出来的 JSON 能安全解析（它会套 ```围栏、会说客套话、会吐垃圾）
    2. 没聊够轮数时**绝不**调用 API —— 不能偷偷花钱，也不能卡住对话
    3. 它能把新事实写进库，并且把已有事实**连 id** 一起交给模型判重

不联网、不花钱：所有"模型的回复"都是假的。

    python -m unittest discover tests -v
"""

from __future__ import annotations

import dataclasses
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

try:
    import openai  # noqa: F401  （构造客户端需要它）
except ImportError:                                   # pragma: no cover
    openai = None

try:
    from xiaoyu.config import LlmConfig, MemoryConfig, Paths, Settings
    from xiaoyu.llm.client import DeepSeekClient, _parse_facts
    from xiaoyu.memory.store import MemoryStore
except ImportError:                                   # pragma: no cover
    DeepSeekClient = None
    _parse_facts = None
    MemoryStore = None
    Settings = None

_SKIP = "未安装 openai / loguru，跳过"


def _settings(db: Path, summarize_every: int = 20):
    """只改数据库路径和总结频率。api_key 是假值，只为过构造函数那一关。"""
    return Settings(
        paths=dataclasses.replace(Paths(), db=db),
        llm=dataclasses.replace(LlmConfig(), api_key="test"),
        memory=dataclasses.replace(MemoryConfig(), summarize_every=summarize_every),
    )


def _reply(text: str):
    """伪造一个 OpenAI 风格的非流式返回。"""
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))]
    )


@unittest.skipIf(_parse_facts is None, _SKIP)
class TestParseFacts(unittest.TestCase):
    """解析容错 —— 模型答应得好好的，但经常不老实。"""

    def test_plain_json(self):
        new, dup = _parse_facts('{"new_facts": ["主人叫小明"], "duplicates": ["主人是学生"]}')
        self.assertEqual(new, ["主人叫小明"])
        self.assertEqual(dup, ["主人是学生"])

    def test_fenced_json(self):
        """包在 ```json 围栏里也要能读出来。"""
        raw = '```json\n{"new_facts": ["主人在准备考研"], "duplicates": []}\n```'
        self.assertEqual(_parse_facts(raw)[0], ["主人在准备考研"])

    def test_chatty_prefix_and_suffix(self):
        """前面多一句客套话，后面多个句号，都得能抠出来。"""
        raw = '好的，我整理好了：\n{"new_facts": ["主人养了只猫"], "duplicates": []}\n还有别的吗？'
        self.assertEqual(_parse_facts(raw)[0], ["主人养了只猫"])

    def test_empty_string(self):
        self.assertEqual(_parse_facts(""), ([], []))

    def test_no_json_at_all(self):
        self.assertEqual(_parse_facts("我这就整理……然后忘了输出"), ([], []))

    def test_broken_json(self):
        self.assertEqual(_parse_facts('{"new_facts": ["少个引号]}'), ([], []))

    def test_missing_keys(self):
        self.assertEqual(_parse_facts("{}"), ([], []))

    def test_a_json_array_instead_of_an_object(self):
        self.assertEqual(_parse_facts('["主人叫小明"]'), ([], []))

    def test_items_may_be_objects(self):
        raw = '{"new_facts": [{"text": "主人叫小明"}, {"content": "主人在考研"}], "duplicates": []}'
        self.assertEqual(_parse_facts(raw)[0], ["主人叫小明", "主人在考研"])

    def test_blank_items_are_dropped(self):
        raw = '{"new_facts": ["主人叫小明", "   ", ""], "duplicates": []}'
        self.assertEqual(_parse_facts(raw)[0], ["主人叫小明"])

    def test_wrong_type_for_the_list(self):
        self.assertEqual(_parse_facts('{"new_facts": "主人叫小明"}'), ([], []))


@unittest.skipIf(MemoryStore is None, _SKIP)
class TestFactStorage(unittest.TestCase):
    """记忆库里事实的读写。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="xiaoyu_facts_")
        self.db = Path(self._tmp.name) / "xiaoyu.db"
        self.store = MemoryStore(_settings(self.db))

    def tearDown(self) -> None:
        try:
            self.store.close()
        except Exception:                             # pragma: no cover
            pass
        self._tmp.cleanup()

    def test_facts_with_id_is_ordered_oldest_first(self):
        self.store.add_facts(["第一件", "第二件", "第三件"])
        rows = self.store.facts_with_id()
        self.assertEqual([r["content"] for r in rows], ["第一件", "第二件", "第三件"])
        self.assertEqual([r["id"] for r in rows], sorted(r["id"] for r in rows))

    def test_facts_with_id_on_an_empty_table(self):
        self.assertEqual(self.store.facts_with_id(), [])

    def test_add_facts_skips_blanks(self):
        self.assertEqual(self.store.add_facts(["有效", "  ", ""]), 1)
        self.assertEqual(len(self.store.facts_with_id()), 1)

    def test_add_facts_dedupes_within_one_batch(self):
        """同一条事实在一次输出里出现两遍，只存一条。"""
        self.assertEqual(self.store.add_facts(["主人叫小明", "主人叫小明"]), 1)

    def test_add_facts_on_empty_input(self):
        self.assertEqual(self.store.add_facts([]), 0)


@unittest.skipIf(DeepSeekClient is None, _SKIP)
class FactSummaryTestCase(unittest.TestCase):
    """每个用例一个临时记忆库 + 一个换了假客户端的真 client。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="xiaoyu_summary_")
        self.db = Path(self._tmp.name) / "xiaoyu.db"
        self.store = MemoryStore(_settings(self.db))

    def tearDown(self) -> None:
        try:
            self.store.close()
        except Exception:                             # pragma: no cover
            pass
        self._tmp.cleanup()

    def _client(self, summarize_every: int = 3) -> "DeepSeekClient":
        client = DeepSeekClient(
            _settings(self.db, summarize_every=summarize_every), memory=self.store
        )
        # 把真正的 OpenAI 客户端换成假的 —— 绝不联网。
        client._client = mock.MagicMock()
        return client


class TestSummarizeFacts(FactSummaryTestCase):
    def test_does_nothing_before_enough_rounds(self):
        client = self._client(summarize_every=3)
        client._rounds_since_summary = 2
        self.assertEqual(client.summarize_facts(), [])
        client._client.chat.completions.create.assert_not_called()

    def test_summarizes_once_the_threshold_is_reached(self):
        client = self._client(summarize_every=2)
        client._rounds_since_summary = 2
        client._client.chat.completions.create.return_value = _reply(
            '{"new_facts": ["主人叫小明", "主人在准备考研"], "duplicates": []}'
        )
        self.store.remember_exchange("我叫小明，最近在准备考研", "记住啦")

        saved = client.summarize_facts()
        self.assertEqual(saved, ["主人叫小明", "主人在准备考研"])
        self.assertEqual(
            [r["content"] for r in self.store.facts_with_id()], saved
        )

    def test_force_ignores_the_threshold(self):
        client = self._client(summarize_every=100)
        client._rounds_since_summary = 0
        client._client.chat.completions.create.return_value = _reply(
            '{"new_facts": ["主人叫小明"], "duplicates": []}'
        )
        self.store.remember_exchange("我叫小明", "记住啦")
        self.assertEqual(client.summarize_facts(force=True), ["主人叫小明"])

    def test_counter_resets_after_a_run(self):
        client = self._client(summarize_every=2)
        client._rounds_since_summary = 2
        client._client.chat.completions.create.return_value = _reply(
            '{"new_facts": [], "duplicates": []}'
        )
        client.summarize_facts()
        self.assertEqual(client._rounds_since_summary, 0)

    def test_known_facts_go_to_the_model_with_their_ids(self):
        """判重靠「连 id 一起给」—— 这是 4b 的关键设计，别被后人删掉。"""
        self.store.add_facts(["主人叫小明"])
        client = self._client(summarize_every=1)
        client._rounds_since_summary = 1
        client._client.chat.completions.create.return_value = _reply(
            '{"new_facts": [], "duplicates": ["主人叫小明"]}'
        )
        self.store.remember_exchange("我叫小明", "记住啦")
        client.summarize_facts()

        kwargs = client._client.chat.completions.create.call_args.kwargs
        user_text = kwargs["messages"][-1]["content"]
        self.assertIn("1. 主人叫小明", user_text)
        self.assertIn("我叫小明", user_text, "最近的对话也要一起给它")

    def test_broken_json_breaks_nothing(self):
        client = self._client(summarize_every=1)
        client._rounds_since_summary = 1
        client._client.chat.completions.create.return_value = _reply("我忘了输出 JSON")
        self.store.remember_exchange("随便聊聊", "嗯嗯")

        self.assertEqual(client.summarize_facts(), [])
        self.assertEqual(self.store.facts_with_id(), [])

    def test_api_failure_is_swallowed(self):
        """整理失败不能影响正常聊天 —— 记忆是加分项，不是主流程。"""
        client = self._client(summarize_every=1)
        client._rounds_since_summary = 1
        client._client.chat.completions.create.side_effect = RuntimeError("boom")
        self.store.remember_exchange("在吗", "在")

        self.assertEqual(client.summarize_facts(), [])

    def test_empty_database_is_a_no_op(self):
        client = self._client(summarize_every=1)
        client._rounds_since_summary = 1
        self.assertEqual(client.summarize_facts(), [])
        client._client.chat.completions.create.assert_not_called()

    def test_works_without_a_memory_object(self):
        client = DeepSeekClient(_settings(self.db, summarize_every=1), memory=None)
        client._client = mock.MagicMock()
        self.assertEqual(client.summarize_facts(force=True), [])


class TestSummarizeAsync(FactSummaryTestCase):
    def test_returns_false_below_the_threshold(self):
        client = self._client(summarize_every=5)
        client._rounds_since_summary = 4
        self.assertFalse(client.summarize_facts_async())

    def test_returns_true_when_it_actually_starts(self):
        client = self._client(summarize_every=1)
        client._rounds_since_summary = 1
        client._client.chat.completions.create.return_value = _reply(
            '{"new_facts": ["主人叫小明"], "duplicates": []}'
        )
        self.store.remember_exchange("我叫小明", "记住啦")

        self.assertTrue(client.summarize_facts_async())
        # 等后台线程把库写完再断言
        for _ in range(50):
            if self.store.facts_with_id():
                break
            time.sleep(0.05)
        self.assertEqual([r["content"] for r in self.store.facts_with_id()], ["主人叫小明"])

    def test_one_run_at_a_time(self):
        """已经在整理了就别再起一个线程。"""
        client = self._client(summarize_every=1)
        client._rounds_since_summary = 1
        client._summary_running = True
        self.assertFalse(client.summarize_facts_async())


if __name__ == "__main__":
    unittest.main()
