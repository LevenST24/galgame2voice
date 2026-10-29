"""Database identity selection shared by chat context and cached TTS snapshots."""

import asyncio
from typing import Any

import aiosqlite

from galgame2voice.database import crud
from galgame2voice.services import character_identity


async def select_voice_profile(
    conn: aiosqlite.Connection,
    profile_id: int | None = None,
    character_name: str | None = None,
) -> Any | None:
    """Use the ID, an explicit unique name fallback, or the unspecified default.

    The caller owns the connection and any surrounding transaction. No filesystem
    reference preparation or model loading belongs to identity selection.
    """
    name = character_name.strip() if character_name else None
    profile = await crud.get_voice_profile(conn, int(profile_id)) if profile_id is not None else None
    if profile is None and name:
        profiles = await crud.list_voice_profiles(conn)
        profile, _, conflicting = await asyncio.to_thread(character_identity.resolve_profile_for_query, profiles, name)
        if conflicting:
            raise character_identity.AmbiguousProfileName(
                "角色名匹配到多个音色，请在「设置中心 → 角色与会话」选择唯一音色。"
            )
    if profile_id is not None or name:
        return profile
    return await crud.get_active_voice_profile(conn)
