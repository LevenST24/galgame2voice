"""
Session and message CRUD operations for SQLite persistence in galgame2voice.
"""

import logging
from pathlib import Path
import re
import sqlite3
from typing import Any, Dict, List, Optional

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
    "get_or_create_session",
    "get_session",
    "upsert_session",
    "list_sessions",
    "list_sessions_overview",
    "delete_session",
    "clear_session_messages",
    "add_message",
    "get_recent_messages",
    "count_session_messages",
]


async def get_or_create_session(
    conn: aiosqlite.Connection,
    session_id: str,
    channel: str = "web",
    user_id: str = "",
) -> SessionResponse:
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
                VALUES (?, ?, ?, ?, 4096)
                ON CONFLICT(id) DO NOTHING;
            """, (session_id, channel, user_id, profile_id))
    except (sqlite3.IntegrityError, aiosqlite.IntegrityError):
        # Fallback if profile_id had a foreign key issue
        try:
            async with immediate_transaction(conn):
                await conn.execute("""
                    INSERT INTO sessions (id, channel, user_id, voice_profile_id, token_budget)
                    VALUES (?, ?, ?, NULL, 4096)
                    ON CONFLICT(id) DO NOTHING;
                """, (session_id, channel, user_id))
        except Exception:
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
        token_budget=4096,
    )


async def get_session(conn: aiosqlite.Connection, session_id: str) -> Optional[SessionResponse]:
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute("SELECT * FROM sessions WHERE id = ?;", (session_id,))
    row = await cursor.fetchone()
    if not row:
        return None
    return SessionResponse(**dict(row))


async def upsert_session(
    conn: aiosqlite.Connection,
    session_id: str,
    title: Optional[str] = None,
    channel: str = "web",
    user_id: str = "",
    voice_profile_id: Optional[int] = None,
    custom_system_prompt: Optional[str] = None,
    settings_json: Optional[str] = None,
) -> SessionResponse:
    conn.row_factory = aiosqlite.Row
    clean_title = (title or "").strip()
    cursor = await conn.execute("SELECT * FROM sessions WHERE id = ?;", (session_id,))
    row = await cursor.fetchone()
    if row:
        updates = ["updated_at = CURRENT_TIMESTAMP"]
        params: List[Any] = []
        if title is not None:
            updates.append("title = ?")
            params.append(clean_title)
        if voice_profile_id is not None:
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
        if voice_profile_id is None:
            active_p = await get_active_voice_profile(conn)
            voice_profile_id = active_p.id if active_p else None
        async with immediate_transaction(conn):
            await conn.execute("""
                INSERT INTO sessions (id, channel, user_id, voice_profile_id, custom_system_prompt, title, settings_json, token_budget)
                VALUES (?, ?, ?, ?, ?, ?, ?, 4096)
                ON CONFLICT(id) DO UPDATE SET
                    title = COALESCE(excluded.title, sessions.title),
                    voice_profile_id = COALESCE(excluded.voice_profile_id, sessions.voice_profile_id),
                    custom_system_prompt = COALESCE(excluded.custom_system_prompt, sessions.custom_system_prompt),
                    settings_json = COALESCE(excluded.settings_json, sessions.settings_json),
                    updated_at = CURRENT_TIMESTAMP;
            """, (session_id, channel, user_id, voice_profile_id, custom_system_prompt, clean_title, settings_json))

    cursor = await conn.execute("SELECT * FROM sessions WHERE id = ?;", (session_id,))
    new_row = await cursor.fetchone()
    return SessionResponse(**dict(new_row))


async def list_sessions(conn: aiosqlite.Connection, limit: int = 50) -> List[SessionResponse]:
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute("SELECT * FROM sessions ORDER BY updated_at DESC LIMIT ?;", (limit,))
    rows = await cursor.fetchall()
    return [SessionResponse(**dict(r)) for r in rows]


async def list_sessions_overview(conn: aiosqlite.Connection, limit: int = 50) -> List[Dict[str, Any]]:
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
            ORDER BY s.updated_at DESC
            LIMIT ?;
        """, (limit,))
        rows = await cursor.fetchall()
    except (sqlite3.OperationalError, aiosqlite.OperationalError):
        await init_schema_and_seeds(conn)
        cursor = await conn.execute("SELECT * FROM sessions ORDER BY updated_at DESC LIMIT ?;", (limit,))
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


def _cascade_delete_message_audios(audio_urls: List[str]) -> None:
    try:
        from galgame2voice.config import get_settings
        audio_dir = get_settings().audio_dir
    except Exception:
        audio_dir = Path("audio")

    for raw_url in audio_urls:
        if not raw_url:
            continue
        try:
            cleaned_url = raw_url.split("?")[0].strip()
            if cleaned_url.startswith("/api/audio/"):
                rel_path = cleaned_url[len("/api/audio/"):]
            elif cleaned_url.startswith("/audio/"):
                rel_path = cleaned_url[len("/audio/"):]
            else:
                rel_path = Path(cleaned_url).name

            target_file = (audio_dir / rel_path).resolve()
            if str(target_file).startswith(str(audio_dir.resolve())):
                if target_file.is_file() and target_file.suffix.lower() != ".ogg":
                    target_file.unlink(missing_ok=True)
        except Exception as e:
            logger.debug("Failed cascading audio deletion for %s: %s", raw_url, e)


async def _cleanup_session_audios(conn: aiosqlite.Connection, session_id: str, context: str) -> None:
    """Helper to query and cascade-delete ephemeral audios belonging to a session."""
    try:
        cur_urls = await conn.execute(
            "SELECT audio_url FROM messages WHERE session_id = ? AND audio_url IS NOT NULL AND audio_url != '';",
            (session_id,),
        )
        audio_urls = [r[0] for r in await cur_urls.fetchall() if r and r[0]]
        _cascade_delete_message_audios(audio_urls)
    except Exception as exc:
        logger.debug("Cascade audio cleanup on %s skipped: %s", context, exc)


async def delete_session(conn: aiosqlite.Connection, session_id: str) -> bool:
    await _cleanup_session_audios(conn, session_id, "delete_session")

    async with immediate_transaction(conn):
        await conn.execute("DELETE FROM messages WHERE session_id = ?;", (session_id,))
        cursor = await conn.execute("DELETE FROM sessions WHERE id = ?;", (session_id,))
    return cursor.rowcount > 0


async def clear_session_messages(conn: aiosqlite.Connection, session_id: str) -> bool:
    await _cleanup_session_audios(conn, session_id, "clear_session_messages")

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
    conn.row_factory = aiosqlite.Row
    # Ensure session exists
    await get_or_create_session(conn, msg.session_id)
    async with immediate_transaction(conn):
        cursor = await conn.execute("""
            INSERT INTO messages (session_id, role, content_chinese, content_japanese, audio_url, latency_ms)
            VALUES (?, ?, ?, ?, ?, ?)
            RETURNING *;
        """, (
            msg.session_id, msg.role, msg.content_chinese,
            msg.content_japanese, msg.audio_url, msg.latency_ms
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
) -> List[MessageResponse]:
    conn.row_factory = aiosqlite.Row
    try:
        cursor = await conn.execute("""
            SELECT * FROM (
                SELECT * FROM messages INDEXED BY idx_messages_session_id_desc
                WHERE session_id = ? ORDER BY id DESC LIMIT ?
            ) ORDER BY id ASC;
        """, (session_id, limit))
        rows = await cursor.fetchall()
    except (sqlite3.OperationalError, aiosqlite.OperationalError):
        cursor = await conn.execute("""
            SELECT * FROM (
                SELECT * FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT ?
            ) ORDER BY id ASC;
        """, (session_id, limit))
        rows = await cursor.fetchall()
    return [MessageResponse(**dict(r)) for r in rows]


async def count_session_messages(conn: aiosqlite.Connection, session_id: str) -> int:
    cursor = await conn.execute("SELECT COUNT(*) FROM messages WHERE session_id = ?;", (session_id,))
    row = await cursor.fetchone()
    return row[0] if row else 0
