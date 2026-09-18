"""向量编码（4c）：fastembed + bge-small-zh，不引入 torch。

为什么单独一个文件：fastembed 是可选依赖。装不上只是"检索退回最近 N 条"，
记忆主流程一行不受影响 —— 和表情脸/眼睛跟随同一个"可选件静默降级"原则。

首次encode会从 HuggingFace 下载模型（约 90MB），只下一次，之后走本地缓存。
"""

from __future__ import annotations

import numpy as np

from ..logger import get_logger

logger = get_logger(__name__)


class Embedder:
    """文本 -> 向量。bge-small-zh 输出 512 维，已归一化（cosine = 点积）。"""

    def __init__(self, model_name: str) -> None:
        from fastembed import TextEmbedding  # 延迟导入：没装这个包也能 import 本模块

        self._model = TextEmbedding(model_name)
        dim = getattr(self._model, "embedding_size", None) or 512
        self.dim = int(dim)
        logger.info("向量编码器就绪 | 模型={} | 维度={}", model_name, self.dim)

    def encode(self, texts: list[str]) -> list[np.ndarray]:
        """一批文本 -> 向量列表。空字符串会给零向量，调用方自行过滤。"""
        clean = [(t or "").strip() for t in texts]
        vectors = list(self._model.embed(clean))
        return [np.asarray(v, dtype=np.float32) for v in vectors]


def cosine_scores(query: np.ndarray, candidates: list[tuple[int, np.ndarray]]) -> list[tuple[int, float]]:
    """query 向量对一批 (id, 向量) 算相似度，返回按分数降序的 (id, score)。

    纯函数：检索的核心就这一行点积，bge 输出已归一化，cosine 退化成了点积。
    """
    q = np.asarray(query, dtype=np.float32)
    scored = [(fid, float(np.dot(q, vec))) for fid, vec in candidates]
    scored.sort(key=lambda item: item[1], reverse=True)
    return scored
