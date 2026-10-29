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
from galgame2voice.telegram_bot.handlers import TelegramBotHandlers
from galgame2voice.telegram_bot import console_menus


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


async def test_telegram_character_switch_does_not_ignore_an_unconfirmed_write(stack, monkeypatch):
    manager = SimpleNamespace(switch_active_profile=AsyncMock(return_value=True))
    monkeypatch.setattr(voice_manager, "get_voice_manager", lambda: manager)
    monkeypatch.setattr(crud, "set_active_voice_profile", AsyncMock(return_value=False))
    error, note = await TelegramBotHandlers._switch_character_weights(SimpleNamespace(db_path=stack.path), stack.new.id)
    assert error and "保存" in error
    assert "离线" not in note


async def test_telegram_console_does_not_announce_success_after_a_rejected_write(stack, monkeypatch):
    manager = SimpleNamespace(switch_active_profile=AsyncMock(return_value=True))
    monkeypatch.setattr(voice_manager, "get_voice_manager", lambda: manager)
    monkeypatch.setattr(crud, "set_active_voice_profile", AsyncMock(return_value=False))
    monkeypatch.setattr(console_menus, "_edit_menu_text", AsyncMock())
    returned = AsyncMock()
    monkeypatch.setattr(console_menus, "_return_to_main_console", returned)
    monkeypatch.setattr(console_menus, "build_voice_menu", lambda **kwargs: "synthetic menu")
    context = SimpleNamespace(
        data=f"set_voice_{stack.new.id}", db_path=stack.path, query=SimpleNamespace(answer=AsyncMock())
    )
    await console_menus._handle_set_voice(context)
    answer = context.query.answer.await_args
    assert "保存" in answer.args[0] and "已切换" not in answer.args[0]
    assert answer.kwargs["show_alert"] is True
    returned.assert_not_awaited()
