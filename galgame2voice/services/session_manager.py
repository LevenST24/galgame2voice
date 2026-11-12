"""
Multi-turn Conversational Memory, Sliding Window Truncation, and Prompt Templating Service.
"""

from pathlib import Path
import json
import aiosqlite
from pydantic import BaseModel, Field

from galgame2voice.adapters.base import ChatMessage
from galgame2voice.database.session import get_database_path, get_db, immediate_transaction
from galgame2voice.database.models import decode_lines
from galgame2voice.services.metrics_collector import MetricsCollector
from galgame2voice.services.chat_pipelines.line_builder import build_lines
from galgame2voice.services.chat_pipelines.reply_contract import format_lines_turn


class SessionTurn(BaseModel):
    """Data model representing a single conversational turn in a session."""

    role: str = Field(..., description="'user' or 'assistant'")
    content_chinese: str = Field(default="")
    content_japanese: str = Field(default="")
    emotion: str = Field(default="gentle", description="Emotion archetype: gentle, shy, happy, tsundere, cool, sad, angry")
    raw_content: str | None = None
    audio_url: str = Field(default="")
    latency_ms: int = Field(default=0)
    lines: list[dict] | None = None


def _render_system_template(tpl: str, character_name: str) -> str:
    """Renders a system template string with character_name substitution."""
    if "{character_name}" in tpl:
        rendered = tpl.replace("{character_name}", character_name)
        return rendered.replace("{{", "{").replace("}}", "}")
    if "{" in tpl:
        try:
            return tpl.format(character_name=character_name)
        except Exception:
            return tpl.replace("{character_name}", character_name)
    return tpl


def _is_duplicate_user_prompt(last_msg: dict[str, str] | None, prompt: str) -> bool:
    """Checks whether the prompt matches the immediately preceding user message."""
    if not last_msg or last_msg.get("role") != "user":
        return False
    content = last_msg.get("content")
    return content == prompt or content == json.dumps({"chinese": prompt, "japanese": ""}, ensure_ascii=False)


class SessionManager:
    """
    Manages multi-turn conversation context, sliding window history, and prompt templating.
    Supports dual SQLite schema (messages and session_messages) for maximum test and runtime compatibility.
    """

    DEFAULT_SYSTEM_TEMPLATE = "你是一个Galgame二次元伴侣角色【{character_name}】。"

    def __init__(
        self,
        db_path: str | Path | None = None,
        default_system_template: str | None = None,
    ) -> None:
        self.db_path = str(db_path) if db_path is not None else get_database_path()
        self.system_template = default_system_template or self.DEFAULT_SYSTEM_TEMPLATE
        self._table_name: str | None = None
        self._session_messages_has_lines: bool | None = None
        self._messages_has_lines: bool | None = None

    def estimate_tokens(self, text: str) -> int:
        """
        Estimates token count for mixed CJK / English text.
        Delegates to MetricsCollector.estimate_tokens so token budgeting stays
        consistent with the telemetry pipeline (CJK * 1.1 + non-CJK / 3.5).
        """
        return MetricsCollector.estimate_tokens(text)

    async def add_turn(
        self,
        session_id: str,
        role: str,
        chinese: str,
        japanese: str = "",
        raw: str | None = None,
        audio_url: str = "",
        latency_ms: int = 0,
        emotion: str = "gentle",
        lines: list[dict] | None = None,
    ) -> SessionTurn:
        """Persists a new conversation turn to SQLite."""
        if role == "assistant" and lines is None:
            lines = build_lines(chinese, japanese, emotion, with_audio=True)
        async with get_db(self.db_path) as db:
            async with immediate_transaction(db):
                # Check table existence: session_messages vs messages
                cur = await db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='session_messages';")
                has_session_messages = await cur.fetchone()

                if has_session_messages:
                    if self._session_messages_has_lines is None:
                        cols = await (await db.execute("PRAGMA table_info(session_messages);")).fetchall()
                        self._session_messages_has_lines = any(c[1] == "lines_json" for c in cols)

                    if self._session_messages_has_lines:
                        await db.execute("""
                            INSERT INTO session_messages (session_id, role, content_chinese, content_japanese, raw_content, lines_json)
                            VALUES (?, ?, ?, ?, ?, ?)
                        """, (session_id, role, chinese, japanese, raw or chinese,
                              json.dumps(lines, ensure_ascii=False) if lines is not None else None))
                    else:
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

                    if self._messages_has_lines is None:
                        cols = await (await db.execute("PRAGMA table_info(messages);")).fetchall()
                        self._messages_has_lines = any(c[1] == "lines_json" for c in cols)

                    if self._messages_has_lines:
                        await db.execute("""
                            INSERT INTO messages (session_id, role, content_chinese, content_japanese, audio_url, latency_ms, lines_json)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                        """, (session_id, role, chinese, japanese, audio_url, latency_ms,
                              json.dumps(lines, ensure_ascii=False) if lines is not None else None))
                    else:
                        await db.execute("""
                            INSERT INTO messages (session_id, role, content_chinese, content_japanese, audio_url, latency_ms)
                            VALUES (?, ?, ?, ?, ?, ?)
                        """, (session_id, role, chinese, japanese, audio_url, latency_ms))

        return SessionTurn(
            role=role,
            content_chinese=chinese,
            content_japanese=japanese,
            emotion=emotion,
            raw_content=raw,
            audio_url=audio_url,
            latency_ms=latency_ms,
            lines=lines,
        )

    async def get_history(
        self,
        session_id: str,
        max_messages: int = 10,
        max_tokens: int = 8000,
        conn: aiosqlite.Connection | None = None,
    ) -> list[SessionTurn]:
        """
        Retrieves chronological history with two-stage sliding window:
        1. Max message count limit (most recent N turns).
        2. Token budget trimming (pops oldest turns until total tokens <= max_tokens).
        """
        if conn is not None:
            return await self._get_history_on_conn(conn, session_id, max_messages, max_tokens)
        async with get_db(self.db_path) as db:
            return await self._get_history_on_conn(db, session_id, max_messages, max_tokens)

    def _history_query_for_table(self, table: str) -> str:
        if table == "session_messages":
            if self._session_messages_has_lines:
                return """
                    SELECT role, content_chinese, content_japanese, raw_content, lines_json
                    FROM session_messages
                    WHERE session_id = ?
                    ORDER BY id DESC
                    LIMIT ?
                """
            return """
                SELECT role, content_chinese, content_japanese, raw_content, NULL as lines_json
                FROM session_messages
                WHERE session_id = ?
                ORDER BY id DESC
                LIMIT ?
            """
        if self._messages_has_lines:
            return """
                SELECT role, content_chinese, content_japanese, '' as raw_content, audio_url, latency_ms, lines_json
                FROM messages
                WHERE session_id = ?
                ORDER BY id DESC
                LIMIT ?
            """
        return """
            SELECT role, content_chinese, content_japanese, '' as raw_content, audio_url, latency_ms, NULL as lines_json
            FROM messages
            WHERE session_id = ?
            ORDER BY id DESC
            LIMIT ?
        """

    async def _detect_table_name(self, db: aiosqlite.Connection) -> str | None:
        """Detects and caches the messages table name (session_messages vs messages)."""
        if self._table_name is None:
            cur = await db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='session_messages';")
            if await cur.fetchone():
                self._table_name = "session_messages"
            else:
                cur_msg = await db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='messages';")
                if not await cur_msg.fetchone():
                    return None
                self._table_name = "messages"
        if self._table_name == "session_messages" and self._session_messages_has_lines is None:
            cols = await (await db.execute("PRAGMA table_info(session_messages);")).fetchall()
            self._session_messages_has_lines = any(c[1] == "lines_json" for c in cols)
        elif self._table_name == "messages" and self._messages_has_lines is None:
            cols = await (await db.execute("PRAGMA table_info(messages);")).fetchall()
            self._messages_has_lines = any(c[1] == "lines_json" for c in cols)
        return self._table_name

    async def _get_history_on_conn(
        self,
        db: aiosqlite.Connection,
        session_id: str,
        max_messages: int,
        max_tokens: int,
    ) -> list[SessionTurn]:
        db.row_factory = aiosqlite.Row
        try:
            if not await self._detect_table_name(db):
                return []
            query = self._history_query_for_table(self._table_name)
            async with db.execute(query, (session_id, max_messages)) as cursor:
                rows = await cursor.fetchall()
        except aiosqlite.OperationalError:
            # If the database was dynamically re-initialized, invalidate cache and retry once
            self._table_name = None
            self._session_messages_has_lines = None
            self._messages_has_lines = None
            if not await self._detect_table_name(db):
                return []
            q = self._history_query_for_table(self._table_name)
            async with db.execute(q, (session_id, max_messages)) as cursor:
                rows = await cursor.fetchall()

        # Restore chronological order
        turns = [SessionTurn(
            **dict(r),
            lines=decode_lines(r["lines_json"]) if "lines_json" in r.keys() and r["lines_json"] is not None else None,
        ) for r in reversed(rows)]

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
            async with immediate_transaction(db):
                cur = await db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='session_messages';")
                has_session_messages = await cur.fetchone()

                if has_session_messages:
                    res = await db.execute("DELETE FROM session_messages WHERE session_id = ?", (session_id,))
                else:
                    cur_msg = await db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='messages';")
                    if not await cur_msg.fetchone():
                        return False
                    res = await db.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
                return res.rowcount > 0

    def format_llm_messages(
        self,
        character_name: str,
        history: list[SessionTurn],
        new_user_prompt: str | None = None,
        system_template: str | None = None,
        memory_prompt_block: str | None = None,
        reply_format: str = "lines",
    ) -> list[dict[str, str]]:
        """
        Constructs system prompt and formatted OpenAI-compatible message list.
        User messages are formatted as plain text; assistant messages replay per-line JSON.
        Appends new_user_prompt unless it duplicates the immediately preceding message.
        Injects optional long-term memory & affection context block into system prompt.
        """
        tpl = system_template or self.system_template
        system_content = _render_system_template(tpl, character_name)

        if memory_prompt_block and memory_prompt_block.strip():
            system_content = f"{system_content}\n\n{memory_prompt_block.strip()}"

        messages = [{"role": "system", "content": system_content}]

        for turn in history:
            # Only user/assistant turns are dialogue. A "system" row can only
            # come from an imported backup file; replaying it would promote
            # untrusted file text to a system-level instruction.
            if turn.role not in ("user", "assistant"):
                continue
            if turn.role == "assistant":
                content = format_lines_turn(
                    turn.lines,
                    chinese=turn.content_chinese,
                    japanese=turn.content_japanese,
                    emotion=turn.emotion,
                    tts=None,
                )
            else:
                content = turn.content_chinese or turn.raw_content or ""
            messages.append({"role": turn.role, "content": content})

        # Append new_user_prompt only if it is not already the last message in history
        if new_user_prompt:
            last_msg = messages[-1] if len(messages) > 1 else None
            if not _is_duplicate_user_prompt(last_msg, new_user_prompt):
                messages.append({"role": "user", "content": new_user_prompt})

        return messages

    async def build_chat_messages(
        self,
        session_id: str,
        user_prompt: str,
        character_name: str | None = None,
        custom_system_prompt: str | None = None,
        max_messages: int = 10,
        max_tokens: int = 8000,
        memory_prompt_block: str | None = None,
        conn: aiosqlite.Connection | None = None,
        reply_format: str = "lines",
    ) -> list[ChatMessage]:
        """High-level helper returning List[ChatMessage] for BaseLLMAdapter."""
        history = await self.get_history(
            session_id, max_messages=max_messages, max_tokens=max_tokens, conn=conn
        )
        dict_msgs = self.format_llm_messages(
            character_name=character_name or "Character",
            history=history,
            new_user_prompt=user_prompt,
            system_template=custom_system_prompt,
            memory_prompt_block=memory_prompt_block,
            reply_format="lines",
        )
        return [ChatMessage(role=m["role"], content=m["content"]) for m in dict_msgs]

    async def aclose(self) -> None:
        """Gracefully release any cached state or resources held by SessionManager."""
        self._table_name = None


__all__ = ["SessionTurn", "SessionManager"]
