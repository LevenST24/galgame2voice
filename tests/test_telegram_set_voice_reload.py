"""Telegram console must reload weights when the saved voice is not in the engine.

The menu used to treat the database active id as proof the engine had that
voice. After a restart, or after a switch saved while the engine was offline,
choosing that same voice has to load it. Choosing it again once it is loaded
stays a no-op toast.
"""

import sqlite3
from unittest.mock import AsyncMock

import pytest

from galgame2voice.database import crud
from galgame2voice.database.models import VoiceProfileCreate
from galgame2voice.database.session import get_db, init_db
from galgame2voice.services.voice_manager import VoiceManager, set_voice_manager
from galgame2voice.telegram_bot.handlers import TelegramBotHandlers
from telegram_test_support import TELEGRAM_TEST_ADMINS
from tests.conftest import MockGptSovitsServer


WEIGHT_ENDPOINTS = ("/set_gpt_weights", "/set_sovits_weights")


class _Query:
    def __init__(self, data):
        self.data = data
        self.answer_text = None
        self.show_alert = False
        self.edited_text = None

    async def answer(self, text=None, show_alert=False):
        self.answer_text = text
        self.show_alert = show_alert

    async def edit_message_text(self, text, reply_markup=None):
        self.edited_text = text


class _Update:
    def __init__(self, data):
        self.callback_query = _Query(data)
        self.effective_chat = type("Chat", (), {"id": 1002})()


class _Context:
    def __init__(self):
        self.bot = None


def _weight_calls(server):
    return [call for call in server.call_history if call["endpoint"] in WEIGHT_ENDPOINTS]


async def _saved_voice(db_path):
    await init_db(db_path)
    async with get_db(db_path) as conn:
        profile = await crud.create_voice_profile(
            conn,
            VoiceProfileCreate(
                name="音色A",
                gpt_weights_path="weights/voice-a.ckpt",
                sovits_weights_path="weights/voice-a.pth",
                ref_audio_path="ref/voice-a.wav",
                prompt_text="こんにちは",
                prompt_lang="ja",
                text_lang="ja",
            ),
        )
        assert await crud.set_active_voice_profile(conn, profile.id)
        return await crud.get_voice_profile(conn, profile.id)


def _standin(db_path, monkeypatch):
    server = MockGptSovitsServer()
    server.current_gpt_weights = None
    server.current_sovits_weights = None
    manager = VoiceManager(gpt_sovits_client_or_server=server, db_path=db_path)
    monkeypatch.setattr(manager, "_check_switch_memory_guard", lambda force: None)
    monkeypatch.setattr(manager, "warmup_current_profile", AsyncMock(return_value=True))
    monkeypatch.setattr(
        "galgame2voice.services.voice_manager.release_system_memory",
        lambda: None,
    )
    set_voice_manager(manager)
    return server, manager


async def _choose(handlers, callback_data):
    update = _Update(callback_data)
    await handlers.handle_callback_query(update, _Context())
    return update.callback_query.answer_text


@pytest.mark.parametrize("prefix", ["set_voice_", "set_char_"])
@pytest.mark.asyncio
async def test_console_reloads_saved_voice_when_standin_has_no_weights(temp_db_path, monkeypatch, prefix):
    """Database says A is current and the stand-in has no weights: choosing A loads A."""
    profile = await _saved_voice(temp_db_path)
    server, manager = _standin(temp_db_path, monkeypatch)
    assert manager.client.current_gpt_weights is None
    assert manager.is_active_profile(profile) is False
    handlers = TelegramBotHandlers(admin_ids=TELEGRAM_TEST_ADMINS, db_path=temp_db_path)

    answer = await _choose(handlers, f"{prefix}{profile.id}")

    assert answer != "已经是当前音色，无需切换"
    assert "音色已切换为" in (answer or "")
    assert profile.name in (answer or "")
    assert manager.client.current_gpt_weights == profile.gpt_weights_path
    assert server.current_gpt_weights == profile.gpt_weights_path
    assert server.current_sovits_weights == profile.sovits_weights_path
    assert _weight_calls(server)


@pytest.mark.parametrize("prefix", ["set_voice_", "set_char_"])
@pytest.mark.asyncio
async def test_console_keeps_already_loaded_voice(temp_db_path, monkeypatch, prefix):
    """Once the stand-in has loaded A, choosing A again only says it is current."""
    profile = await _saved_voice(temp_db_path)
    server, manager = _standin(temp_db_path, monkeypatch)
    assert await manager.client.switch_voice_profile(profile) is True
    assert manager.is_active_profile(profile) is True
    before = list(server.call_history)
    handlers = TelegramBotHandlers(admin_ids=TELEGRAM_TEST_ADMINS, db_path=temp_db_path)

    answer = await _choose(handlers, f"{prefix}{profile.id}")

    assert answer == "已经是当前音色，无需切换"
    assert server.call_history == before
    assert manager.client.current_gpt_weights == profile.gpt_weights_path


async def _add_second_voice(db_path):
    async with get_db(db_path) as conn:
        return await crud.create_voice_profile(
            conn,
            VoiceProfileCreate(
                name="音色B",
                gpt_weights_path="weights/voice-b.ckpt",
                sovits_weights_path="weights/voice-b.pth",
                ref_audio_path="ref/voice-b.wav",
                prompt_text="さようなら",
                prompt_lang="ja",
                text_lang="ja",
            ),
        )


@pytest.mark.parametrize("prefix", ["set_voice_", "set_char_"])
@pytest.mark.asyncio
async def test_console_rejects_nonexistent_voice_id(temp_db_path, monkeypatch, prefix):
    """Clicking a deleted or nonexistent voice must not switch or pollute settings."""
    profile = await _saved_voice(temp_db_path)
    server, manager = _standin(temp_db_path, monkeypatch)
    handlers = TelegramBotHandlers(admin_ids=TELEGRAM_TEST_ADMINS, db_path=temp_db_path)

    nonexistent_id = 9999
    update = _Update(f"{prefix}{nonexistent_id}")
    await handlers.handle_callback_query(update, _Context())

    answer = update.callback_query.answer_text
    assert "已切换" not in (answer or "")
    assert ("不存在" in (answer or "") or "删除" in (answer or ""))
    assert update.callback_query.show_alert is True
    assert update.callback_query.edited_text is not None
    assert "选择角色音色" in update.callback_query.edited_text

    async with get_db(temp_db_path) as conn:
        cursor = await conn.execute("SELECT active_voice_profile_id FROM settings WHERE id = 1;")
        row = await cursor.fetchone()
        assert row["active_voice_profile_id"] == profile.id


@pytest.mark.parametrize("prefix", ["set_voice_", "set_char_"])
@pytest.mark.asyncio
async def test_console_saves_existing_voice_when_engine_offline(temp_db_path, monkeypatch, prefix):
    """Clicking an existing voice when the engine is offline still saves it and notes offline status."""
    await _saved_voice(temp_db_path)
    profile_b = await _add_second_voice(temp_db_path)
    server, manager = _standin(temp_db_path, monkeypatch)
    server.set_online(False)
    handlers = TelegramBotHandlers(admin_ids=TELEGRAM_TEST_ADMINS, db_path=temp_db_path)

    update = _Update(f"{prefix}{profile_b.id}")
    await handlers.handle_callback_query(update, _Context())

    answer = update.callback_query.answer_text
    assert "音色已切换为" in (answer or "")
    assert "语音引擎离线" in (answer or "")
    assert profile_b.name in (answer or "")
    assert update.callback_query.show_alert is True

    async with get_db(temp_db_path) as conn:
        cursor = await conn.execute("SELECT active_voice_profile_id FROM settings WHERE id = 1;")
        row = await cursor.fetchone()
        assert row["active_voice_profile_id"] == profile_b.id


@pytest.mark.asyncio
async def test_console_rejects_deleted_profile_after_actual_db_deletion(temp_db_path, monkeypatch):
    """Create profile A and B, delete B from DB, then click B's button."""
    profile_a = await _saved_voice(temp_db_path)
    profile_b = await _add_second_voice(temp_db_path)
    server, manager = _standin(temp_db_path, monkeypatch)
    handlers = TelegramBotHandlers(admin_ids=TELEGRAM_TEST_ADMINS, db_path=temp_db_path)

    async with get_db(temp_db_path) as conn:
        assert await crud.delete_voice_profile(conn, profile_b.id) is True

    update = _Update(f"set_voice_{profile_b.id}")
    await handlers.handle_callback_query(update, _Context())

    answer = update.callback_query.answer_text
    assert "已切换" not in (answer or "")
    assert ("不存在" in (answer or "") or "删除" in (answer or ""))
    assert update.callback_query.show_alert is True
    assert update.callback_query.edited_text is not None
    assert "选择角色音色" in update.callback_query.edited_text

    async with get_db(temp_db_path) as conn:
        cursor = await conn.execute("SELECT active_voice_profile_id FROM settings WHERE id = 1;")
        row = await cursor.fetchone()
        assert row["active_voice_profile_id"] == profile_a.id


@pytest.mark.asyncio
async def test_console_handles_db_error_during_lookup(temp_db_path, monkeypatch):
    """If get_voice_profile raises an unexpected SQLite error, handle gracefully without UnboundLocalError."""
    profile = await _saved_voice(temp_db_path)
    server, manager = _standin(temp_db_path, monkeypatch)
    handlers = TelegramBotHandlers(admin_ids=TELEGRAM_TEST_ADMINS, db_path=temp_db_path)

    async def _failing_get(*args, **kwargs):
        raise sqlite3.OperationalError("database disk image is malformed")

    monkeypatch.setattr(crud, "get_voice_profile", _failing_get)

    update = _Update("set_voice_42")
    await handlers.handle_callback_query(update, _Context())

    answer = update.callback_query.answer_text
    assert "切换异常" in (answer or "")
    assert update.callback_query.show_alert is True

    async with get_db(temp_db_path) as conn:
        cursor = await conn.execute("SELECT active_voice_profile_id FROM settings WHERE id = 1;")
        row = await cursor.fetchone()
        assert row["active_voice_profile_id"] == profile.id


