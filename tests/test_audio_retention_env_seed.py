"""GALGAME2VOICE_AUDIO_RETENTION_MINUTES seeds the stored retention setting that cleanup reads."""

import aiosqlite
import pytest

from galgame2voice.config import get_settings
from galgame2voice.database import crud
from galgame2voice.database.migrations import init_schema_and_seeds


async def _seeded_retention(tmp_path) -> int:
    async with aiosqlite.connect(tmp_path / "fresh.db") as conn:
        await init_schema_and_seeds(conn)
        await conn.commit()
        return (await crud.get_settings_raw(conn)).audio_retention_minutes


@pytest.mark.asyncio
@pytest.mark.parametrize(("value", "expected"), [("5", 5), ("1440", 1440), ("0", 30), ("20000", 30), (None, 30)])
async def test_fresh_database_takes_retention_from_environment(monkeypatch, tmp_path, value, expected):
    monkeypatch.delenv("AUDIO_RETENTION_MINUTES", raising=False)
    if value is None:
        monkeypatch.delenv("GALGAME2VOICE_AUDIO_RETENTION_MINUTES", raising=False)
    else:
        monkeypatch.setenv("GALGAME2VOICE_AUDIO_RETENTION_MINUTES", value)
    get_settings.cache_clear()
    try:
        assert await _seeded_retention(tmp_path) == expected
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_existing_setting_is_not_overwritten(monkeypatch, tmp_path):
    monkeypatch.setenv("GALGAME2VOICE_AUDIO_RETENTION_MINUTES", "5")
    get_settings.cache_clear()
    try:
        async with aiosqlite.connect(tmp_path / "fresh.db") as conn:
            await init_schema_and_seeds(conn)
            await conn.execute("UPDATE settings SET audio_retention_minutes = 90 WHERE id = 1;")
            await conn.commit()
            await init_schema_and_seeds(conn)
            assert (await crud.get_settings_raw(conn)).audio_retention_minutes == 90
    finally:
        get_settings.cache_clear()
