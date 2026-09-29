"""
User memory and character affection CRUD operations for SQLite persistence in galgame2voice.
"""

from datetime import date
import json
import logging
from typing import Any, Dict, List, Optional, Tuple

import aiosqlite

from galgame2voice.database.models import (
    CharacterAffectionResponse,
    CharacterAffectionUpdate,
    UserMemoryCreate,
    UserMemoryResponse,
    UserMemoryUpdate,
)
from galgame2voice.database.session import immediate_transaction

logger = logging.getLogger("galgame2voice.database.crud_modules.memory_affection")

__all__ = [
    "create_memory",
    "get_memory",
    "list_memories",
    "update_memory",
    "delete_memory",
    "clear_memories",
    "upsert_memory",
    "record_memory_recall",
    "record_memory_recall_batch",
    "calculate_affection_level",
    "_format_affection_response",
    "get_or_create_character_affection",
    "get_user_affections_for_profiles",
    "get_character_affection",
    "update_character_affection",
    "reset_character_affection",
    "unlock_character_dialogues",
    "increment_affection",
]


async def create_memory(conn: aiosqlite.Connection, memory: UserMemoryCreate) -> UserMemoryResponse:
    conn.row_factory = aiosqlite.Row
    async with immediate_transaction(conn):
        cursor = await conn.execute("""
            INSERT INTO user_memories (
                user_id, character_id, category, fact_key, fact_value,
                confidence, source_message_id, recall_count, last_recalled_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            RETURNING *;
        """, (
            memory.user_id, memory.character_id, memory.category, memory.fact_key, memory.fact_value,
            memory.confidence, memory.source_message_id, memory.recall_count, memory.last_recalled_at
        ))
        row = await cursor.fetchone()
        if not row and cursor.lastrowid:
            cur2 = await conn.execute("SELECT * FROM user_memories WHERE id = ?;", (cursor.lastrowid,))
            row = await cur2.fetchone()
    if not row:
        raise RuntimeError("Failed to persist user memory")
    return UserMemoryResponse(**dict(row))


async def get_memory(conn: aiosqlite.Connection, memory_id: int) -> Optional[UserMemoryResponse]:
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute("SELECT * FROM user_memories WHERE id = ?;", (memory_id,))
    row = await cursor.fetchone()
    if not row:
        return None
    return UserMemoryResponse(**dict(row))


async def list_memories(
    conn: aiosqlite.Connection,
    user_id: str = "default_user",
    character_id: Optional[int] = None,
    category: Optional[str] = None,
    limit: int = 100
) -> List[UserMemoryResponse]:
    conn.row_factory = aiosqlite.Row
    conditions = ["user_id = ?"]
    params: List[Any] = [user_id]

    if character_id is not None:
        conditions.append("(character_id = ? OR character_id IS NULL)")
        params.append(character_id)

    if category:
        conditions.append("category = ?")
        params.append(category)

    where_clause = " AND ".join(conditions)
    params.append(limit)

    query = f"SELECT * FROM user_memories WHERE {where_clause} ORDER BY updated_at DESC LIMIT ?;"
    cursor = await conn.execute(query, tuple(params))
    rows = await cursor.fetchall()
    return [UserMemoryResponse(**dict(r)) for r in rows]


async def update_memory(
    conn: aiosqlite.Connection,
    memory_id: int,
    updates: UserMemoryUpdate
) -> Optional[UserMemoryResponse]:
    current = await get_memory(conn, memory_id)
    if not current:
        return None

    fields = []
    values: List[Any] = []
    up_dict = updates.model_dump(exclude_unset=True)

    for k, v in up_dict.items():
        if v is not None:
            fields.append(f"{k} = ?")
            values.append(v)

    if fields:
        fields.append("updated_at = CURRENT_TIMESTAMP")
        values.append(memory_id)
        query = f"UPDATE user_memories SET {', '.join(fields)} WHERE id = ?;"
        async with immediate_transaction(conn):
            await conn.execute(query, tuple(values))

    return await get_memory(conn, memory_id)


async def delete_memory(conn: aiosqlite.Connection, memory_id: int) -> bool:
    async with immediate_transaction(conn):
        cursor = await conn.execute("DELETE FROM user_memories WHERE id = ?;", (memory_id,))
    return cursor.rowcount > 0


async def clear_memories(
    conn: aiosqlite.Connection,
    user_id: str = "default_user",
    character_id: Optional[int] = None
) -> int:
    conditions = ["user_id = ?"]
    params: List[Any] = [user_id]
    if character_id is not None:
        conditions.append("character_id = ?")
        params.append(character_id)

    where_clause = " AND ".join(conditions)
    async with immediate_transaction(conn):
        cursor = await conn.execute(f"DELETE FROM user_memories WHERE {where_clause};", tuple(params))
    return cursor.rowcount


async def upsert_memory(conn: aiosqlite.Connection, memory: UserMemoryCreate) -> Optional[UserMemoryResponse]:
    conn.row_factory = aiosqlite.Row
    character_id = memory.character_id if memory.character_id is not None else 1
    async with immediate_transaction(conn):
        cursor = await conn.execute("""
            INSERT INTO user_memories (
                user_id, character_id, category, fact_key, fact_value,
                confidence, source_message_id, recall_count, last_recalled_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, NULL)
            ON CONFLICT(user_id, character_id, fact_key) DO UPDATE SET
                fact_value = excluded.fact_value,
                category = excluded.category,
                confidence = excluded.confidence,
                source_message_id = COALESCE(excluded.source_message_id, source_message_id),
                updated_at = CURRENT_TIMESTAMP
            RETURNING *;
        """, (
            memory.user_id, character_id, memory.category, memory.fact_key, memory.fact_value,
            memory.confidence, memory.source_message_id,
        ))
        row = await cursor.fetchone()
        if not row:
            cur2 = await conn.execute(
                "SELECT * FROM user_memories WHERE user_id = ? AND character_id = ? AND fact_key = ?;",
                (memory.user_id, character_id, memory.fact_key),
            )
            row = await cur2.fetchone()
    return UserMemoryResponse(**dict(row)) if row else None


async def record_memory_recall(conn: aiosqlite.Connection, memory_id: int) -> None:
    async with immediate_transaction(conn):
        await conn.execute("""
            UPDATE user_memories
            SET recall_count = recall_count + 1, last_recalled_at = CURRENT_TIMESTAMP
            WHERE id = ?;
        """, (memory_id,))


async def record_memory_recall_batch(conn: aiosqlite.Connection, memory_ids: List[int]) -> None:
    """Records recall for multiple memories in a single transaction."""
    ids = list(dict.fromkeys(memory_ids))
    if not ids:
        return
    async with immediate_transaction(conn):
        await conn.executemany("""
            UPDATE user_memories
            SET recall_count = recall_count + 1, last_recalled_at = CURRENT_TIMESTAMP
            WHERE id = ?;
        """, [(mid,) for mid in ids])


def calculate_affection_level(score: int) -> Tuple[int, str]:
    """Calculates intimacy tier (1-5) and Chinese tier name from 0-100 score."""
    clamped = max(0, min(100, score))
    if clamped >= 80:
        return 5, "恋慕/誓约"
    elif clamped >= 60:
        return 4, "亲密/依赖"
    elif clamped >= 40:
        return 3, "友好/信任"
    elif clamped >= 20:
        return 2, "熟悉/同伴"
    else:
        return 1, "初识/生疏"


def _format_affection_response(row_dict: Dict[str, Any]) -> CharacterAffectionResponse:
    d = dict(row_dict)
    raw_unlocked = d.get("unlocked_dialogues", "[]")
    if isinstance(raw_unlocked, str):
        try:
            d["unlocked_dialogues"] = json.loads(raw_unlocked)
        except Exception:
            d["unlocked_dialogues"] = []
    elif not isinstance(raw_unlocked, list):
        d["unlocked_dialogues"] = []

    level, level_name = calculate_affection_level(d.get("affection_score", 0))
    d["affection_level"] = level
    d["level_name"] = level_name
    return CharacterAffectionResponse(**d)


async def get_or_create_character_affection(
    conn: aiosqlite.Connection,
    user_id: str = "default_user",
    character_id: int = 1
) -> CharacterAffectionResponse:
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute("""
        SELECT * FROM character_affection WHERE user_id = ? AND character_id = ?;
    """, (user_id, character_id))
    row = await cursor.fetchone()

    if row:
        return _format_affection_response(dict(row))

    async with immediate_transaction(conn):
        await conn.execute("""
            INSERT INTO character_affection (
                user_id, character_id, affection_score, affection_level, current_emotion,
                interaction_count, daily_points_earned, last_interaction_date, unlocked_dialogues, custom_nickname
            ) VALUES (?, ?, 0, 1, 'normal', 0, 0, '', '[]', NULL)
            ON CONFLICT(user_id, character_id) DO NOTHING;
        """, (user_id, character_id))

    cursor = await conn.execute("""
        SELECT * FROM character_affection WHERE user_id = ? AND character_id = ?;
    """, (user_id, character_id))
    row = await cursor.fetchone()
    if row:
        return _format_affection_response(dict(row))

    return CharacterAffectionResponse(
        id=0,
        user_id=user_id,
        character_id=character_id,
        affection_score=0,
        affection_level=1,
        level_name="初识/生疏",
        current_emotion="normal",
        interaction_count=0,
        daily_points_earned=0,
        last_interaction_date="",
        unlocked_dialogues=[],
        custom_nickname=None,
    )


async def get_user_affections_for_profiles(
    conn: aiosqlite.Connection,
    user_id: str = "default_user",
    profile_ids: Optional[List[int]] = None,
) -> Dict[int, CharacterAffectionResponse]:
    """
    Fetches character affections for multiple profiles in a single batched query,
    preserving get-or-create semantics per profile.
    """
    if not profile_ids:
        return {}

    conn.row_factory = aiosqlite.Row
    placeholders = ",".join("?" for _ in profile_ids)
    cursor = await conn.execute(f"""
        SELECT * FROM character_affection WHERE user_id = ? AND character_id IN ({placeholders});
    """, [user_id, *profile_ids])
    rows = await cursor.fetchall()
    result_map: Dict[int, CharacterAffectionResponse] = {
        row["character_id"]: _format_affection_response(dict(row))
        for row in rows
    }

    missing_ids = [pid for pid in profile_ids if pid not in result_map]
    if missing_ids:
        async with immediate_transaction(conn):
            insert_params = [(user_id, pid) for pid in missing_ids]
            await conn.executemany("""
                INSERT INTO character_affection (
                    user_id, character_id, affection_score, affection_level, current_emotion,
                    interaction_count, daily_points_earned, last_interaction_date, unlocked_dialogues, custom_nickname
                ) VALUES (?, ?, 0, 1, 'normal', 0, 0, '', '[]', NULL)
                ON CONFLICT(user_id, character_id) DO NOTHING;
            """, insert_params)

        missing_placeholders = ",".join("?" for _ in missing_ids)
        cursor = await conn.execute(f"""
            SELECT * FROM character_affection WHERE user_id = ? AND character_id IN ({missing_placeholders});
        """, [user_id, *missing_ids])
        new_rows = await cursor.fetchall()
        for row in new_rows:
            result_map[row["character_id"]] = _format_affection_response(dict(row))

        for pid in missing_ids:
            if pid not in result_map:
                result_map[pid] = CharacterAffectionResponse(
                    id=0,
                    user_id=user_id,
                    character_id=pid,
                    affection_score=0,
                    affection_level=1,
                    level_name="初识/生疏",
                    current_emotion="normal",
                    interaction_count=0,
                    daily_points_earned=0,
                    last_interaction_date="",
                    unlocked_dialogues=[],
                    custom_nickname=None,
                )

    return result_map


async def get_character_affection(
    conn: aiosqlite.Connection,
    user_id: str = "default_user",
    character_id: int = 1
) -> Optional[CharacterAffectionResponse]:
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute("""
        SELECT * FROM character_affection WHERE user_id = ? AND character_id = ?;
    """, (user_id, character_id))
    row = await cursor.fetchone()
    if not row:
        return None
    return _format_affection_response(dict(row))


async def update_character_affection(
    conn: aiosqlite.Connection,
    user_id: str = "default_user",
    character_id: int = 1,
    updates: Optional[CharacterAffectionUpdate] = None
) -> Optional[CharacterAffectionResponse]:
    current = await get_or_create_character_affection(conn, user_id, character_id)
    if not updates:
        return current

    fields = []
    values: List[Any] = []
    up_dict = updates.model_dump(exclude_unset=True)

    if "affection_score" in up_dict and up_dict["affection_score"] is not None:
        score = max(0, min(100, up_dict["affection_score"]))
        fields.append("affection_score = ?")
        values.append(score)
        level, _ = calculate_affection_level(score)
        fields.append("affection_level = ?")
        values.append(level)
        up_dict.pop("affection_level", None)

    for k, v in up_dict.items():
        if k == "affection_score":
            continue
        if v is not None:
            if k == "unlocked_dialogues":
                fields.append("unlocked_dialogues = ?")
                values.append(json.dumps(v, ensure_ascii=False))
            else:
                fields.append(f"{k} = ?")
                values.append(v)

    if fields:
        fields.append("updated_at = CURRENT_TIMESTAMP")
        values.extend([user_id, character_id])
        query = f"UPDATE character_affection SET {', '.join(fields)} WHERE user_id = ? AND character_id = ?;"
        async with immediate_transaction(conn):
            await conn.execute(query, tuple(values))

    return await get_character_affection(conn, user_id, character_id)


async def reset_character_affection(
    conn: aiosqlite.Connection,
    user_id: str = "default_user",
    character_id: int = 1
) -> CharacterAffectionResponse:
    await get_or_create_character_affection(conn, user_id, character_id)
    async with immediate_transaction(conn):
        await conn.execute("""
            UPDATE character_affection
            SET affection_score = 0, affection_level = 1, current_emotion = 'normal',
                interaction_count = 0, daily_points_earned = 0, last_interaction_date = '',
                unlocked_dialogues = '[]', custom_nickname = NULL, updated_at = CURRENT_TIMESTAMP
            WHERE user_id = ? AND character_id = ?;
        """, (user_id, character_id))
    return await get_or_create_character_affection(conn, user_id, character_id)


async def unlock_character_dialogues(
    conn: aiosqlite.Connection,
    user_id: str,
    character_id: int,
    dialogue_ids_to_add: List[str],
) -> List[str]:
    """
    Atomically appends new dialogue IDs to unlocked_dialogues under an immediate transaction.
    Guarantees no lost updates or race conditions when concurrent requests unlock dialogues.
    """
    if not dialogue_ids_to_add:
        aff = await get_character_affection(conn, user_id, character_id)
        return aff.unlocked_dialogues if aff else []

    await get_or_create_character_affection(conn, user_id, character_id)

    async with immediate_transaction(conn):
        cursor = await conn.execute(
            "SELECT unlocked_dialogues FROM character_affection WHERE user_id = ? AND character_id = ?;",
            (user_id, character_id),
        )
        row = await cursor.fetchone()
        raw_json = row[0] if row else "[]"
        try:
            current_list = json.loads(raw_json) if raw_json else []
            if not isinstance(current_list, list):
                current_list = []
        except Exception:
            current_list = []

        seen = set(current_list)
        changed = False
        for d_id in dialogue_ids_to_add:
            if d_id and d_id not in seen:
                current_list.append(d_id)
                seen.add(d_id)
                changed = True

        if changed:
            await conn.execute(
                "UPDATE character_affection SET unlocked_dialogues = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE user_id = ? AND character_id = ?;",
                (json.dumps(current_list, ensure_ascii=False), user_id, character_id),
            )
        return current_list


async def increment_affection(
    conn: aiosqlite.Connection,
    user_id: str,
    character_id: int,
    delta_points: int,
    emotion: Optional[str] = None,
    daily_limit: int = 15,
    today_date_str: Optional[str] = None
) -> Tuple[CharacterAffectionResponse, int, bool]:
    """
    Increments affection with daily cap checking atomically in a single transaction.
    Returns (updated_affection, actual_points_gained, did_level_up).
    """
    today = today_date_str or date.today().isoformat()
    current = await get_or_create_character_affection(conn, user_id, character_id)

    async with immediate_transaction(conn):
        cursor = await conn.execute(
            "SELECT affection_score, affection_level, daily_points_earned, last_interaction_date "
            "FROM character_affection WHERE user_id = ? AND character_id = ?;",
            (user_id, character_id),
        )
        row = await cursor.fetchone()
        old_score = row[0] if row else current.affection_score
        old_level = row[1] if row else current.affection_level
        daily_before = (row[2] if row and row[3] == today else 0)

        daily_remaining = max(0, daily_limit - daily_before)
        pts_to_add = min(max(0, delta_points), daily_remaining)
        new_score = min(100, max(0, old_score + pts_to_add))
        new_daily = daily_before + pts_to_add
        new_level, _ = calculate_affection_level(new_score)

        await conn.execute("""
            UPDATE character_affection
            SET interaction_count = interaction_count + 1,
                last_interaction_date = ?,
                daily_points_earned = ?,
                affection_score = ?,
                affection_level = ?,
                current_emotion = COALESCE(?, current_emotion),
                updated_at = CURRENT_TIMESTAMP
            WHERE user_id = ? AND character_id = ?;
        """, (today, new_daily, new_score, new_level, emotion, user_id, character_id))

    updated = await get_or_create_character_affection(conn, user_id, character_id)
    actual_gain = pts_to_add
    level_up = new_level > old_level
    return updated, actual_gain, level_up
