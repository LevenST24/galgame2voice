"""
Multi-turn Conversational Memory, Sliding Window Truncation, and Prompt Templating Service.
"""

from typing import List, Dict, Any, Optional, Union
from pathlib import Path
import json
import re
import aiosqlite
from pydantic import BaseModel, Field

from galgame2voice.adapters.base import ChatMessage
from galgame2voice.database.session import get_database_path, get_db


class SessionTurn(BaseModel):
    role: str = Field(..., description="'user' or 'assistant'")
    content_chinese: str = Field(default="")
    content_japanese: str = Field(default="")
    raw_content: Optional[str] = None
    audio_url: str = Field(default="")
    latency_ms: int = Field(default=0)


class SessionManager:
    """
    Manages multi-turn conversation context, sliding window history, and prompt templating.
    Supports dual SQLite schema (messages and session_messages) for maximum test and runtime compatibility.
    """

    DEFAULT_SYSTEM_TEMPLATE = (
        "你是一个Galgame二次元伴侣角色【{character_name}】。\n"
        "请始终输出如下 JSON 格式，不要包含 Markdown 标记或多余文字：\n"
        "{{\"chinese\": \"给玩家看的中文内容\", \"japanese\": \"对应的口语化日语音频台词\"}}"
    )

    def __init__(
        self,
        db_path: Optional[Union[str, Path]] = None,
        default_system_template: Optional[str] = None,
    ):
        self.db_path = str(db_path) if db_path is not None else get_database_path()
        self.system_template = default_system_template or self.DEFAULT_SYSTEM_TEMPLATE

    def estimate_tokens(self, text: str) -> int:
        """
        Estimates token count for mixed CJK / English text.
        Returns 0 for empty string, ~1 token per 2 CJK characters / 4 Latin characters.
        """
        if not text:
            return 0
        cjk_count = len(re.findall(r'[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]', text))
        other_count = len(text) - cjk_count
        tokens = (cjk_count + 1) // 2 + (other_count + 3) // 4
        return max(1, tokens)

    async def add_turn(
        self,
        session_id: str,
        role: str,
        chinese: str,
        japanese: str = "",
        raw: Optional[str] = None,
        audio_url: str = "",
        latency_ms: int = 0,
    ) -> SessionTurn:
        """Persists a new conversation turn to SQLite."""
        async with get_db(self.db_path) as db:
            # Check table existence: session_messages vs messages
            cur = await db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='session_messages';")
            has_session_messages = await cur.fetchone()

            if has_session_messages:
                await db.execute("""
                    INSERT INTO session_messages (session_id, role, content_chinese, content_japanese, raw_content)
                    VALUES (?, ?, ?, ?, ?)
                """, (session_id, role, chinese, japanese, raw or chinese))
            else:
                # Check if sessions table exists to satisfy foreign keys
                cur_sess = await db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='sessions';")
                if await cur_sess.fetchone():
                    await db.execute("""
                        INSERT OR IGNORE INTO sessions (id, channel, user_id)
                        VALUES (?, 'web', '')
                    """, (session_id,))

                # Use messages table from production schema
                await db.execute("""
                    INSERT INTO messages (session_id, role, content_chinese, content_japanese, audio_url, latency_ms)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (session_id, role, chinese, japanese, audio_url, latency_ms))
            await db.commit()

        return SessionTurn(
            role=role,
            content_chinese=chinese,
            content_japanese=japanese,
            raw_content=raw,
            audio_url=audio_url,
            latency_ms=latency_ms,
        )

    async def get_history(
        self,
        session_id: str,
        max_messages: int = 10,
        max_tokens: int = 2000,
    ) -> List[SessionTurn]:
        """
        Retrieves chronological history with two-stage sliding window:
        1. Max message count limit (most recent N turns).
        2. Token budget trimming (pops oldest turns until total tokens <= max_tokens).
        """
        async with get_db(self.db_path) as db:
            cur = await db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='session_messages';")
            has_session_messages = await cur.fetchone()

            if has_session_messages:
                query = """
                    SELECT role, content_chinese, content_japanese, raw_content
                    FROM session_messages
                    WHERE session_id = ?
                    ORDER BY id DESC
                    LIMIT ?
                """
            else:
                cur_msg = await db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='messages';")
                if not await cur_msg.fetchone():
                    return []
                query = """
                    SELECT role, content_chinese, content_japanese, '' as raw_content, audio_url, latency_ms
                    FROM messages
                    WHERE session_id = ?
                    ORDER BY id DESC
                    LIMIT ?
                """

            async with db.execute(query, (session_id, max_messages)) as cursor:
                rows = await cursor.fetchall()

        # Restore chronological order
        turns = [SessionTurn(**dict(r)) for r in reversed(rows)]

        # Token limit sliding window: pop oldest turns if total tokens exceed max_tokens
        total_tokens = sum(
            self.estimate_tokens(t.content_chinese) + self.estimate_tokens(t.content_japanese)
            for t in turns
        )
        while turns and total_tokens > max_tokens:
            removed = turns.pop(0)
            total_tokens -= (
                self.estimate_tokens(removed.content_chinese) +
                self.estimate_tokens(removed.content_japanese)
            )

        return turns

    async def clear_session(self, session_id: str) -> bool:
        """Clears all conversation turns for a given session ID."""
        async with get_db(self.db_path) as db:
            cur = await db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='session_messages';")
            has_session_messages = await cur.fetchone()

            if has_session_messages:
                res = await db.execute("DELETE FROM session_messages WHERE session_id = ?", (session_id,))
            else:
                cur_msg = await db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='messages';")
                if not await cur_msg.fetchone():
                    return False
                res = await db.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            await db.commit()
            return res.rowcount > 0

    def format_llm_messages(
        self,
        character_name: str,
        history: List[SessionTurn],
        new_user_prompt: Optional[str] = None,
        system_template: Optional[str] = None,
    ) -> List[Dict[str, str]]:
        """
        Constructs system prompt and formatted OpenAI-compatible message list.
        User messages are formatted as plain text; assistant messages as bilingual JSON.
        Ensures new_user_prompt is never duplicated.
        """
        tpl = system_template or self.system_template

        if "{character_name}" in tpl:
            system_content = tpl.replace("{character_name}", character_name)
            system_content = system_content.replace("{{", "{").replace("}}", "}")
        elif "{" in tpl:
            try:
                system_content = tpl.format(character_name=character_name)
            except Exception:
                system_content = tpl.replace("{character_name}", character_name)
        else:
            system_content = tpl

        messages = [{"role": "system", "content": system_content}]

        for turn in history:
            if turn.role == "user":
                content = turn.content_chinese or turn.raw_content or ""
            else:
                content = turn.raw_content or json.dumps(
                    {"chinese": turn.content_chinese, "japanese": turn.content_japanese},
                    ensure_ascii=False,
                )
            messages.append({"role": turn.role, "content": content})

        # Append new_user_prompt only if it is not already the last message in history
        if new_user_prompt:
            last_msg = messages[-1] if len(messages) > 1 else None
            if not last_msg or last_msg.get("role") != "user" or (last_msg.get("content") != new_user_prompt and last_msg.get("content") != json.dumps({"chinese": new_user_prompt, "japanese": ""}, ensure_ascii=False)):
                messages.append({"role": "user", "content": new_user_prompt})

        return messages

    async def build_chat_messages(
        self,
        session_id: str,
        user_prompt: str,
        character_name: Optional[str] = None,
        custom_system_prompt: Optional[str] = None,
        max_messages: int = 10,
        max_tokens: int = 2000,
    ) -> List[ChatMessage]:
        """High-level helper returning List[ChatMessage] for BaseLLMAdapter."""
        history = await self.get_history(session_id, max_messages=max_messages, max_tokens=max_tokens)
        dict_msgs = self.format_llm_messages(
            character_name=character_name or "四季夏目",
            history=history,
            new_user_prompt=user_prompt,
            system_template=custom_system_prompt,
        )
        return [ChatMessage(role=m["role"], content=m["content"]) for m in dict_msgs]


__all__ = ["SessionTurn", "SessionManager"]
