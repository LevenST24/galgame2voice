"""
Regression tests for Telegram bot fixes: whitelist enforcement for every command
and button.
"""

from types import SimpleNamespace

import pytest

from galgame2voice.database import crud
from galgame2voice.database.models import VoiceProfileCreate
from galgame2voice.database.session import get_db
from galgame2voice.telegram_bot import TelegramBotHandlers

ADMIN_ID = 4242
STRANGER_ID = 9999
GROUP_CHAT_ID = -1001234567


class RecordingBot:
    """Minimal stand-in for telegram.Bot that records outgoing traffic."""

    def __init__(self):
        self.sent_messages: list[dict] = []
        self.sent_voices: list[dict] = []

    async def send_chat_action(self, chat_id, action, **kwargs):
        return True

    async def send_message(self, chat_id, text, reply_markup=None, **kwargs):
        self.sent_messages.append({"chat_id": chat_id, "text": text})

    async def send_voice(self, chat_id, voice, caption=None, **kwargs):
        self.sent_voices.append({"chat_id": chat_id, "caption": caption})


class FakeMessage:
    def __init__(self, text=None, voice=None):
        self.text = text
        self.voice = voice
        self.replies: list[str] = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)


class FakeQuery:
    def __init__(self, data):
        self.data = data
        self.answers: list[tuple] = []
        self.edited_text = None

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))

    async def edit_message_text(self, text, reply_markup=None):
        self.edited_text = text


def make_update(chat_id, user_id, text=None, voice=None):
    return SimpleNamespace(
        effective_chat=SimpleNamespace(id=chat_id),
        effective_user=SimpleNamespace(id=user_id),
        message=FakeMessage(text=text, voice=voice),
    )


def make_callback_update(data, chat_id, user_id):
    return SimpleNamespace(
        callback_query=FakeQuery(data),
        effective_chat=SimpleNamespace(id=chat_id),
        effective_user=SimpleNamespace(id=user_id),
        message=None,
    )


def make_context(bot=None):
    return SimpleNamespace(bot=bot or RecordingBot())


async def create_profile(conn, name, system_prompt=""):
    # Seeds the settings row; set_active_voice_profile is a silent no-op without it.
    await crud.get_settings_raw(conn)
    return await crud.create_voice_profile(conn, VoiceProfileCreate(
        name=name,
        system_prompt=system_prompt,
        gpt_weights_path=f"{name}.ckpt",
        sovits_weights_path=f"{name}.pth",
        ref_audio_path=f"{name}.wav",
        prompt_text="テスト",
        prompt_lang="ja",
        text_lang="ja",
    ))


COMMAND_CASES = [
    ("handle_start", "/start"),
    ("handle_help", "/help"),
    ("handle_voice", "/voice"),
    ("handle_model", "/model"),
    ("handle_console", "/console"),
    ("handle_reset", "/reset"),
    ("handle_nickname", "/nickname 小夏"),
    ("handle_character", "/character 角色乙"),
    ("handle_unknown", "/whatever"),
]


class TestCommandAuthorization:
    @pytest.mark.parametrize("method_name,text", COMMAND_CASES)
    @pytest.mark.asyncio
    async def test_non_admin_command_is_rejected_with_user_id(self, temp_db_path, method_name, text):
        handlers = TelegramBotHandlers(admin_ids=[ADMIN_ID], db_path=temp_db_path)
        update = make_update(STRANGER_ID, STRANGER_ID, text=text)

        reply = await getattr(handlers, method_name)(update, make_context())

        assert "没有使用本机器人的权限" in reply
        assert str(STRANGER_ID) in reply
        assert update.message.replies == [reply]

    @pytest.mark.asyncio
    async def test_empty_whitelist_rejects_everyone(self, temp_db_path):
        handlers = TelegramBotHandlers(admin_ids=[], db_path=temp_db_path)
        update = make_update(ADMIN_ID, ADMIN_ID, text="/start")

        reply = await handlers.handle_start(update, make_context())

        assert str(ADMIN_ID) in reply
        assert "二次元AI伴侣" not in reply

    @pytest.mark.asyncio
    async def test_non_admin_cannot_switch_global_character(self, temp_db_path, monkeypatch):
        from galgame2voice.services import voice_manager

        class AlwaysSwitches:
            async def switch_active_profile(self, profile_id):
                return True

        monkeypatch.setattr(voice_manager, "get_voice_manager", lambda: AlwaysSwitches())
        async with get_db(temp_db_path) as conn:
            alpha = await create_profile(conn, "角色甲")
            await create_profile(conn, "角色乙")
            await crud.set_active_voice_profile(conn, alpha.id)
        handlers = TelegramBotHandlers(admin_ids=[ADMIN_ID], db_path=temp_db_path)

        await handlers.handle_character(make_update(STRANGER_ID, STRANGER_ID, "/character 角色乙"), make_context())

        async with get_db(temp_db_path) as conn:
            active = await crud.get_active_voice_profile(conn)
        assert active.id == alpha.id

    @pytest.mark.asyncio
    async def test_admin_command_still_runs(self, temp_db_path):
        handlers = TelegramBotHandlers(admin_ids=[ADMIN_ID], db_path=temp_db_path)

        reply = await handlers.handle_start(make_update(ADMIN_ID, ADMIN_ID, "/start"), make_context())

        assert "二次元AI伴侣" in reply


class TestCallbackAuthorization:
    @pytest.mark.parametrize(
        "data", ["menu_main", "menu_model", "menu_metrics", "menu_voice", "action_reset", "set_speed_1.2"]
    )
    @pytest.mark.asyncio
    async def test_non_admin_callback_is_answered_without_running(self, temp_db_path, data):
        handlers = TelegramBotHandlers(admin_ids=[ADMIN_ID], db_path=temp_db_path)
        update = make_callback_update(data, STRANGER_ID, STRANGER_ID)

        await handlers.handle_callback_query(update, make_context())

        assert update.callback_query.answers == [("⛔ 没有权限", True)]
        assert update.callback_query.edited_text is None

    @pytest.mark.asyncio
    async def test_admin_callback_still_renders_menu(self, temp_db_path):
        handlers = TelegramBotHandlers(admin_ids=[ADMIN_ID], db_path=temp_db_path)
        update = make_callback_update("menu_model", ADMIN_ID, ADMIN_ID)

        await handlers.handle_callback_query(update, make_context())

        assert update.callback_query.edited_text
