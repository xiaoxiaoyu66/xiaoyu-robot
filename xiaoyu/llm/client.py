"""大模型对话：DeepSeek 流式接口。

两个关键设计：
    1. 流式 + 按句切分（切分逻辑在 xiaoyu/text.py）。
       回答一出句号就送去合成，不等整段生成完，
       这样首句才能在 2.5 秒内出声。
    2. 性格写在 config/persona.md 里，改人格不用动代码。
"""

from __future__ import annotations

import time
from collections.abc import Iterator

from ..config import Settings
from ..logger import get_logger
from ..text import read_text, sanitize, split_sentences

logger = get_logger(__name__)

_FALLBACK_PERSONA = "你是小宇，一台放在书桌上的陪伴机器人。说话简短、自然，像朋友聊天。"


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
        logger.info(
            "对话模型就绪 | 模型={} | 性格提示词 {} 字",
            self._cfg.model,
            len(self._persona),
        )

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

        messages.extend(self._history[-self._cfg.max_history :])
        messages.append({"role": "user", "content": user_text})
        return messages

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

        except Exception:
            logger.exception("调用大模型失败")
            raise
        finally:
            reply = "".join(collected).strip()
            if reply:
                self._history.append({"role": "user", "content": user_text})
                self._history.append({"role": "assistant", "content": reply})
                logger.info("小宇说：{}", reply)
                if self._memory is not None:
                    try:
                        self._memory.remember_exchange(user_text, reply)
                    except Exception:
                        logger.exception("写入记忆失败")