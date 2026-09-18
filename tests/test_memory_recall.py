"""4c 向量检索的纯逻辑测试：cosine 排序 + recall 降级路径。

跑法：python -m unittest discover tests
fastembed 真编码的验证在 .scratch/smoke_recall.py（要下载模型，不进单测）。
"""

from __future__ import annotations

import dataclasses
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from xiaoyu.memory.vectors import cosine_scores


def _unit(v):
    a = np.asarray(v, dtype=np.float32)
    return a / (np.linalg.norm(a) + 1e-9)


class CosineScoresTest(unittest.TestCase):
    def test_sorted_descending(self):
        q = _unit([1.0, 0.0])
        cands = [(1, _unit([0.9, 0.1])), (2, _unit([-1.0, 0.0])), (3, _unit([0.5, 0.5]))]
        scored = cosine_scores(q, cands)
        ids = [fid for fid, _ in scored]
        self.assertEqual(ids[0], 1)       # 最像的排最前
        self.assertEqual(ids[-1], 2)      # 反方向排最后
        self.assertGreater(scored[0][1], scored[-1][1])

    def test_identical_is_highest(self):
        v = _unit([0.6, 0.8])
        scored = cosine_scores(v, [(7, v)])
        self.assertEqual(scored[0][0], 7)
        self.assertAlmostEqual(scored[0][1], 1.0, places=5)


class RecallFallbackTest(unittest.TestCase):
    def test_recall_falls_back_without_fastembed(self):
        """fastembed 没装时 recall 必须退回"最近事实"，不抛异常。"""
        from xiaoyu.config import Paths, Settings
        from xiaoyu.memory.store import MemoryStore

        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                paths=dataclasses.replace(Paths(), db=Path(tmp) / "test.db")
            )
            store = MemoryStore(settings)
            try:
                store.add_facts(["主人叫小林", "小林在准备考研"])
                with mock.patch(
                    "xiaoyu.memory.vectors.Embedder",
                    side_effect=ImportError("no fastembed"),
                ):
                    result = store.recall("他叫什么名字", top_k=1)
                self.assertTrue(result)   # 退回最近事实，至少能拿到东西
            finally:
                store.close()
