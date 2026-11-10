"""Regression tests for fresh database seed default provider consistency."""

from httpx import ASGITransport, AsyncClient
import pytest

from galgame2voice.config import get_settings
from galgame2voice.database import crud
from galgame2voice.database.migrations import CURRENT_SCHEMA_VERSION
from galgame2voice.database.session import get_db, get_schema_version, init_db
from galgame2voice.main import create_app


@pytest.fixture
def isolated_fresh_db_path(tmp_path, monkeypatch):
    """Prepare an empty database path under tmp_path without prebuilt tables."""
    db_path = tmp_path / "data" / "fresh.db"

    monkeypatch.setenv("GALGAME2VOICE_PROJECT_ROOT", str(tmp_path))
    monkeypatch.delenv("DATA_DIR_NAME", raising=False)
    monkeypatch.setenv("GALGAME2VOICE_DB_PATH", str(db_path))
    monkeypatch.setenv("GALGAME_DB_PATH", str(db_path))
    get_settings.cache_clear()
    try:
        yield db_path
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_fresh_db_seed_default_provider_consistent_deepseek(isolated_fresh_db_path):
    """Fresh init_db must seed deepseek as the sole active provider matching settings.active_provider_id."""
    db_path = isolated_fresh_db_path
    assert not db_path.exists()

    await init_db(db_path)

    async with get_db(db_path) as conn:
        active = await crud.get_active_provider_raw(conn)
        assert active is not None
        assert active.id == "deepseek"

        settings = await crud.get_settings_raw(conn)
        assert settings.active_provider_id == "deepseek"
        assert active.id == settings.active_provider_id

        cursor = await conn.execute("SELECT id FROM providers WHERE is_active = 1;")
        active_ids = [row[0] for row in await cursor.fetchall()]
        assert active_ids == ["deepseek"]

    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/config")
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["settings"]["active_provider_id"] == "deepseek"
        assert payload["active_provider"] is not None
        assert payload["active_provider"]["id"] == "deepseek"


@pytest.mark.asyncio
async def test_reinit_db_preserves_existing_active_provider_selection(isolated_fresh_db_path):
    """Subsequent init_db calls on an existing database must not overwrite user-selected provider."""
    db_path = isolated_fresh_db_path
    await init_db(db_path)

    async with get_db(db_path) as conn:
        assert await crud.set_active_provider(conn, "gemini") is True

    await init_db(db_path)

    async with get_db(db_path) as conn:
        active = await crud.get_active_provider_raw(conn)
        assert active is not None
        assert active.id == "gemini"

        settings = await crud.get_settings_raw(conn)
        assert settings.active_provider_id == "gemini"

        cursor = await conn.execute("SELECT id FROM providers WHERE is_active = 1;")
        active_ids = [row[0] for row in await cursor.fetchall()]
        assert active_ids == ["gemini"]

        assert await get_schema_version(conn) == 7
        assert CURRENT_SCHEMA_VERSION == 7
