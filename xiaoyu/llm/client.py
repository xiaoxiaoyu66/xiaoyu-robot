"""大模型对话：DeepSeek 流式接口。

四个关键设计：
    1. 流式 + 按句切分（切分逻辑在 xiaoyu/text.py）。
       回答一出句号就送去合成，不等整段生成完，
       这样首句才能在 2.5 秒内出声。
    2. 性格写在 config/persona.md 里，改人格不用动代码。
    3. 启动时把上次的对话从 SQLite 读回来（4a）——
       数据一直都在库里，以前只是没人读回去。
    4. 有连接预热。慢的那 1.5 秒全在建 TCP + TLS，不在"想得久"。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from datetime import datetime

from ..config import Settings
from ..logger import get_logger
from ..text import describe_last_seen, describe_now, read_text, sanitize, split_sentences

logger = get_logger(__name__)

# 预热用的话。故意极短：目的只是把 TCP + TLS 连接建起来，
# 并不需要它真的回答什么。配合 max_tokens=1，几乎不花钱。
_WARMUP_PROMPT = "在吗"

# 距上次预热不到这么久就不重复预热。DeepSeek 的保活时间比这个长，
# 多打的每一次都是白花的钱。
_WARMUP_REUSE_SECONDS = 60.0

_FALLBACK_PERSONA = "你是小宇，一台放在书桌上的陪伴机器人。说话简短、自然，像朋友聊天。"

# 认得出原因时说哪句。说不出原因就当它是真 bug，交给上层记堆栈。
_DEGRADED_AUTH = "等等，我的钥匙好像不对，让主人看一眼配置。"
_DEGRADED_MONEY = "我这边欠费了，充点钱我就能接着聊。"
_DEGRADED_NETWORK = "我这会儿连不上脑子了，等一下再喊我。"
_DEGRADED_BUSY = "你问得太快了，让我喘口气。"


def _speakable_error(exc: BaseException) -> str | None:
    """把 API 异常翻译成一句能说出口的话。

    为什么值得做：断网、欠费的时候它一声不吭，人会以为它坏了，
    然后开始怀疑唤醒词、怀疑麦克风、怀疑代码 —— 排查成本全落在主人身上。
    说一句话，故障就变成了性格。
    """
    name = type(exc).__name__.lower()
    text = f"{name} {exc}".lower()

    if "401" in text or "403" in text or "authentication" in name:
        return _DEGRADED_AUTH
    if any(k in text for k in ("402", "insufficient", "balance", "quota", "exceeded")):
        return _DEGRADED_MONEY
    if "429" in text or "ratelimit" in name:
        return _DEGRADED_BUSY
    if any(
        k in text
        for k in (
            "timeout",
            "timed out",
            "timedout",
            "connection",
            "connect",
            "unreachable",
            "ssl",
            "proxy",
            "network",
        )
    ):
        return _DEGRADED_NETWORK
    return None


def _trim_dangling(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    """清掉读回来的历史里"说不通"的收尾。

    两种脏数据：
        1. 空内容 —— 空消息会让 API 直接报错，整个对话起不来
        2. 结尾是一条没有回复的 user 消息 —— 上一轮聊到一半程序被关了。
           留着它，下一句问话就会变成连着两条 user。
    结尾若是 assistant 消息则保留，那是一个完整回合。

    只丢结尾那一条，不做循环 —— 正常写入永远是"一问一答"成对出现，
    循环丢弃会在数据异常时把整段历史悄悄清空，那比留着一条脏数据更糟。
    """
    cleaned = [row for row in rows if (row.get("content") or "").strip()]
    if cleaned and cleaned[-1].get("role") == "user":
        dropped = cleaned.pop().get("content", "")
        logger.debug("丢掉结尾那条没等到回复的提问：{}", str(dropped)[:20])
    return cleaned


class DeepSeekClient:
    """带上下文和记忆的对话客户端。"""

    def __init__(self, settings: Settings, memory=None) -> None:
        from openai import OpenAI

        self.settings = settings
        self._cfg = settings.llm
        self._memory = memory

        if not self._cfg.has_key:
            raise RuntimeError(
                "没有找到 DEEPSEEK_API_KEY。请在项目根目录建一个 .env 文件，"
                "写入 DEEPSEEK_API_KEY=sk-你的key"
            )

        self._client = OpenAI(
            api_key=self._cfg.api_key,
            base_url=self._cfg.base_url,
            timeout=self._cfg.timeout_seconds,
        )
        self._persona = self._load_persona()
        self._history: list[dict[str, str]] = []
        self._last_seen_at: str | None = None
        self._warmup_lock = threading.Lock()
        self._warmup_running = False
        self._warmed_at = 0.0
        logger.info(
            "对话模型就绪 | 模型={} | 性格提示词 {} 字",
            self._cfg.model,
            len(self._persona),
        )
        self._restore_history()

    def _restore_history(self) -> None:
        """启动时把上次的对话读回来 —— 这就是 S4 的 4a。

        数据一直都在 SQLite 里（`memory/store.py` 从第一天就在写），
        但以前没有任何人读回来，所以程序一关它就对你一无所知。

        顺带把"上次聊天的时间"记下来（`_last_seen_at`）。
        必须在**本轮对话开始之前**取，取到的才真的是"上一次"。
        """
        if self._memory is None:
            return

        try:
            self._last_seen_at = self._memory.last_message_at()
            rows = self._memory.recent_messages(self._cfg.max_history, include_time=True)
        except Exception:
            # 读不回来就退回"仅本次会话记忆" —— 那本来就是以前的行为，不会更糟
            logger.exception("读回历史失败，这次就从头开始聊")
            return

        rows = _trim_dangling(rows)
        self._history.extend(
            {"role": row["role"], "content": row["content"]} for row in rows
        )

        if not self._history:
            logger.info("记忆库里还没有对话，这是第一次聊天")
            return

        logger.info(
            "恢复了 {} 条历史消息（最早的是 {}）| 上次聊天：{}",
            len(self._history),
            rows[0].get("created_at", "?"),
            self._last_seen_at or "?",
        )

    def _time_sense_message(self) -> dict[str, str] | None:
        """给模型一点时间感，否则它会以为"上次聊天"就是刚刚。

        加上这一段，它才说得出"上次你不是说在忙作业吗"这种话 ——
        一句话的体感价值远大于它的技术含量。

        时间必须每轮现取。启动时算一次存着的话，聊到晚上它会一直报下午的时间。
        """
        now = datetime.now()
        lines = [f"现在是 {describe_now(now)}。"]

        last = describe_last_seen(self._last_seen_at, now)
        if last:
            lines.append(
                f"你们上一次聊天是{last}。如果提到时间，以这个为准，别以为就是刚才。"
            )

        return {"role": "system", "content": "".join(lines)}

    def _load_persona(self) -> str:
        path = self.settings.paths.persona
        if not path.exists():
            logger.warning("性格文件不存在，使用默认性格：{}", path)
            return _FALLBACK_PERSONA
        text = read_text(path).strip()
        if not text:
            logger.warning("性格文件是空的，使用默认性格")
            return _FALLBACK_PERSONA
        logger.debug("已加载性格文件 {}（{} 字）", path.name, len(text))
        return text

    @property
    def history(self) -> list[dict[str, str]]:
        return list(self._history)

    def reset(self) -> None:
        self._history.clear()
        logger.info("对话上下文已清空")

    def _build_messages(self, user_text: str) -> list[dict[str, str]]:
        messages = [{"role": "system", "content": self._persona}]

        # 长期记忆：把相关的事实塞进 system 之后
        if self._memory is not None:
            try:
                memories = self._memory.recall(user_text, self.settings.memory.top_k)
            except Exception:
                logger.exception("检索记忆失败，本轮忽略记忆")
                memories = []
            if memories:
                joined = "\n".join(f"- {m}" for m in memories)
                messages.append(
                    {"role": "system", "content": f"你记得关于主人的这些事：\n{joined}"}
                )
                logger.debug("注入 {} 条记忆", len(memories))

        sense = self._time_sense_message()
        if sense is not None:
            messages.append(sense)

        messages.extend(self._history[-self._cfg.max_history :])
        messages.append({"role": "user", "content": user_text})
        return messages

    # ------------------------------------------------------------ 连接预热

    def _warmup_fresh(self) -> bool:
        """刚预热过就别再预热了。"""
        return (time.perf_counter() - self._warmed_at) < _WARMUP_REUSE_SECONDS

    def warmup(self) -> float:
        """把连接先建起来，返回耗时（秒）。

        为什么需要这个（实测数据，不是感觉）：
            同一个问题，**第一次**请求要 2.0~2.3 秒，
            之后的请求只要 0.5~0.8 秒。试过把历史对话从 0 轮堆到 40 轮，
            首句耗时几乎不变 —— 所以那多出来的 1.5 秒不是“问题太长”，
            而是全花在建连接（TCP + TLS 握手）上。

            机器人待机十分钟，连接早被回收了，
            于是**每次“第一句话”都是冷的**。这正是“反应慢”里最难受的那一段。

            启动时打一次，就把这 1.5 秒从用户身上挪到了启动阶段。
        """
        started = time.perf_counter()
        try:
            self._client.chat.completions.create(
                model=self._cfg.model,
                messages=[{"role": "user", "content": _WARMUP_PROMPT}],
                max_tokens=1,
                temperature=0.0,
                stream=False,
            )
        except Exception:
            # 不能因为预热失败就不让启动：可能只是网络抖了一下，
            # 真正请求时还会再试一次，到时该报错会正常报。
            logger.warning("对话连接预热失败，第一次回答可能会慢一点")
            return time.perf_counter() - started

        elapsed = time.perf_counter() - started
        self._warmed_at = time.perf_counter()
        logger.info(
            "对话连接已预热 | 耗时 {:.2f} 秒（相当于把首次回答的 1.5 秒提前花了）",
            elapsed,
        )
        return elapsed

    def warmup_async(self) -> bool:
        """后台预热，不阻塞。返回是否真的发起了预热。

        唤醒词命中后立刻调它：这时候还要录音 + 识别，大约 1 秒多，
        连接正好在这段时间里建好，等真正提问时就是热的。
        不同时开两个，也不重复预热刚预热过的连接。
        """
        if self._warmup_fresh():
            return False
        with self._warmup_lock:
            if self._warmup_running or self._warmup_fresh():
                return False
            self._warmup_running = True

        def run() -> None:
            try:
                self.warmup()
            finally:
                with self._warmup_lock:
                    self._warmup_running = False

        threading.Thread(target=run, name="xiaoyu-llm-warmup", daemon=True).start()
        return True

    def stream_reply(self, user_text: str) -> Iterator[str]:
        """流式对话，逐句 yield（可以直接喂给 speaker）。"""
        user_text = sanitize(user_text).strip()
        if not user_text:
            return

        logger.info("主人说：{}", user_text)
        messages = self._build_messages(user_text)
        started = time.perf_counter()
        buffer = ""
        collected: list[str] = []
        first_sentence_logged = False
        completed = False

        try:
            stream = self._client.chat.completions.create(
                model=self._cfg.model,
                messages=messages,
                stream=True,
                temperature=self._cfg.temperature,
            )
            for chunk in stream:
                if not chunk.choices:
                    continue
                piece = chunk.choices[0].delta.content or ""
                if not piece:
                    continue
                buffer += piece
                collected.append(piece)

                sentences, buffer = split_sentences(buffer)
                for sentence in sentences:
                    if not first_sentence_logged:
                        first_sentence_logged = True
                        logger.info("首句就绪，用时 {:.2f} 秒", time.perf_counter() - started)
                    yield sentence

            if buffer.strip():
                yield buffer.strip()
            completed = True

        except Exception as exc:
            logger.exception("调用大模型失败")
            line = _speakable_error(exc)
            if line is None:
                # 认不出来的异常是真 bug，交给上层记堆栈，别用一句好话盖住
                raise
            logger.warning("这一轮说不成话，改用兜底的一句：{}", line)
            yield line
        finally:
            reply = "".join(collected).strip()
            if reply and completed:
                self._history.append({"role": "user", "content": user_text})
                self._history.append({"role": "assistant", "content": reply})
                logger.info("小宇说：{}", reply)
                if self._memory is not None:
                    try:
                        self._memory.remember_exchange(user_text, reply)
                    except Exception:
                        logger.exception("写入记忆失败")
            elif reply:
                # 半截回复一旦进了历史，下次启动读回来就是一句没头没尾的话
                logger.warning("这一轮没正常说完（{} 字），不写进历史", len(reply))