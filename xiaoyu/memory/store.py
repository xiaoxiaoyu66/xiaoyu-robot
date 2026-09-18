"""记忆：SQLite 存对话，为 S4 的向量检索预留位置。

阶段说明：
    S2 只需要"最近 N 轮"（recent_messages），SQLite 就够。
    S4 再加向量检索（recall），把 bge-small-zh 的向量存进 vectors 表。
"""

from __future__ import annotations

import shutil
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
        # 先备份，再打开。顺序不能反：打开之后库可能已经被写过了。
        self._backup_once_a_day()
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

    def _backup_once_a_day(self) -> None:
        """每天第一次打开时，把记忆库复制一份。

        S4 之后这个库就是它全部的记忆，硬盘一坏、代码写错一次就永久失忆。
        一天一份、同名不覆盖 —— 因为"当天的第一份"通常才是干净的那份，
        同一天再开一次就把它盖掉，正好会把好备份换成坏备份。
        """
        if not self._path.exists() or self._path.stat().st_size == 0:
            return

        target = self._path.with_name(f"{self._path.name}.{_today()}.bak")
        if target.exists():
            logger.debug("今天的记忆库备份已存在，不覆盖：{}", target.name)
            return

        try:
            shutil.copy2(self._path, target)
        except OSError:
            # 备份是保险，不是前提：拷贝失败绝不能让程序起不来
            logger.warning("记忆库备份失败（不影响使用）：{}", target.name, exc_info=True)
            return

        logger.info(
            "已备份记忆库 → {}（{:.0f} KB）",
            target.name,
            target.stat().st_size / 1024,
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

    def add_facts(self, contents: list[str]) -> int:
        """一次存多条，返回真正写进去的条数（去空、去重）。

        为什么要去重：模型偶尔会把同一条事实拆成两条相似的输出，
        库里出现"主人喜欢猫"和"主人养了猫"这种近乎重复的条目，
        注入提示词时只会白占位置。
        """
        cleaned: list[str] = []
        seen: set[str] = set()
        for raw in contents:
            text = (raw or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            cleaned.append(text)

        if not cleaned:
            return 0

        self._conn.executemany(
            "INSERT INTO facts(content, created_at) VALUES (?, ?)",
            [(text, _now()) for text in cleaned],
        )
        self._conn.commit()
        for text in cleaned:
            logger.info("记住一件事：{}", text)
        return len(cleaned)

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------
    def recent_messages(
        self, limit: int = 20, include_time: bool = False
    ) -> list[dict[str, str]]:
        """最近 N 条对话，按时间正序返回（最早的在前，直接能塞进 prompt）。

        include_time=True 时会多带一个 created_at，供"上次聊天是昨天 21:30"
        这种时间感使用；默认不带，免得别的调用方被动多出一个字段。
        """
        columns = "role, content, created_at" if include_time else "role, content"
        rows = self._conn.execute(
            f"SELECT {columns} FROM messages ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [{key: r[key] for key in r.keys()} for r in reversed(rows)]

    def last_message_at(self) -> str | None:
        """最后一次对话的时间（不带上限地取最新一条）。空库返回 None。

        要在**本轮对话开始之前**取，取到的才是"上一次聊天"的时间。
        """
        row = self._conn.execute(
            "SELECT created_at FROM messages ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return str(row["created_at"]) if row else None

    def facts_with_id(self, limit: int = 50) -> list[dict[str, object]]:
        """已有的事实，带 id，按 id 正序（老的在前）。

        为什么要把 id 一并给模型：判重的时候，让模型直接说
        "这条和 id=3 重复"，比让它自己比对两段文字靠谱得多 ——
        而且出问题时我们能顺着 id 回查它到底在跟哪条比。
        """
        rows = self._conn.execute(
            "SELECT id, content FROM facts ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [{"id": int(r["id"]), "content": str(r["content"])} for r in reversed(rows)]

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


def _today() -> str:
    return time.strftime("%Y%m%d")