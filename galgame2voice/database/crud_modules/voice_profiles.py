"""
Voice profile CRUD operations for SQLite persistence in galgame2voice.
"""

import logging
from typing import Any, List, Optional

import aiosqlite

from galgame2voice.database.models import (
    VoiceProfileCreate,
    VoiceProfileResponse,
    VoiceProfileUpdate,
)
from galgame2voice.database.session import immediate_transaction

logger = logging.getLogger("galgame2voice.database.crud_modules.voice_profiles")

__all__ = [
    "list_voice_profiles",
    "get_voice_profile",
    "get_voice_profile_by_name",
    "get_active_voice_profile",
    "create_voice_profile",
    "update_voice_profile",
    "set_active_voice_profile",
    "delete_voice_profile",
]


async def list_voice_profiles(conn: aiosqlite.Connection) -> List[VoiceProfileResponse]:
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute("SELECT * FROM voice_profiles ORDER BY id ASC;")
    rows = await cursor.fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["is_default"] = bool(d.get("is_default", 0))
        result.append(VoiceProfileResponse(**d))
    return result


async def get_voice_profile(conn: aiosqlite.Connection, profile_id: int) -> Optional[VoiceProfileResponse]:
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute("SELECT * FROM voice_profiles WHERE id = ?;", (profile_id,))
    row = await cursor.fetchone()
    if not row:
        return None
    d = dict(row)
    d["is_default"] = bool(d.get("is_default", 0))
    return VoiceProfileResponse(**d)


async def get_voice_profile_by_name(conn: aiosqlite.Connection, name: str) -> Optional[VoiceProfileResponse]:
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute("SELECT * FROM voice_profiles WHERE name = ? LIMIT 1;", (name,))
    row = await cursor.fetchone()
    if not row:
        return None
    d = dict(row)
    d["is_default"] = bool(d.get("is_default", 0))
    return VoiceProfileResponse(**d)


async def get_active_voice_profile(conn: aiosqlite.Connection) -> Optional[VoiceProfileResponse]:
    conn.row_factory = aiosqlite.Row
    try:
        cursor = await conn.execute("SELECT active_voice_profile_id FROM settings WHERE id = 1;")
        row = await cursor.fetchone()
        if row and row["active_voice_profile_id"]:
            profile = await get_voice_profile(conn, row["active_voice_profile_id"])
            if profile:
                return profile
    except Exception:
        try:
            cursor = await conn.execute("SELECT value FROM settings WHERE key = 'active_voice_profile_id';")
            row = await cursor.fetchone()
            if row and row[0]:
                profile = await get_voice_profile(conn, int(row[0]))
                if profile:
                    return profile
        except Exception:
            pass

    # First check settings table for active_voice_profile_id
    settings_cursor = await conn.execute("SELECT active_voice_profile_id FROM settings WHERE id = 1;")
    settings_row = await settings_cursor.fetchone()
    if settings_row and settings_row["active_voice_profile_id"]:
        cursor = await conn.execute("SELECT * FROM voice_profiles WHERE id = ?;", (settings_row["active_voice_profile_id"],))
        row = await cursor.fetchone()
        if row:
            return VoiceProfileResponse(**dict(row))

    # Fallback to is_default = 1
    cursor = await conn.execute("SELECT * FROM voice_profiles WHERE is_default = 1 LIMIT 1;")
    row = await cursor.fetchone()
    if row:
        return VoiceProfileResponse(**dict(row))

    # Fallback to first available profile
    cursor = await conn.execute("SELECT * FROM voice_profiles ORDER BY id ASC LIMIT 1;")
    row = await cursor.fetchone()
    if row:
        return VoiceProfileResponse(**dict(row))

    return None


async def create_voice_profile(conn: aiosqlite.Connection, profile: VoiceProfileCreate) -> VoiceProfileResponse:
    conn.row_factory = aiosqlite.Row
    async with immediate_transaction(conn):
        if profile.is_default:
            await conn.execute("UPDATE voice_profiles SET is_default = 0;")

        cursor = await conn.execute("""
            INSERT INTO voice_profiles (
                name, description, gpt_weights_path, sovits_weights_path,
                ref_audio_path, prompt_text, prompt_lang, text_lang, system_prompt, is_default
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """, (
            profile.name, profile.description, profile.gpt_weights_path,
            profile.sovits_weights_path, profile.ref_audio_path,
            profile.prompt_text, profile.prompt_lang, profile.text_lang,
            profile.system_prompt, 1 if profile.is_default else 0
        ))
        new_id = cursor.lastrowid
    res = await get_voice_profile(conn, new_id)
    if res is None:
        raise RuntimeError(f"Failed to retrieve created voice profile {new_id}")
    return res


async def update_voice_profile(conn: aiosqlite.Connection, profile_id: int, updates: VoiceProfileUpdate) -> Optional[VoiceProfileResponse]:
    current = await get_voice_profile(conn, profile_id)
    if not current:
        return None

    fields = []
    values: List[Any] = []
    up_dict = updates.model_dump(exclude_unset=True)

    for k, v in up_dict.items():
        if k == "is_default":
            fields.append("is_default = ?")
            values.append(1 if v else 0)
        else:
            fields.append(f"{k} = ?")
            values.append(v)

    if fields:
        fields.append("updated_at = CURRENT_TIMESTAMP")
        values.append(profile_id)
        query = f"UPDATE voice_profiles SET {', '.join(fields)} WHERE id = ?;"
        async with immediate_transaction(conn):
            await conn.execute(query, tuple(values))

    return await get_voice_profile(conn, profile_id)


async def set_active_voice_profile(conn: aiosqlite.Connection, profile_id: int) -> bool:
    try:
        async with immediate_transaction(conn):
            await conn.execute("UPDATE settings SET active_voice_profile_id = ?, updated_at = CURRENT_TIMESTAMP WHERE id = 1;", (profile_id,))
        return True
    except Exception:
        try:
            async with immediate_transaction(conn):
                await conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('active_voice_profile_id', ?);", (str(profile_id),))
            return True
        except Exception:
            return False


async def delete_voice_profile(conn: aiosqlite.Connection, profile_id: int) -> bool:
    async with immediate_transaction(conn):
        cursor = await conn.execute("DELETE FROM voice_profiles WHERE id = ?;", (profile_id,))
    return cursor.rowcount > 0
