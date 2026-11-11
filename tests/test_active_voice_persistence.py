"""A loaded voice and a durably saved default are separate observable states."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
import httpx
import pytest

from galgame2voice.config import get_settings
from galgame2voice.database import crud
from galgame2voice.database.models import VoiceProfileCreate
from galgame2voice.database.session import get_db, init_db
from galgame2voice.main import create_app
from galgame2voice.routers import characters, voice
from galgame2voice.services import voice_manager
from galgame2voice.services.gpt_sovits_client import GptSovitsClient


@pytest.fixture
async def stack(monkeypatch):
    path = str(get_settings().db_path)
    await init_db(path)
    async with get_db(path) as connection:
        profiles = [
            await crud.create_voice_profile(
                connection, VoiceProfileCreate(name=name, gpt_weights_path="", sovits_weights_path="")
            )
            for name in ["Synthetic old", "Synthetic new"]
        ]
        assert await crud.set_active_voice_profile(connection, profiles[0].id)
    client = GptSovitsClient(server=SimpleNamespace(handle_request=AsyncMock()))
    client.active_profile = profiles[0]

    async def load(profile, force=False):
        client.active_profile = profile
        return True

    monkeypatch.setattr(client, "switch_voice_profile", AsyncMock(side_effect=load))
    manager = voice_manager.VoiceManager(client, db_path=path)
    monkeypatch.setattr(manager, "_check_switch_memory_guard", lambda force: None)
    monkeypatch.setattr(voice_manager, "release_system_memory", lambda: None)
    monkeypatch.setattr(manager, "warmup_current_profile", AsyncMock(return_value=True))
    monkeypatch.setattr(
        manager,
        "is_active_profile",
        lambda profile, force=False: not force and getattr(client.active_profile, "id", None) == profile.id,
    )
    monkeypatch.setattr(voice, "get_voice_manager", lambda: manager)
    monkeypatch.setattr(characters, "get_voice_manager", lambda: manager)
    yield SimpleNamespace(manager=manager, client=client, old=profiles[0], new=profiles[1], path=path)
    await manager.aclose()
    await client.aclose()


async def stored_id(path):
    async with get_db(path) as connection:
        return (await crud.get_active_voice_profile(connection)).id


@pytest.mark.parametrize("failure", ["false", "exception", "read-only"])
async def test_manager_never_reports_a_failed_selection_write_as_success(stack, monkeypatch, failure):
    original = crud.set_active_voice_profile

    async def rejected(connection, profile_id):
        if failure == "exception":
            raise OSError("Synthetic private database path must not escape in an API message")
        if failure == "read-only":
            await connection.execute("PRAGMA query_only = ON")
            return await original(connection, profile_id)
        return False

    monkeypatch.setattr(crud, "set_active_voice_profile", rejected)
    with pytest.raises(RuntimeError, match="保存|persist"):
        await stack.manager.switch_profile(stack.new)
    assert stack.client.active_profile.id == stack.new.id
    assert await stored_id(stack.path) == stack.old.id
    stack.manager.warmup_current_profile.assert_not_awaited()
    assert not stack.manager._bg_tasks
    assert not stack.manager.switch_lock.locked()


@pytest.mark.parametrize("already_loaded", [False, True])
@pytest.mark.parametrize(
    "endpoint,field", [("/api/voice/switch", "profile_id"), ("/api/characters/switch", "character_id")]
)
async def test_switch_api_requires_saved_selection_and_allows_retry_without_reloading(
    stack, monkeypatch, endpoint, field, already_loaded
):
    if already_loaded:
        stack.client.active_profile = stack.new
    original = crud.set_active_voice_profile
    monkeypatch.setattr(crud, "set_active_voice_profile", AsyncMock(return_value=False))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app()), base_url="http://test") as client:
        response = await client.post(endpoint, json={field: stack.new.id})
        assert response.status_code == 500, response.text
        detail = response.json()["detail"]
        assert detail["error_code"] == "VOICE_PROFILE_SAVE_FAILED"
        assert "保存" in detail["message"] and "重试" in detail["message"]
        assert await stored_id(stack.path) == stack.old.id
        loads = stack.client.switch_voice_profile.await_count
        monkeypatch.setattr(crud, "set_active_voice_profile", original)
        response = await client.post(endpoint, json={field: stack.new.id})
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "switched"
        assert await stored_id(stack.path) == stack.new.id
        assert stack.client.switch_voice_profile.await_count == loads


@pytest.mark.parametrize(
    "endpoint,field", [("/api/voice/switch", "profile_id"), ("/api/characters/switch", "character_id")]
)
async def test_saved_default_does_not_prove_models_are_loaded(stack, monkeypatch, endpoint, field):
    stack.client.active_profile = None
    monkeypatch.setattr(
        stack.manager,
        "is_active_profile",
        voice_manager.VoiceManager.is_active_profile.__get__(stack.manager),
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app()), base_url="http://test") as client:
        response = await client.post(endpoint, json={field: stack.old.id})
    assert response.status_code == 200, response.text
    stack.client.switch_voice_profile.assert_awaited_once()
    assert stack.client.active_profile.id == stack.old.id


async def test_missing_modern_settings_row_does_not_confirm_an_update(stack):
    async with get_db(stack.path) as connection:
        await connection.execute("DELETE FROM settings")
        await connection.commit()
        assert await crud.set_active_voice_profile(connection, stack.new.id) is False


async def test_legacy_key_value_selection_still_saves(tmp_path):
    async with aiosqlite.connect(tmp_path / "legacy.db") as connection:
        await connection.execute("CREATE TABLE settings (key TEXT PRIMARY KEY,value TEXT)")
        assert await crud.set_active_voice_profile(connection, 7) is True
        async with connection.execute("SELECT value FROM settings WHERE key='active_voice_profile_id'") as cursor:
            assert (await cursor.fetchone())[0] == "7"


async def test_cancelled_failed_save_finishes_under_the_switch_lock_and_preserves_cause(stack, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    causes = []

    async def rejected(connection, profile_id):
        entered.set()
        await release.wait()
        return False

    monkeypatch.setattr(crud, "set_active_voice_profile", rejected)

    async def switch():
        try:
            return await stack.manager.switch_profile(stack.new)
        except asyncio.CancelledError as error:
            # Observe the exception before Python 3.10 stores task cancellation
            # as a fresh CancelledError, which loses its original exception chain.
            causes.append(error.__cause__)
            raise

    operation = asyncio.create_task(switch())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        operation.cancel()
        await asyncio.sleep(0)
        operation.cancel()
        assert stack.manager.switch_lock.locked() and not operation.done()
        release.set()
        finished, _ = await asyncio.wait({operation}, timeout=1)
        assert operation in finished
        with pytest.raises(asyncio.CancelledError):
            await operation
        assert len(causes) == 1 and isinstance(causes[0], RuntimeError)
        assert await stored_id(stack.path) == stack.old.id
        assert not stack.manager.switch_lock.locked()
        stack.manager.warmup_current_profile.assert_not_awaited()
    finally:
        release.set()
        await asyncio.gather(operation, return_exceptions=True)


async def test_runtime_only_switch_does_not_require_a_database_write(stack, monkeypatch):
    write = AsyncMock(return_value=False)
    monkeypatch.setattr(crud, "set_active_voice_profile", write)
    assert await stack.manager.switch_profile(stack.new, persist=False) is True
    write.assert_not_awaited()
    assert await stored_id(stack.path) == stack.old.id


async def test_delete_active_voice_profile_clears_settings_and_manager(stack, monkeypatch):
    assert stack.manager.active_profile.id == stack.old.id
    async with get_db(stack.path) as connection:
        raw_settings = await crud.get_settings_raw(connection)
        assert raw_settings.active_voice_profile_id == stack.old.id

    monkeypatch.setattr(
        stack.manager,
        "warmup_current_profile",
        voice_manager.VoiceManager.warmup_current_profile.__get__(stack.manager),
    )
    monkeypatch.setattr(stack.client, "check_health", AsyncMock(return_value={"connected": True}))
    warmed_profiles = []

    async def fake_switch(profile, force=False):
        warmed_profiles.append(profile)
        return True

    monkeypatch.setattr(stack.client, "switch_voice_profile", AsyncMock(side_effect=fake_switch))
    monkeypatch.setattr(stack.client, "synthesize", AsyncMock(return_value=b"audio"))

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app()), base_url="http://test") as client:
        # 1. Deleting an inactive profile must keep active settings and active manager intact
        del_inactive = await client.delete(f"/api/voice/profiles/{stack.new.id}")
        assert del_inactive.status_code == 200, del_inactive.text
        assert del_inactive.json() == {"status": "deleted", "profile_id": stack.new.id}

        assert stack.manager.active_profile is not None
        assert stack.manager.active_profile.id == stack.old.id
        async with get_db(stack.path) as connection:
            raw_settings = await crud.get_settings_raw(connection)
            assert raw_settings.active_voice_profile_id == stack.old.id

        cfg_resp = await client.get("/api/config")
        assert cfg_resp.status_code == 200
        assert cfg_resp.json()["settings"]["active_voice_profile_id"] == stack.old.id

        # 2. Deleting the currently active voice profile must clear settings and manager active profile
        resp = await client.delete(f"/api/voice/profiles/{stack.old.id}")
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"status": "deleted", "profile_id": stack.old.id}

        # 3. /api/config must now report active_voice_profile_id as None (not a dangling ID)
        cfg_resp2 = await client.get("/api/config")
        assert cfg_resp2.status_code == 200
        assert cfg_resp2.json()["settings"]["active_voice_profile_id"] is None

    async with get_db(stack.path) as connection:
        raw_settings = await crud.get_settings_raw(connection)
        assert raw_settings.active_voice_profile_id is None
        cursor = await connection.execute("SELECT active_voice_profile_id FROM settings WHERE id = 1;")
        row = await cursor.fetchone()
        assert row[0] is None

    assert stack.manager.active_profile is None

    result = await stack.manager.warmup_current_profile()
    assert result is False
    assert len(warmed_profiles) == 0


async def test_manager_delete_profile_clears_active_state(stack, monkeypatch):
    await stack.manager.switch_profile(stack.new)
    assert stack.manager.active_profile.id == stack.new.id
    async with get_db(stack.path) as connection:
        raw_settings = await crud.get_settings_raw(connection)
        assert raw_settings.active_voice_profile_id == stack.new.id

    monkeypatch.setattr(
        stack.manager,
        "warmup_current_profile",
        voice_manager.VoiceManager.warmup_current_profile.__get__(stack.manager),
    )
    monkeypatch.setattr(stack.client, "check_health", AsyncMock(return_value={"connected": True}))
    warmed_profiles = []

    async def fake_switch(profile, force=False):
        warmed_profiles.append(profile)
        return True

    monkeypatch.setattr(stack.client, "switch_voice_profile", AsyncMock(side_effect=fake_switch))
    monkeypatch.setattr(stack.client, "synthesize", AsyncMock(return_value=b"audio"))

    # 1. Deleting inactive stack.old.id must preserve active stack.new.id
    assert await stack.manager.delete_profile(stack.old.id) is True
    assert stack.manager.active_profile is not None
    assert stack.manager.active_profile.id == stack.new.id
    async with get_db(stack.path) as connection:
        raw_settings = await crud.get_settings_raw(connection)
        assert raw_settings.active_voice_profile_id == stack.new.id

    # 2. Deleting active stack.new.id must clear active profile in manager and settings
    deleted = await stack.manager.delete_profile(stack.new.id)
    assert deleted is True

    async with get_db(stack.path) as connection:
        raw_settings = await crud.get_settings_raw(connection)
        assert raw_settings.active_voice_profile_id is None

    assert stack.manager.active_profile is None

    warmed_profiles.clear()
    assert await stack.manager.warmup_current_profile() is False
    assert len(warmed_profiles) == 0



