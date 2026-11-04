"""
Session and message CRUD operations for SQLite persistence in galgame2voice.
"""

import json
import logging
import re
import sqlite3
from typing import Any

import aiosqlite

from galgame2voice.database.crud_modules.voice_profiles import get_active_voice_profile
from galgame2voice.database.migrations import init_schema_and_seeds
from galgame2voice.database.models import (
    MessageCreate,
    MessageResponse,
    SessionResponse,
)
from galgame2voice.database.session import immediate_transaction

logger = logging.getLogger("galgame2voice.database.crud_modules.sessions")

__all__ = [
    "DEFAULT_TOKEN_BUDGET",
    "get_or_create_session",
    "get_session",
    "upsert_session",
    "list_sessions",
    "list_sessions_overview",
    "delete_session",
    "clear_session_messages",
    "add_message",
    "delete_message",
    "lookup_turn_reply",
    "get_recent_messages",
    "count_session_messages",
]

DEFAULT_TOKEN_BUDGET: int = 4096


async def get_or_create_session(
    conn: aiosqlite.Connection,
    session_id: str,
    channel: str = "web",
    user_id: str = "",
) -> SessionResponse:
    """Fetches an existing session or creates a new one with default voice profile."""
    conn.row_factory = aiosqlite.Row
    try:
        cursor = await conn.execute("SELECT * FROM sessions WHERE id = ?;", (session_id,))
        row = await cursor.fetchone()
    except (sqlite3.OperationalError, aiosqlite.OperationalError) as err:
        if "no such table" in str(err).lower():
            await init_schema_and_seeds(conn)
            cursor = await conn.execute("SELECT * FROM sessions WHERE id = ?;", (session_id,))
            row = await cursor.fetchone()
        else:
            raise

    if row:
        return SessionResponse(**dict(row))

    # Fetch default active voice profile
    active_profile = await get_active_voice_profile(conn)
    profile_id = active_profile.id if active_profile else None

    try:
        async with immediate_transaction(conn):
            await conn.execute("""
                INSERT INTO sessions (id, channel, user_id, voice_profile_id, token_budget)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(id) DO NOTHING;
            """, (session_id, channel, user_id, profile_id, DEFAULT_TOKEN_BUDGET))
    except (sqlite3.IntegrityError, aiosqlite.IntegrityError):
        # Fallback if profile_id had a foreign key issue
        try:
            async with immediate_transaction(conn):
                await conn.execute("""
                    INSERT INTO sessions (id, channel, user_id, voice_profile_id, token_budget)
                    VALUES (?, ?, ?, NULL, ?)
                    ON CONFLICT(id) DO NOTHING;
                """, (session_id, channel, user_id, DEFAULT_TOKEN_BUDGET))
        except (sqlite3.IntegrityError, aiosqlite.IntegrityError):
            pass

    cursor = await conn.execute("SELECT * FROM sessions WHERE id = ?;", (session_id,))
    row = await cursor.fetchone()
    if row:
        return SessionResponse(**dict(row))

    return SessionResponse(
        id=session_id,
        channel=channel,
        user_id=user_id,
        voice_profile_id=profile_id,
        token_budget=DEFAULT_TOKEN_BUDGET,
    )


async def get_session(conn: aiosqlite.Connection, session_id: str) -> SessionResponse | None:
    """Fetches a session by session ID."""
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute("SELECT * FROM sessions WHERE id = ?;", (session_id,))
    row = await cursor.fetchone()
    if not row:
        return None
    return SessionResponse(**dict(row))


async def upsert_session(
    conn: aiosqlite.Connection,
    session_id: str,
    title: str | None = None,
    channel: str = "web",
    user_id: str = "",
    voice_profile_id: int | None = None,
    custom_system_prompt: str | None = None,
    settings_json: str | None = None,
    update_voice_binding: bool = False,
) -> SessionResponse:
    """Persist metadata; update_voice_binding distinguishes explicit null from omission."""
    conn.row_factory = aiosqlite.Row
    clean_title = (title or "").strip()
    cursor = await conn.execute("SELECT * FROM sessions WHERE id = ?;", (session_id,))
    row = await cursor.fetchone()
    if row:
        updates = ["updated_at = CURRENT_TIMESTAMP"]
        params: list[Any] = []
        if title is not None:
            updates.append("title = ?")
            params.append(clean_title)
        if voice_profile_id is not None or update_voice_binding:
            updates.append("voice_profile_id = ?")
            params.append(voice_profile_id)
        if custom_system_prompt is not None:
            updates.append("custom_system_prompt = ?")
            params.append(custom_system_prompt)
        if settings_json is not None:
            updates.append("settings_json = ?")
            params.append(settings_json)
        params.append(session_id)
        async with immediate_transaction(conn):
            await conn.execute(f"UPDATE sessions SET {', '.join(updates)} WHERE id = ?;", params)
    else:
        if voice_profile_id is None and not update_voice_binding:
            active_p = await get_active_voice_profile(conn)
            voice_profile_id = active_p.id if active_p else None
        async with immediate_transaction(conn):
            await conn.execute("""
                INSERT INTO sessions (id, channel, user_id, voice_profile_id, custom_system_prompt, title, settings_json, token_budget)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    title = COALESCE(excluded.title, sessions.title),
                    voice_profile_id = CASE WHEN ? THEN excluded.voice_profile_id
                        ELSE COALESCE(excluded.voice_profile_id, sessions.voice_profile_id) END,
                    custom_system_prompt = COALESCE(excluded.custom_system_prompt, sessions.custom_system_prompt),
                    settings_json = COALESCE(excluded.settings_json, sessions.settings_json),
                    updated_at = CURRENT_TIMESTAMP;
            """, (session_id, channel, user_id, voice_profile_id, custom_system_prompt, clean_title,
                  settings_json, DEFAULT_TOKEN_BUDGET, update_voice_binding))

    cursor = await conn.execute("SELECT * FROM sessions WHERE id = ?;", (session_id,))
    new_row = await cursor.fetchone()
    return SessionResponse(**dict(new_row))


async def list_sessions(conn: aiosqlite.Connection, limit: int = 50) -> list[SessionResponse]:
    """Lists sessions ordered by last updated timestamp."""
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute("SELECT * FROM sessions ORDER BY updated_at DESC LIMIT ?;", (limit,))
    rows = await cursor.fetchall()
    return [SessionResponse(**dict(r)) for r in rows]


async def list_sessions_overview(conn: aiosqlite.Connection, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
    """
    Returns sessions ordered by updated_at DESC, augmented with message count,
    last message preview, and dynamically inferred title if not explicitly set.
    """
    conn.row_factory = aiosqlite.Row
    try:
        cursor = await conn.execute("""
            SELECT
                s.*,
                (SELECT COUNT(*) FROM messages WHERE session_id = s.id) as message_count,
                (SELECT content_chinese FROM messages WHERE session_id = s.id ORDER BY id DESC LIMIT 1) as last_message,
                (SELECT content_chinese FROM messages WHERE session_id = s.id AND role = 'user' ORDER BY id ASC LIMIT 1) as first_user_message
            FROM sessions s
            ORDER BY s.updated_at DESC, s.id DESC
            LIMIT ? OFFSET ?;
        """, (limit, offset))
        rows = await cursor.fetchall()
    except (sqlite3.OperationalError, aiosqlite.OperationalError):
        await init_schema_and_seeds(conn)
        cursor = await conn.execute(
            "SELECT * FROM sessions ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?;", (limit, offset)
        )
        rows = await cursor.fetchall()

    result = []
    for r in rows:
        d = dict(r)
        explicit_title = str(d.get("title") or "").strip()
        if not explicit_title:
            first_user = str(d.get("first_user_message") or "").strip()
            if first_user:
                clean_preview = re.sub(r"[\r\n\t]+", " ", first_user).strip()
                explicit_title = clean_preview[:24]
            else:
                explicit_title = "新对话"
        d["title"] = explicit_title
        result.append(d)
    return result


async def delete_session(conn: aiosqlite.Connection, session_id: str) -> bool:
    """Delete records atomically; the retention worker owns shared audio files."""

    async with immediate_transaction(conn):
        await conn.execute("DELETE FROM messages WHERE session_id = ?;", (session_id,))
        cursor = await conn.execute("DELETE FROM sessions WHERE id = ?;", (session_id,))
    return cursor.rowcount > 0


async def clear_session_messages(conn: aiosqlite.Connection, session_id: str) -> bool:
    """Clear records without unlinking shared cache or reference audio."""

    cleared = False
    async with immediate_transaction(conn):
        cur = await conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='session_messages';")
        if await cur.fetchone():
            cursor = await conn.execute("DELETE FROM session_messages WHERE session_id = ?;", (session_id,))
            if cursor.rowcount > 0:
                cleared = True

        cur_msg = await conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='messages';")
        if await cur_msg.fetchone():
            cursor = await conn.execute("DELETE FROM messages WHERE session_id = ?;", (session_id,))
            if cursor.rowcount > 0:
                cleared = True
            cur_sess = await conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='sessions';")
            if await cur_sess.fetchone():
                await conn.execute("UPDATE sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?;", (session_id,))

    return cleared


async def add_message(conn: aiosqlite.Connection, msg: MessageCreate) -> MessageResponse:
    """Appends a new conversation message to a session."""
    conn.row_factory = aiosqlite.Row
    # Ensure session exists
    await get_or_create_session(conn, msg.session_id)
    async with immediate_transaction(conn):
        cursor = await conn.execute("""
            INSERT INTO messages (session_id, role, content_chinese, content_japanese, audio_url, latency_ms, lines_json, reply_to_message_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            RETURNING *;
        """, (
            msg.session_id, msg.role, msg.content_chinese,
            msg.content_japanese, msg.audio_url, msg.latency_ms,
            json.dumps(msg.lines, ensure_ascii=False) if msg.lines is not None else None,
            getattr(msg, "reply_to_message_id", None),
        ))
        row = await cursor.fetchone()
        if not row and cursor.lastrowid:
            cur2 = await conn.execute("SELECT * FROM messages WHERE id = ?;", (cursor.lastrowid,))
            row = await cur2.fetchone()
        await conn.execute("UPDATE sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?;", (msg.session_id,))
    if not row:
        raise RuntimeError(f"Failed to persist or retrieve message for session '{msg.session_id}'")
    return MessageResponse(**dict(row))


async def get_recent_messages(
    conn: aiosqlite.Connection,
    session_id: str,
    limit: int = 10,
    before_id: int | None = None,
) -> list[MessageResponse]:
    """Fetches the most recent messages for a session in chronological order."""
    conn.row_factory = aiosqlite.Row
    where_clause = "WHERE session_id = ? AND id < ?" if before_id is not None else "WHERE session_id = ?"
    params = (session_id, before_id, limit) if before_id is not None else (session_id, limit)
    try:
        cursor = await conn.execute(f"""
            SELECT * FROM (
                SELECT * FROM messages INDEXED BY idx_messages_session_id_desc
                {where_clause} ORDER BY id DESC LIMIT ?
            ) ORDER BY id ASC;
        """, params)
        rows = await cursor.fetchall()
    except (sqlite3.OperationalError, aiosqlite.OperationalError):
        cursor = await conn.execute(f"""
            SELECT * FROM (
                SELECT * FROM messages {where_clause} ORDER BY id DESC LIMIT ?
            ) ORDER BY id ASC;
        """, params)
        rows = await cursor.fetchall()
    return [MessageResponse(**dict(r)) for r in rows]


async def count_session_messages(conn: aiosqlite.Connection, session_id: str) -> int:
    """Counts total messages recorded for a session."""
    cursor = await conn.execute("SELECT COUNT(*) FROM messages WHERE session_id = ?;", (session_id,))
    row = await cursor.fetchone()
    return row[0] if row else 0


async def delete_message(conn: aiosqlite.Connection, message_id: int) -> None:
    """Deletes a message by its ID."""
    async with immediate_transaction(conn):
        await conn.execute("DELETE FROM messages WHERE id = ?;", (message_id,))


async def lookup_turn_reply(
    conn: aiosqlite.Connection,
    session_id: str,
    user_message_id: int,
) -> dict[str, Any]:
    """
    Consistent read-only snapshot query for assistant reply linked to a specific user message.
    Uses a single SQL statement (LEFT JOIN) to guarantee an atomic snapshot across user and
    assistant rows, avoiding intermediate state anomalies (such as returning 'pending' during
    an atomic clear/delete of user and reply rows).

    Returns a dict with session_id, user_message_id, and status:
      - ready: user exists, exactly 1 matching assistant reply found -> includes message_id
      - pending: user exists, but no matching assistant reply committed yet
      - gone: user does not exist in this session (e.g. deleted, cleared, or wrong session)
      - ambiguous: multiple matching assistant replies exist (never arbitrarily pick one)
      - unavailable: database busy/locked
    """
    conn.row_factory = aiosqlite.Row
    query = """
        SELECT
            u.id AS user_id,
            a.id AS assistant_id
        FROM messages u
        LEFT JOIN messages a ON (
            a.session_id = u.session_id
            AND a.role = 'assistant'
            AND a.reply_to_message_id = u.id
        )
        WHERE u.session_id = ? AND u.id = ? AND u.role = 'user'
        ORDER BY a.id ASC;
    """
    try:
        cur = await conn.execute(query, (session_id, user_message_id))
        rows = await cur.fetchall()
    except (sqlite3.OperationalError, aiosqlite.OperationalError) as exc:
        err_msg = str(exc).lower()
        if "busy" in err_msg or "locked" in err_msg:
            return {"session_id": session_id, "user_message_id": user_message_id, "status": "unavailable"}
        if "no such table" in err_msg:
            return {"session_id": session_id, "user_message_id": user_message_id, "status": "gone"}
        if "no such column" in err_msg:
            # Fallback for old schema without reply_to_message_id
            try:
                cur_user = await conn.execute(
                    "SELECT id FROM messages WHERE session_id = ? AND id = ? AND role = 'user';",
                    (session_id, user_message_id),
                )
                user_row = await cur_user.fetchone()
                return {
                    "session_id": session_id,
                    "user_message_id": user_message_id,
                    "status": "pending" if user_row else "gone",
                }
            except (sqlite3.OperationalError, aiosqlite.OperationalError) as fallback_exc:
                if "busy" in str(fallback_exc).lower() or "locked" in str(fallback_exc).lower():
                    return {"session_id": session_id, "user_message_id": user_message_id, "status": "unavailable"}
                return {"session_id": session_id, "user_message_id": user_message_id, "status": "gone"}
        raise

    if not rows:
        return {"session_id": session_id, "user_message_id": user_message_id, "status": "gone"}

    assistant_ids = [r["assistant_id"] for r in rows if r["assistant_id"] is not None]
    if not assistant_ids:
        return {"session_id": session_id, "user_message_id": user_message_id, "status": "pending"}
    elif len(assistant_ids) == 1:
        return {
            "session_id": session_id,
            "user_message_id": user_message_id,
            "status": "ready",
            "message_id": int(assistant_ids[0]),
        }
    else:
        return {"session_id": session_id, "user_message_id": user_message_id, "status": "ambiguous"}
