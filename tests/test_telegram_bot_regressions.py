"""
Regression tests for Telegram bot fixes: whitelist enforcement for every command
and button, persona switching with /character, per-member nickname and voice
task keys in group chats, and speech recognition provider support.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import galgame2voice.telegram_bot.handlers as handlers_module
from galgame2voice.adapters.registry import PROVIDER_PRESETS, SpeechRecognitionUnsupportedError, get_stt_adapter
from galgame2voice.database import crud
from galgame2voice.database.models import ProviderCreate, ProviderInDB, ProviderUpdate, VoiceProfileCreate
from galgame2voice.database.session import get_db
from galgame2voice.telegram_bot import TelegramBotHandlers

ADMIN_ID = 4242
OTHER_ADMIN_ID = 5353
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


class SilentTts:
    """TTS stand-in that returns no audio, so no ffmpeg conversion is attempted."""

    async def synthesize(self, text, options=None):
        return b""


class BlockingTts:
    """TTS stand-in whose synthesis waits until released, keeping voice tasks pending."""

    def __init__(self):
        self.release = asyncio.Event()

    async def synthesize(self, text, options=None):
        await self.release.wait()
        return b""


class CapturingAdapter:
    """LLM adapter stand-in that records the prompt of every call."""

    def __init__(self):
        self.calls: list[list] = []

    async def chat(self, messages, model=None, **kwargs):
        self.calls.append(messages)
        return SimpleNamespace(content='{"chinese": "好的", "japanese": "はい"}')


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


def system_prompt_of(messages) -> str:
    for msg in messages:
        role = msg.get("role") if isinstance(msg, dict) else getattr(msg, "role", None)
        if role == "system":
            return msg.get("content") if isinstance(msg, dict) else msg.content
    return ""


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


class TestCharacterSwitchPersona:
    @pytest.mark.asyncio
    async def test_chat_turn_follows_switched_character(self, temp_db_path):
        async with get_db(temp_db_path) as conn:
            alpha = await create_profile(conn, "角色甲", system_prompt="你是角色甲。PERSONA_ALPHA")
            beta = await create_profile(conn, "角色乙", system_prompt="你是角色乙。PERSONA_BETA")
            await crud.set_active_voice_profile(conn, alpha.id)

        handlers = TelegramBotHandlers(admin_ids=[ADMIN_ID], db_path=temp_db_path)
        adapter = CapturingAdapter()
        handlers.chat_service.get_active_llm_adapter = AsyncMock(return_value=(adapter, "mock-model", "mock"))
        handlers.tts_service = SilentTts()
        bot = RecordingBot()

        await (await handlers.process_text_chat(ADMIN_ID, "第一句", bot, user_id=ADMIN_ID))
        # The session row now remembers alpha; /character switches the global profile.
        async with get_db(temp_db_path) as conn:
            await crud.set_active_voice_profile(conn, beta.id)
        await (await handlers.process_text_chat(ADMIN_ID, "第二句", bot, user_id=ADMIN_ID))

        assert len(adapter.calls) == 2
        assert "PERSONA_ALPHA" in system_prompt_of(adapter.calls[0])
        switched = system_prompt_of(adapter.calls[1])
        assert "PERSONA_BETA" in switched
        assert "PERSONA_ALPHA" not in switched


class TestNicknameKey:
    @pytest.mark.asyncio
    async def test_group_nickname_is_saved_for_the_sender(self, temp_db_path):
        async with get_db(temp_db_path) as conn:
            profile = await create_profile(conn, "角色甲")
            await crud.set_active_voice_profile(conn, profile.id)
        handlers = TelegramBotHandlers(admin_ids=[ADMIN_ID], db_path=temp_db_path)

        reply = await handlers.handle_nickname(make_update(GROUP_CHAT_ID, ADMIN_ID, "/nickname 小夏"), make_context())
        assert "小夏" in reply

        async with get_db(temp_db_path) as conn:
            member = await crud.get_or_create_character_affection(conn, user_id=str(ADMIN_ID), character_id=profile.id)
            whole_chat = await crud.get_or_create_character_affection(
                conn, user_id=str(GROUP_CHAT_ID), character_id=profile.id
            )
        assert member.custom_nickname == "小夏"
        assert whole_chat.custom_nickname != "小夏"

        # The affection menu and chat turns read the same per-member key.
        text, _ = await handlers.build_affection_menu(GROUP_CHAT_ID, ADMIN_ID)
        assert "小夏" in text


async def settle(task: asyncio.Task) -> None:
    """Lets a cancelled task finish unwinding; the voice worker swallows CancelledError."""
    await asyncio.wait([task], timeout=2.0)


class TestPerMemberVoiceTasks:
    @pytest.mark.asyncio
    async def test_group_members_only_cancel_their_own_voice_note(self, temp_db_path):
        handlers = TelegramBotHandlers(admin_ids=[ADMIN_ID, OTHER_ADMIN_ID], db_path=temp_db_path)
        handlers.chat_service.get_active_llm_adapter = AsyncMock(
            return_value=(CapturingAdapter(), "mock-model", "mock")
        )
        tts = BlockingTts()
        handlers.tts_service = tts
        bot = RecordingBot()
        context = make_context(bot)

        try:
            task_a = await handlers.process_text_chat(GROUP_CHAT_ID, "我是成员A", bot, user_id=ADMIN_ID)
            task_b = await handlers.process_text_chat(GROUP_CHAT_ID, "我是成员B", bot, user_id=OTHER_ADMIN_ID)
            await asyncio.sleep(0.05)
            assert not task_a.done(), "member B's message cancelled member A's voice note"

            # B clears B's own context with the command and with the console button.
            await handlers.handle_reset(make_update(GROUP_CHAT_ID, OTHER_ADMIN_ID, "/reset"), context)
            await settle(task_b)
            assert task_b.done()
            await handlers.handle_callback_query(
                make_callback_update("action_reset", GROUP_CHAT_ID, OTHER_ADMIN_ID), context
            )
            await asyncio.sleep(0.05)
            assert not task_a.done(), "member B's reset cancelled member A's voice note"

            # A's own reset still cancels A's pending voice note.
            await handlers.handle_reset(make_update(GROUP_CHAT_ID, ADMIN_ID, "/reset"), context)
            await settle(task_a)
            assert task_a.done()
            assert bot.sent_voices == []
        finally:
            tts.release.set()

    @pytest.mark.asyncio
    async def test_cancel_user_task_accepts_a_bare_task_key(self):
        handlers = TelegramBotHandlers(admin_ids=[ADMIN_ID])
        task = asyncio.create_task(asyncio.sleep(10))
        handlers.user_tasks[777] = task

        handlers.cancel_user_task(777)

        assert 777 not in handlers.user_tasks
        await settle(task)
        assert task.cancelled()


FAKE_WAV = (
    b"RIFF\x24\x08\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00\x80>\x00\x00\x00}\x00\x00\x02\x00"
    b"\x10\x00data\x00\x08\x00\x00" + b"\x00" * 200
)


class VoiceBot(RecordingBot):
    """Serves a fake OGG file for every voice note."""

    async def get_file(self, file_id):
        return SimpleNamespace(download_as_bytearray=AsyncMock(return_value=bytearray(b"OggS fake voice")))


def make_voice_update(chat_id, user_id):
    return make_update(chat_id, user_id, voice=SimpleNamespace(file_id="voice-1", file_size=2048))


async def activate_provider(conn, provider_id, stt_model=""):
    await crud.get_settings_raw(conn)
    if await crud.get_provider_raw(conn, provider_id) is None:
        await crud.create_provider(conn, ProviderCreate(
            id=provider_id,
            name=provider_id,
            api_base_url=PROVIDER_PRESETS[provider_id]["default_base_url"],
            chat_model="chat-model",
        ))
    await crud.update_provider(conn, provider_id, ProviderUpdate(stt_model=stt_model))
    await crud.set_active_provider(conn, provider_id)


class TestVoiceNoteSpeechRecognition:
    @pytest.fixture(autouse=True)
    def fake_audio_conversion(self, monkeypatch):
        monkeypatch.setattr(handlers_module, "convert_ogg_to_wav", AsyncMock(return_value=FAKE_WAV))

    @pytest.mark.asyncio
    async def test_provider_without_speech_recognition_gets_a_clear_reply(self, temp_db_path):
        async with get_db(temp_db_path) as conn:
            await activate_provider(conn, "deepseek")
        handlers = TelegramBotHandlers(admin_ids=[ADMIN_ID], db_path=temp_db_path)
        handlers.process_text_chat = AsyncMock()
        update = make_voice_update(ADMIN_ID, ADMIN_ID)

        result = await handlers.handle_voice_message(update, make_context(VoiceBot()))

        assert result is None
        assert len(update.message.replies) == 1
        assert "不支持语音识别" in update.message.replies[0]
        handlers.process_text_chat.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_configured_stt_model_is_used_for_transcription(self, temp_db_path, monkeypatch):
        from galgame2voice.adapters.stt.openai_stt import OpenAICompatibleSTTAdapter

        used_models = []

        async def fake_transcribe(self, audio_bytes, filename="audio.wav", language=None, **kwargs):
            used_models.append(kwargs.get("model") or self.default_model)
            return "你好"

        monkeypatch.setattr(OpenAICompatibleSTTAdapter, "transcribe", fake_transcribe)
        async with get_db(temp_db_path) as conn:
            await activate_provider(conn, "custom", stt_model="whisper-large-v3")
        handlers = TelegramBotHandlers(admin_ids=[ADMIN_ID], db_path=temp_db_path)
        handlers.process_text_chat = AsyncMock(return_value=None)

        await handlers.handle_voice_message(make_voice_update(ADMIN_ID, ADMIN_ID), make_context(VoiceBot()))

        assert used_models == ["whisper-large-v3"]
        handlers.process_text_chat.assert_awaited_once()
        assert handlers.process_text_chat.await_args.args[1] == "你好"


class TestSpeechRecognitionFactory:
    def test_chat_only_preset_is_rejected(self):
        deepseek = ProviderInDB(
            id="deepseek", name="DeepSeek", api_base_url="https://api.deepseek.com", chat_model="deepseek-chat"
        )

        with pytest.raises(SpeechRecognitionUnsupportedError):
            get_stt_adapter(deepseek)

    def test_configured_stt_model_becomes_the_default_model(self):
        qwen = ProviderInDB(
            id="qwen", name="Qwen", api_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            chat_model="qwen-max", stt_model="qwen-audio-asr-latest",
        )
        # A chat-only preset is usable once the operator configures an STT model for it.
        glm = ProviderInDB(
            id="glm", name="GLM", api_base_url="https://open.bigmodel.cn/api/paas/v4",
            chat_model="glm-4-plus", stt_model="glm-asr",
        )

        assert get_stt_adapter(qwen).default_model == "qwen-audio-asr-latest"
        assert get_stt_adapter(glm).default_model == "glm-asr"
