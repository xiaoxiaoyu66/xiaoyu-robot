"""记忆：SQLite 存对话，为 S4 的向量检索预留位置。

阶段说明：
    S2 只需要"最近 N 轮"（recent_messages），SQLite 就够。
    S4 再加向量检索（recall），把 bge-small-zh 的向量存进 vectors 表。
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from ..config import Settings
from ..logger import get_logger

logger = get_logger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    role       TEXT NOT NULL,
    content    TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_created ON messages(created_at);

CREATE TABLE IF NOT EXISTS facts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    content    TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- S4 才会用到：把每句话的向量存这里
CREATE TABLE IF NOT EXISTS vectors (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER,
    dim        INTEGER NOT NULL,
    embedding  BLOB NOT NULL,
    FOREIGN KEY (message_id) REFERENCES messages(id)
);
"""


class MemoryStore:
    """长期记忆。现在能存能取，向量检索等 S4 再补。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._path: Path = settings.paths.db
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        logger.info(
            "记忆库就绪 | {} | 已有对话 {} 条 / 事实 {} 条",
            self._path.name,
            self._count("messages"),
            self._count("facts"),
        )

    def _count(self, table: str) -> int:
        row = self._conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
        return int(row["n"])

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------
    def add_message(self, role: str, content: str) -> int:
        cursor = self._conn.execute(
            "INSERT INTO messages(role, content, created_at) VALUES (?, ?, ?)",
            (role, content, _now()),
        )
        self._conn.commit()
        return int(cursor.lastrowid)

    def remember_exchange(self, user_text: str, reply: str) -> None:
        """把一轮对话存下来。"""
        self.add_message("user", user_text)
        self.add_message("assistant", reply)
        logger.debug("已记录一轮对话（{} 字 / {} 字）", len(user_text), len(reply))

    def add_fact(self, content: str) -> None:
        self._conn.execute(
            "INSERT INTO facts(content, created_at) VALUES (?, ?)", (content, _now())
        )
        self._conn.commit()
        logger.info("记住一件事：{}", content)

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------
    def recent_messages(self, limit: int = 20) -> list[dict[str, str]]:
        rows = self._conn.execute(
            "SELECT role, content FROM messages ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

    def recent_facts(self, limit: int = 10) -> list[str]:
        rows = self._conn.execute(
            "SELECT content FROM facts ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [r["content"] for r in rows]

    def recall(self, query: str, top_k: int = 3) -> list[str]:
        """S4 的接口。现在先退化成"最近记得的事实"，保证流程能跑通。"""
        logger.debug("检索记忆（当前为占位实现，S4 换成向量检索）：{}", query[:20])
        return self.recent_facts(limit=top_k)

    def close(self) -> None:
        self._conn.close()
        logger.debug("记忆库已关闭")


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")