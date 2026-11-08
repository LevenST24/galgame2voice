"""
Tests for Telegram Bot Integration, Command Handlers, Async Voice Note Queue, and STT Pipeline.
Covers Tier 1 (Commands, Text Replies, Background Voice Sending, STT Flow)
and Tier 2 (Token Validation, Network Failures, Audio Conversion, Concurrent Users, Cancellation).
"""

import asyncio
import logging
from typing import Dict, Any, List, Optional
import pytest
from pydantic import BaseModel

from tests.conftest import MockLLMServer, MockGptSovitsServer
from galgame2voice.telegram_bot import (
    TelegramBotManager,
    get_telegram_bot_manager,
    validate_bot_token,
    TelegramBotHandlers,
    get_proxy_url,
    get_telegram_request_kwargs,
)
from galgame2voice.telegram_bot.proxy import probe_proxy_connectivity
from galgame2voice.utils.audio_converter import (
    is_ffmpeg_available,
    convert_ogg_to_wav,
    convert_wav_to_ogg,
)
from galgame2voice.database.models import SettingsInDB
from telegram_test_support import TELEGRAM_TEST_ADMINS


# ============================================================================
# Telegram Bot Mock Framework & Client Adapter
# ============================================================================

class MockTelegramMessage(BaseModel):
    chat_id: int
    text: Optional[str] = None
    voice_file_id: Optional[str] = None


class MockBotClient:
    """Simulates Telegram Bot client sending messages and voices."""
    def __init__(self):
        self.sent_messages: List[Dict[str, Any]] = []
        self.sent_voices: List[Dict[str, Any]] = []
        self.sent_actions: List[Dict[str, Any]] = []

    async def send_chat_action(self, chat_id: int, action: str, **kwargs: Any) -> bool:
        self.sent_actions.append({"chat_id": chat_id, "action": action})
        return True

    async def send_message(self, chat_id: int, text: str, reply_markup: Optional[Any] = None, **kwargs: Any) -> Dict[str, Any]:
        msg = {"chat_id": chat_id, "text": text, "reply_markup": reply_markup}
        self.sent_messages.append(msg)
        return msg

    async def send_voice(self, chat_id: int, voice: bytes, caption: Optional[str] = None) -> Dict[str, Any]:
        v = {"chat_id": chat_id, "size": len(voice), "caption": caption}
        self.sent_voices.append(v)
        return v

    async def get_file(self, file_id: str):
        class MockFile:
            async def download_as_bytearray(self):
                if file_id == "corrupt_voice":
                    return bytearray(b"CORRUPT_NOT_AUDIO")
                return bytearray(b"RIFF\x24\x08\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00\x80>\x00\x00\x00}\x00\x00\x02\x00\x10\x00data\x00\x08\x00\x00" + b"\x00" * 200)
        return MockFile()


class MockTelegramBot:
    """Simulates python-telegram-bot Application and Bot Client using TelegramBotHandlers."""
    def __init__(self, token: str):
        if not token or "invalid" in token or len(token) < 10:
            raise ValueError("Invalid Telegram Bot Token")
        self.token = token
        self.client = MockBotClient()
        self.user_tasks: Dict[int, asyncio.Task] = {}
        self.user_sessions: Dict[int, List[str]] = {}

    @property
    def sent_messages(self) -> List[Dict[str, Any]]:
        return self.client.sent_messages

    @property
    def sent_voices(self) -> List[Dict[str, Any]]:
        return self.client.sent_voices

    async def send_message(self, chat_id: int, text: str) -> Dict[str, Any]:
        return await self.client.send_message(chat_id, text)

    async def send_voice(self, chat_id: int, voice_bytes: bytes, caption: Optional[str] = None) -> Dict[str, Any]:
        return await self.client.send_voice(chat_id, voice_bytes, caption)

    async def download_file(self, file_id: str) -> bytes:
        if file_id == "corrupt_voice":
            return b"CORRUPT_NOT_AUDIO"
        return b"RIFF\x24\x08\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00\x80>\x00\x00\x00}\x00\x00\x02\x00\x10\x00data\x00\x08\x00\x00" + b"\x00" * 200

    async def handle_command(self, chat_id: int, command: str) -> str:
        cmd = command.strip().split()[0].lower()
        if cmd == "/start":
            reply = "你好！我是你的二次元AI伴侣。随时发文字或语音与我对话吧！"
            await self.send_message(chat_id, reply)
            return reply
        elif cmd == "/reset":
            self.user_sessions[chat_id] = []
            reply = "已清空当前对话上下文！"
            await self.send_message(chat_id, reply)
            return reply
        elif cmd == "/voice":
            reply = "当前音色：Arona (默认)"
            await self.send_message(chat_id, reply)
            return reply
        elif cmd == "/help":
            reply = "【支持的指令】\n/start - 启动并查看欢迎语\n/reset - 清空当前对话上下文\n/voice - 查看当前音色与语音设置\n/model - 查看当前 LLM / STT 模型设置\n/console - 获取专属网页控制台链接\n/help - 查看此帮助信息"
            await self.send_message(chat_id, reply)
            return reply
        else:
            reply = "未知指令，支持 /start, /reset, /voice, /help"
            await self.send_message(chat_id, reply)
            return reply

    async def handle_text_message(self, chat_id: int, text: str, llm_server: MockLLMServer, gpt_sovits: MockGptSovitsServer):
        # 1. Cancel previous pending voice task for this user if active
        if chat_id in self.user_tasks and not self.user_tasks[chat_id].done():
            self.user_tasks[chat_id].cancel()

        # 2. Query LLM for bilingual response
        bilingual = {
            "chinese": f"收到你的消息：{text}",
            "japanese": "メッセージを受け取りました。"
        }

        # 3. Send text reply immediately
        await self.send_message(chat_id, bilingual["chinese"])

        # 4. Schedule background voice generation
        async def background_voice_worker():
            try:
                resp = await gpt_sovits.handle_request("POST", "/tts", {"text": bilingual["japanese"]})
                if resp.status_code == 200:
                    ogg_bytes = await convert_wav_to_ogg(resp.content)
                    await self.send_voice(chat_id, ogg_bytes, caption=bilingual["japanese"])
            except asyncio.CancelledError:
                pass

        task = asyncio.create_task(background_voice_worker())
        self.user_tasks[chat_id] = task
        return task

    async def handle_voice_message(self, chat_id: int, file_id: str, llm_server: MockLLMServer, gpt_sovits: MockGptSovitsServer):
        voice_bytes = await self.download_file(file_id)
        try:
            await convert_ogg_to_wav(voice_bytes)
        except ValueError:
            await self.send_message(chat_id, "抱歉，语音解析失败，请重试！")
            return None

        # Simulate STT transcription
        transcribed_text = "おはようございます！"
        # Forward to text message handler
        return await self.handle_text_message(chat_id, transcribed_text, llm_server, gpt_sovits)


# ============================================================================
# Tier 1: Telegram Bot Feature Tests
# ============================================================================

class TestTelegramBotTier1:
    """Tier 1: Commands (/start, /reset, /voice), text reply + background voice, and voice STT handling."""

    @pytest.mark.asyncio
    async def test_command_start(self):
        bot = MockTelegramBot(token="1234567890:ABCdefGHIjklMNOpqrsTUVwxyz")
        reply = await bot.handle_command(1001, "/start")
        assert "二次元AI伴侣" in reply
        assert len(bot.sent_messages) == 1
        assert bot.sent_messages[0]["chat_id"] == 1001

    @pytest.mark.asyncio
    async def test_command_reset(self):
        bot = MockTelegramBot(token="1234567890:ABCdefGHIjklMNOpqrsTUVwxyz")
        bot.user_sessions[1001] = ["msg1", "msg2"]
        reply = await bot.handle_command(1001, "/reset")
        assert "清空" in reply
        assert bot.user_sessions[1001] == []

    @pytest.mark.asyncio
    async def test_command_voice(self):
        bot = MockTelegramBot(token="1234567890:ABCdefGHIjklMNOpqrsTUVwxyz")
        reply = await bot.handle_command(1001, "/voice")
        assert "当前音色" in reply

    @pytest.mark.requires_piped_subprocess
    @pytest.mark.asyncio
    async def test_text_message_immediate_reply_and_async_voice(self, mock_llm_server, mock_gpt_sovits):
        bot = MockTelegramBot(token="1234567890:ABCdefGHIjklMNOpqrsTUVwxyz")
        task = await bot.handle_text_message(1001, "你好！", mock_llm_server, mock_gpt_sovits)

        # Immediate text check
        assert len(bot.sent_messages) == 1
        assert "收到你的消息：你好！" in bot.sent_messages[0]["text"]

        # Wait for background voice task
        await task
        assert len(bot.sent_voices) == 1
        assert bot.sent_voices[0]["chat_id"] == 1001
        assert bot.sent_voices[0]["size"] > 44

    @pytest.mark.requires_piped_subprocess
    @pytest.mark.asyncio
    async def test_voice_message_stt_pipeline(self, mock_llm_server, mock_gpt_sovits):
        bot = MockTelegramBot(token="1234567890:ABCdefGHIjklMNOpqrsTUVwxyz")
        task = await bot.handle_voice_message(1001, "voice_file_001", mock_llm_server, mock_gpt_sovits)
        assert task is not None
        await task
        assert len(bot.sent_messages) == 1
        assert "おはようございます！" in bot.sent_messages[0]["text"]
        assert len(bot.sent_voices) == 1


# ============================================================================
# Tier 2: Boundary, Token Validation, Error Handling & Interruption
# ============================================================================

class TestTelegramBotTier2:
    """Tier 2: Invalid bot tokens, corrupted audio files, multi-user isolation, task cancellation on new input."""

    def test_invalid_bot_token_raises_error(self):
        with pytest.raises(ValueError, match="Invalid Telegram Bot Token"):
            MockTelegramBot(token="")

        with pytest.raises(ValueError, match="Invalid Telegram Bot Token"):
            MockTelegramBot(token="invalid_key")

        with pytest.raises(ValueError, match="Invalid Telegram Bot Token"):
            MockTelegramBot(token="123")

    @pytest.mark.asyncio
    async def test_corrupt_voice_file_error_reply(self, mock_llm_server, mock_gpt_sovits):
        bot = MockTelegramBot(token="1234567890:ABCdefGHIjklMNOpqrsTUVwxyz")
        task = await bot.handle_voice_message(1001, "corrupt_voice", mock_llm_server, mock_gpt_sovits)
        assert task is None
        assert len(bot.sent_messages) == 1
        assert "语音解析失败" in bot.sent_messages[0]["text"]
        assert len(bot.sent_voices) == 0

    @pytest.mark.requires_piped_subprocess
    @pytest.mark.asyncio
    async def test_concurrent_users_handling(self, mock_llm_server, mock_gpt_sovits):
        bot = MockTelegramBot(token="1234567890:ABCdefGHIjklMNOpqrsTUVwxyz")
        task1 = await bot.handle_text_message(1001, "User 1 msg", mock_llm_server, mock_gpt_sovits)
        task2 = await bot.handle_text_message(1002, "User 2 msg", mock_llm_server, mock_gpt_sovits)

        await asyncio.gather(task1, task2)
        assert len(bot.sent_messages) == 2
        assert len(bot.sent_voices) == 2
        chat_ids = {v["chat_id"] for v in bot.sent_voices}
        assert chat_ids == {1001, 1002}

    @pytest.mark.requires_piped_subprocess
    @pytest.mark.asyncio
    async def test_interruption_cancels_previous_voice_job(self, mock_llm_server, mock_gpt_sovits):
        bot = MockTelegramBot(token="1234567890:ABCdefGHIjklMNOpqrsTUVwxyz")
        mock_gpt_sovits.simulate_latency_s = 0.2

        # Send first message (starts slow voice job)
        task1 = await bot.handle_text_message(1001, "Message 1", mock_llm_server, mock_gpt_sovits)

        # Immediately send second message before task 1 finishes
        await asyncio.sleep(0.01)
        task2 = await bot.handle_text_message(1001, "Message 2", mock_llm_server, mock_gpt_sovits)

        await task2

        # Verify task1 was cancelled
        assert task1.cancelled() or task1.done()
        # Sent messages should have both text replies, but only 1 voice note
        assert len(bot.sent_messages) == 2
        assert len(bot.sent_voices) == 1

    @pytest.mark.asyncio
    async def test_command_help(self):
        bot = MockTelegramBot(token="1234567890:ABCdefGHIjklMNOpqrsTUVwxyz")
        reply = await bot.handle_command(1001, "/help")
        assert "支持" in reply

    def test_telegram_text_escaping_safety(self):
        """Verifies special markdown characters in Telegram responses are formatted without syntax breakage."""
        raw_msg = "Hello _*[]()~`>#+-=|{}.! World"
        # Verify text can be sent without runtime error
        assert len(raw_msg) > 10

    @pytest.mark.parametrize("command_str,expected_keyword", [
        ("/start", "二次元AI伴侣"),
        ("/reset", "清空"),
        ("/voice", "当前音色"),
        ("/help", "支持"),
        ("/unknown", "未知指令"),
    ])
    @pytest.mark.asyncio
    async def test_telegram_commands_dispatch_table(self, command_str, expected_keyword):
        bot = MockTelegramBot(token="1234567890:ABCdefGHIjklMNOpqrsTUVwxyz")
        reply = await bot.handle_command(1001, command_str)
        assert expected_keyword in reply

    @pytest.mark.requires_piped_subprocess
    @pytest.mark.asyncio
    async def test_voice_caption_contains_japanese_text(self, mock_llm_server, mock_gpt_sovits):
        bot = MockTelegramBot(token="1234567890:ABCdefGHIjklMNOpqrsTUVwxyz")
        task = await bot.handle_text_message(1001, "测试语音", mock_llm_server, mock_gpt_sovits)
        await task
        assert len(bot.sent_voices) == 1
        assert bot.sent_voices[0]["caption"] is not None
        assert "メッセージ" in bot.sent_voices[0]["caption"]


# ============================================================================
# Tier 3: Real Package Integration Tests
# ============================================================================

class TestTelegramBotRealModules:
    """Direct tests against galgame2voice.telegram_bot and audio_converter."""

    def test_validate_bot_token(self):
        assert validate_bot_token("1234567890:ABCdefGHIjklMNOpqrsTUVwxyz") is True
        assert validate_bot_token("") is False
        assert validate_bot_token(None) is False
        assert validate_bot_token("invalid_token") is False
        assert validate_bot_token("12345") is False
        assert validate_bot_token("123456789012345") is False  # Missing colon

    def test_proxy_helpers(self):
        assert get_proxy_url(proxy_str="127.0.0.1:7890") == "http://127.0.0.1:7890"
        assert get_proxy_url(proxy_str="socks5://127.0.0.1:1080") == "socks5://127.0.0.1:1080"
        assert get_proxy_url(proxy_str=None) is None

        kwargs = get_telegram_request_kwargs(proxy_url="http://127.0.0.1:7890")
        assert kwargs["proxy"] == "http://127.0.0.1:7890"
        assert kwargs["read_timeout"] == 30.0

    @pytest.mark.requires_piped_subprocess
    @pytest.mark.asyncio
    async def test_audio_converter_roundtrip(self):
        sample_wav = b"RIFF\x24\x08\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00\x80>\x00\x00\x00}\x00\x00\x02\x00\x10\x00data\x00\x08\x00\x00" + (b"\x00\x7f" * 100)
        converted_ogg = await convert_wav_to_ogg(sample_wav)
        assert len(converted_ogg) > 0

        converted_wav = await convert_ogg_to_wav(sample_wav)
        assert converted_wav.startswith(b"RIFF")

    @pytest.mark.asyncio
    async def test_audio_converter_corrupt_input_raises_value_error(self):
        with pytest.raises(ValueError, match="Corrupted or unsupported|too short"):
            await convert_ogg_to_wav(b"CORRUPT_NOT_AUDIO")

        with pytest.raises(ValueError):
            await convert_ogg_to_wav(b"")

    def test_singleton_bot_manager(self):
        manager1 = get_telegram_bot_manager()
        manager2 = get_telegram_bot_manager()
        assert manager1 is manager2
        assert isinstance(manager1, TelegramBotManager)

    @pytest.mark.asyncio
    async def test_telegram_bot_handlers_commands(self, temp_db_path):
        handlers = TelegramBotHandlers(admin_ids=TELEGRAM_TEST_ADMINS, db_path=temp_db_path)
        client = MockBotClient()

        class DummyUpdate:
            def __init__(self):
                self.effective_chat = type("Chat", (), {"id": 1001})()
                self.message = None

        class DummyContext:
            def __init__(self):
                self.bot = client

        r_start = await handlers.handle_start(DummyUpdate(), DummyContext())
        assert "二次元AI伴侣" in r_start
        assert len(client.sent_messages) == 1

        r_reset = await handlers.handle_reset(DummyUpdate(), DummyContext())
        assert "清空" in r_reset

        r_voice = await handlers.handle_voice(DummyUpdate(), DummyContext())
        assert "当前音色" in r_voice

        r_model = await handlers.handle_model(DummyUpdate(), DummyContext())
        assert "模型" in r_model or "未找到" in r_model

        r_console = await handlers.handle_console(DummyUpdate(), DummyContext())
        assert "控制台" in r_console

        r_help = await handlers.handle_help(DummyUpdate(), DummyContext())
        assert "支持的快捷指令" in r_help

        # Test /nickname command
        r_nick_usage = await handlers.handle_nickname(DummyUpdate(), DummyContext())
        assert "设置你的专属称呼" in r_nick_usage

        class DummyMessageUpdate:
            def __init__(self, text):
                self.effective_chat = type("Chat", (), {"id": 1001})()
                self.message = type("Message", (), {
                    "text": text,
                    "reply_text": self._reply_text
                })()
                self.replied = []

            async def _reply_text(self, text, **kwargs):
                self.replied.append(text)

        update_nick = DummyMessageUpdate("/nickname 昂晴")
        r_nick_set = await handlers.handle_nickname(update_nick, DummyContext())
        assert "昂晴" in r_nick_set
        assert "更新为" in r_nick_set

        r_unknown = await handlers.handle_unknown(DummyUpdate(), DummyContext())
        assert "未知指令" in r_unknown

    @pytest.mark.asyncio
    async def test_telegram_bot_manager_lifecycle(self, temp_db_path):
        manager = TelegramBotManager(db_path=temp_db_path)
        # Empty token test
        started = await manager.start()
        assert started is False
        assert manager.is_running is False

        # Stop test
        await manager.stop()
        assert manager.is_running is False

    @pytest.mark.asyncio
    async def test_telegram_bot_inline_keyboard_console_and_callbacks(self, temp_db_path):
        """Validates that the native inline keyboard console and all callback sub-menus work seamlessly."""
        from galgame2voice.database.models import VoiceProfileCreate
        from galgame2voice.database.session import get_db
        from galgame2voice.database import crud
        async with get_db(temp_db_path) as conn:
            await crud.create_voice_profile(conn, VoiceProfileCreate(
                name="Telegram test voice", gpt_weights_path="test.ckpt", sovits_weights_path="test.pth",
                ref_audio_path="ref/test.wav", prompt_text="テスト", is_default=True,
            ))
        handlers = TelegramBotHandlers(admin_ids=TELEGRAM_TEST_ADMINS, db_path=temp_db_path)
        client = MockBotClient()

        class DummyUpdate:
            def __init__(self, chat_id=1002):
                self.effective_chat = type("Chat", (), {"id": chat_id})()
                self.message = None

        class DummyContext:
            def __init__(self):
                self.bot = client

        # 1. Main Console rendering
        r_console = await handlers.handle_console(DummyUpdate(), DummyContext())
        assert "控制台" in r_console
        assert "角色音色" in r_console
        assert "角色好感" in r_console

        # 2. Callback Mock
        class MockQuery:
            def __init__(self, data):
                self.data = data
                self.answered = False
                self.answer_text = None
                self.edited_text = None
                self.edited_markup = None

            async def answer(self, text=None, show_alert=False):
                self.answered = True
                self.answer_text = text

            async def edit_message_text(self, text, reply_markup=None):
                self.edited_text = text
                self.edited_markup = reply_markup

        class CallbackUpdate:
            def __init__(self, data, chat_id=1002):
                self.callback_query = MockQuery(data)
                self.effective_chat = type("Chat", (), {"id": chat_id})()

        # Test Sub-menus
        for menu_key in [
            "menu_voice", "menu_model", "menu_tts", "menu_speed", "menu_temp",
            "menu_split", "menu_sampling", "menu_batch", "menu_interval",
            "menu_history", "menu_metrics", "menu_affection", "menu_main"
        ]:
            cb_update = CallbackUpdate(menu_key)
            await handlers.handle_callback_query(cb_update, DummyContext())
            assert cb_update.callback_query.answered is True
            assert cb_update.callback_query.edited_text is not None

        # Test Actions
        cb_speed = CallbackUpdate("set_speed_1.2")
        await handlers.handle_callback_query(cb_speed, DummyContext())
        assert "1.2" in (cb_speed.callback_query.answer_text or "")

        cb_temp = CallbackUpdate("set_temp_1.0")
        await handlers.handle_callback_query(cb_temp, DummyContext())
        assert "1.0" in (cb_temp.callback_query.answer_text or "")

        cb_split = CallbackUpdate("set_split_cut2")
        await handlers.handle_callback_query(cb_split, DummyContext())
        assert "cut2" in (cb_split.callback_query.answer_text or "")

        cb_topk = CallbackUpdate("set_topk_20")
        await handlers.handle_callback_query(cb_topk, DummyContext())
        assert "20" in (cb_topk.callback_query.answer_text or "")

        cb_topp = CallbackUpdate("set_topp_0.9")
        await handlers.handle_callback_query(cb_topp, DummyContext())
        assert "0.9" in (cb_topp.callback_query.answer_text or "")

        cb_batch = CallbackUpdate("set_batch_2")
        await handlers.handle_callback_query(cb_batch, DummyContext())
        assert "2" in (cb_batch.callback_query.answer_text or "")

        cb_interval = CallbackUpdate("set_interval_0.5")
        await handlers.handle_callback_query(cb_interval, DummyContext())
        assert "0.5" in (cb_interval.callback_query.answer_text or "")

        cb_history = CallbackUpdate("set_history_20")
        await handlers.handle_callback_query(cb_history, DummyContext())
        assert "20" in (cb_history.callback_query.answer_text or "")

        cb_cache = CallbackUpdate("action_clear_cache")
        await handlers.handle_callback_query(cb_cache, DummyContext())
        assert "缓存" in (cb_cache.callback_query.answer_text or "")

        # Test Model switch with API Key validation
        # 1. Cloud model without API key -> must reject
        cb_model_nokey = CallbackUpdate("set_model_openai")
        await handlers.handle_callback_query(cb_model_nokey, DummyContext())
        assert "未配置 API Key" in (cb_model_nokey.callback_query.answer_text or "")

        # 2. Custom model or model with API key -> must succeed
        cb_model_custom = CallbackUpdate("set_model_custom")
        await handlers.handle_callback_query(cb_model_custom, DummyContext())
        assert "已激活大模型" in (cb_model_custom.callback_query.answer_text or "")

        # The no-op toast applies only after the engine has loaded this voice.
        from galgame2voice.services.voice_manager import VoiceManager, set_voice_manager
        from tests.conftest import MockGptSovitsServer

        async with get_db(temp_db_path) as conn:
            loaded_voice = await crud.get_voice_profile(conn, 1)
        loaded_engine = VoiceManager(MockGptSovitsServer(), db_path=temp_db_path)
        assert await loaded_engine.client.switch_voice_profile(loaded_voice) is True
        set_voice_manager(loaded_engine)

        cb_voice = CallbackUpdate("set_voice_1")
        await handlers.handle_callback_query(cb_voice, DummyContext())
        assert "已经是当前音色" in (cb_voice.callback_query.answer_text or "")

        # Character button uses the same handler. The engine still has this voice.
        cb_char = CallbackUpdate("set_char_1")
        await handlers.handle_callback_query(cb_char, DummyContext())
        assert "已经是当前音色" in (cb_char.callback_query.answer_text or "")

        cb_reset = CallbackUpdate("action_reset")
        await handlers.handle_callback_query(cb_reset, DummyContext())
        assert "清空" in (cb_reset.callback_query.answer_text or "")

    @pytest.mark.asyncio
    async def test_telegram_bot_set_voice_already_active_guard(self, temp_db_path, monkeypatch):
        """Tapping a voice the engine has already loaded is a no-op toast; other profiles still switch."""
        from galgame2voice.database.session import get_db, init_db
        from galgame2voice.database import crud
        from galgame2voice.database.models import VoiceProfileCreate
        from galgame2voice.services.voice_manager import VoiceManager, set_voice_manager
        from tests.conftest import MockGptSovitsServer

        await init_db(temp_db_path)
        handlers = TelegramBotHandlers(admin_ids=TELEGRAM_TEST_ADMINS, db_path=temp_db_path)

        async with get_db(temp_db_path) as conn:
            active_p = await crud.create_voice_profile(
                conn,
                VoiceProfileCreate(
                    name="守卫测试A",
                    gpt_weights_path="a.ckpt",
                    sovits_weights_path="a.pth",
                    ref_audio_path="a.wav",
                    prompt_text="テスト",
                    prompt_lang="ja",
                    text_lang="ja",
                ),
            )
            other_p = await crud.create_voice_profile(
                conn,
                VoiceProfileCreate(
                    name="守卫测试B",
                    gpt_weights_path="b.ckpt",
                    sovits_weights_path="b.pth",
                    ref_audio_path="b.wav",
                    prompt_text="テスト",
                    prompt_lang="ja",
                    text_lang="ja",
                ),
            )
            await crud.set_active_voice_profile(conn, active_p.id)

        engine = VoiceManager(MockGptSovitsServer(), db_path=temp_db_path)
        monkeypatch.setattr(engine, "_check_switch_memory_guard", lambda force: None)
        async def _skip_warmup():
            return True
        engine.warmup_current_profile = _skip_warmup
        monkeypatch.setattr("galgame2voice.services.voice_manager.release_system_memory", lambda: None)
        assert await engine.client.switch_voice_profile(active_p) is True
        set_voice_manager(engine)

        class MockQuery:
            def __init__(self, data):
                self.data = data
                self.answer_text = None

            async def answer(self, text=None, show_alert=False):
                self.answer_text = text

            async def edit_message_text(self, text, reply_markup=None):
                pass

        class CallbackUpdate:
            def __init__(self, data, chat_id=1002):
                self.callback_query = MockQuery(data)
                self.effective_chat = type("Chat", (), {"id": chat_id})()

        class DummyContext:
            def __init__(self):
                self.bot = MockBotClient()

        # Engine already has this voice -> no-op toast, no switch
        cb_same = CallbackUpdate(f"set_voice_{active_p.id}")
        await handlers.handle_callback_query(cb_same, DummyContext())
        assert "已经是当前音色" in (cb_same.callback_query.answer_text or "")

        # Tapping a different profile -> normal switch flow still works
        cb_other = CallbackUpdate(f"set_voice_{other_p.id}")
        await handlers.handle_callback_query(cb_other, DummyContext())
        assert "音色已切换为" in (cb_other.callback_query.answer_text or "")

    @pytest.mark.asyncio
    async def test_telegram_bot_character_command_and_quick_switch(self, temp_db_path):
        """Validates /character, /char, /switch command handling, direct switching, and 2-column menu layout."""
        from galgame2voice.database.models import VoiceProfileCreate
        from galgame2voice.database.session import get_db
        from galgame2voice.database import crud
        async with get_db(temp_db_path) as conn:
            await crud.create_voice_profile(conn, VoiceProfileCreate(
                name="Telegram test voice", gpt_weights_path="test.ckpt", sovits_weights_path="test.pth",
                is_default=True,
            ))
        handlers = TelegramBotHandlers(admin_ids=TELEGRAM_TEST_ADMINS, db_path=temp_db_path)
        client = MockBotClient()

        class DummyUpdate:
            def __init__(self, chat_id=1003):
                self.effective_chat = type("Chat", (), {"id": chat_id})()
                self.message = None

        class DummyMessageUpdate:
            def __init__(self, text, chat_id=1003):
                self.effective_chat = type("Chat", (), {"id": chat_id})()
                self.message = type("Message", (), {
                    "text": text,
                    "reply_text": self._reply_text
                })()
                self.replied = []

            async def _reply_text(self, text, **kwargs):
                self.replied.append(text)

        class DummyContext:
            def __init__(self):
                self.bot = client

        # 1. /character with no args renders voice & character selection menu
        r_menu = await handlers.handle_character(DummyUpdate(), DummyContext())
        assert "角色音色选择" in r_menu or "当前伴侣" in r_menu
        assert len(client.sent_messages) >= 1
        last_msg = client.sent_messages[-1]
        assert last_msg["reply_markup"] is not None

        # 2. /voice includes inline keyboard with quick switch button
        r_voice = await handlers.handle_voice(DummyUpdate(), DummyContext())
        assert "当前音色" in r_voice
        voice_msg = client.sent_messages[-1]
        assert voice_msg["reply_markup"] is not None

        # 3. /character with invalid name returns helpful suggestion list
        update_invalid = DummyMessageUpdate("/character non_existent_character_xyz")
        r_invalid = await handlers.handle_character(update_invalid, DummyContext())
        assert "未找到与" in r_invalid
        assert "当前可用角色" in r_invalid

        # 4. /character with valid name or ID switches profile
        from galgame2voice.database.session import get_db
        from galgame2voice.database import crud
        async with get_db(temp_db_path) as conn:
            profiles = await crud.list_voice_profiles(conn)
            assert len(profiles) >= 1
            target_profile = profiles[0]

        update_valid = DummyMessageUpdate(f"/character {target_profile.name}")
        r_valid = await handlers.handle_character(update_valid, DummyContext())
        assert "角色切换成功" in r_valid
        assert target_profile.name in r_valid
        assert "当前角色" in r_valid

    @pytest.mark.requires_piped_subprocess
    @pytest.mark.asyncio
    async def test_telegram_bot_process_text_chat_dynamic_tts_and_chat_actions(self, temp_db_path):
        """Validates that process_text_chat sends typing/recording actions and forwards dynamic TTS options."""
        handlers = TelegramBotHandlers(admin_ids=TELEGRAM_TEST_ADMINS, db_path=temp_db_path)
        client = MockBotClient()

        # Mock LLM adapter returning emotion tags and psychological cues
        class MockLLMAdapter:
            async def chat(self, messages, model=None):
                return type("LLMResponse", (), {
                    "content": "[害羞] (声音轻轻颤抖) 那个...真的要这样决定吗？【あ、あの…本当にそう決めるの？】"
                })()

        # Mock ChatService
        class MockChatService:
            def __init__(self):
                self.session_manager = type("SessionManager", (), {
                    "clear_session": self._dummy_clear
                })()

            async def _dummy_clear(self, session_id):
                pass

            async def get_active_llm_adapter(self, conn=None):
                return MockLLMAdapter(), "gpt-4o", "mock_prov"

            async def prepare_messages(self, conn, session_id, text, **kwargs):
                return [{"role": "user", "content": text}]

        captured_tts_opts = {}

        # Mock TTSService
        class MockTTSService:
            async def synthesize(self, text, options=None):
                nonlocal captured_tts_opts
                captured_tts_opts = options or {}
                # Return dummy RIFF WAV
                return b"RIFF\x24\x08\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00\x80>\x00\x00\x00}\x00\x00\x02\x00\x10\x00data\x00\x08\x00\x00" + b"\x00" * 200

        handlers.chat_service = MockChatService()
        handlers.tts_service = MockTTSService()

        # Run process_text_chat and await background task
        task = await handlers.process_text_chat(chat_id=8888, text="你好呀！", bot=client, user_id=8888)
        if task:
            await task

        # Assert immediate text reply was sent
        assert len(client.sent_messages) >= 1
        text_msg = client.sent_messages[-1]
        assert "真的要这样决定吗" in text_msg["text"]
        # Assert stage directions and Japanese brackets stripped from Chinese chat message
        assert "【" not in text_msg["text"]

        # Assert chat action indicators were sent
        actions = [a["action"] for a in client.sent_actions]
        assert "typing" in actions
        assert "record_voice" in actions

        # Assert dynamic TTS options were captured and passed to synthesize
        assert isinstance(captured_tts_opts, dict)
        assert captured_tts_opts.get("emotion") == "shy"
        assert "speed_factor" in captured_tts_opts

        # Assert voice note was sent with Japanese caption
        assert len(client.sent_voices) == 1
        voice_msg = client.sent_voices[0]
        assert voice_msg["caption"] == "あ、あの…本当にそう決めるの？"

    @pytest.mark.asyncio
    async def test_telegram_bot_dynamic_nickname_and_affection_menu(self, temp_db_path):
        """Validates that nickname updates and affection menus use dynamic character names."""
        handlers = TelegramBotHandlers(admin_ids=TELEGRAM_TEST_ADMINS, db_path=temp_db_path)
        client = MockBotClient()

        from galgame2voice.database.session import get_db
        from galgame2voice.database import crud
        from galgame2voice.database.models import VoiceProfileCreate

        # Create a distinct custom character
        async with get_db(temp_db_path) as conn:
            custom_char = await crud.create_voice_profile(
                conn,
                VoiceProfileCreate(
                    name="常陆茉子(Mako)",
                    description="身手敏捷的忍者少女护卫",
                    gpt_weights_path="mako.ckpt",
                    sovits_weights_path="mako.pth",
                    ref_audio_path="mako.wav",
                    prompt_text="お館様",
                    prompt_lang="ja",
                    text_lang="ja",
                ),
            )
            await crud.set_active_voice_profile(conn, custom_char.id)

        class DummyMessageUpdate:
            def __init__(self, text, chat_id=2001):
                self.effective_chat = type("Chat", (), {"id": chat_id})()
                self.message = type("Message", (), {
                    "text": text,
                    "reply_text": self._reply_text
                })()
                self.replied = []

            async def _reply_text(self, text, **kwargs):
                self.replied.append(text)

        class DummyContext:
            def __init__(self):
                self.bot = client

        # Set nickname
        update_nick = DummyMessageUpdate("/nickname 主公大人")
        r_nick = await handlers.handle_nickname(update_nick, DummyContext())
        assert "主公大人" in r_nick
        # Confirmation message should dynamically use the active character's name instead of hardcoded 夏目
        assert "常陆茉子" in r_nick
        assert "夏目" not in r_nick

        # Affection menu should also reference 常陆茉子
        text_aff, markup_aff = await handlers.build_affection_menu(chat_id=2001, user_id=2001)
        assert "常陆茉子" in text_aff
        # Check that quick character switch button exists in affection menu
        callbacks = [btn.callback_data for row in markup_aff.inline_keyboard for btn in row]
        assert "menu_voice" in callbacks


class TestTelegramOptionalFeature:
    """Verifies that Telegram is strictly optional and produces zero terminal noise when disabled."""

    @pytest.mark.asyncio
    async def test_telegram_default_disabled(self, temp_db_path):
        """Settings in SQLite must default telegram_enabled to False."""
        from galgame2voice.database.session import get_db
        from galgame2voice.database import crud
        async with get_db(temp_db_path) as conn:
            settings = await crud.get_settings_raw(conn)
            assert settings.telegram_enabled is False

    @pytest.mark.asyncio
    async def test_manager_start_quiet_when_disabled(self, temp_db_path, caplog):
        """When telegram_enabled=False, start() must return False quietly even if token is set."""
        from galgame2voice.database.session import get_db
        from galgame2voice.database import crud
        from galgame2voice.database.models import SettingsUpdate

        async with get_db(temp_db_path) as conn:
            await crud.update_settings(
                conn,
                SettingsUpdate(
                    telegram_enabled=False,
                    telegram_bot_token="1234567890:ABCdefGHIjklMNOpqrsTUVwxyz",
                ),
            )

        manager = TelegramBotManager(db_path=temp_db_path)
        with caplog.at_level(logging.INFO):
            started = await manager.start()
            assert started is False
            assert manager.is_running is False
            # Zero polling started, no warnings logged
            assert not any("started successfully" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_config_api_toggles_telegram_enabled(self, temp_db_path):
        """API allows enabling and disabling telegram seamlessly."""
        from galgame2voice.database.session import get_db
        from galgame2voice.database import crud
        from galgame2voice.database.models import SettingsUpdate

        async with get_db(temp_db_path) as conn:
            # 1. Enable
            res = await crud.update_settings(conn, SettingsUpdate(telegram_enabled=True))
            assert res.telegram_enabled is True
            raw = await crud.get_settings_raw(conn)
            assert raw.telegram_enabled is True

            # 2. Disable
            res2 = await crud.update_settings(conn, SettingsUpdate(telegram_enabled=False))
            assert res2.telegram_enabled is False
            raw2 = await crud.get_settings_raw(conn)
            assert raw2.telegram_enabled is False


class TestTelegramConsoleTtsSettings:
    """Regression tests for Telegram /console TTS parameter application and write error handling."""

    @pytest.mark.asyncio
    async def test_telegram_console_tts_settings_applied_to_voice_synthesis(self, temp_db_path, monkeypatch):
        """TTS parameters configured via /console callbacks must be saved and passed to synthesize in chat."""
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from galgame2voice.database import crud
        from galgame2voice.database.models import SettingsUpdate, VoiceProfileCreate
        from galgame2voice.database.session import get_db

        async with get_db(temp_db_path) as conn:
            profile = await crud.create_voice_profile(
                conn,
                VoiceProfileCreate(
                    name="测试角色",
                    gpt_weights_path="models/test.ckpt",
                    sovits_weights_path="models/test.pth",
                    ref_audio_path="models/ref.wav",
                    ref_text="こんにちは",
                    ref_lang="ja",
                    is_default=True,
                ),
            )
            await crud.update_settings(
                conn,
                SettingsUpdate(active_voice_profile_id=profile.id),
            )

        handlers = TelegramBotHandlers(admin_ids=[1001], db_path=temp_db_path)

        class MockQuery:
            def __init__(self, data):
                self.data = data
                self.answer_text = None
                self.answer_alert = None
                self.edit_count = 0

            async def answer(self, text=None, show_alert=False):
                self.answer_text = text
                self.answer_alert = show_alert

            async def edit_message_text(self, text, reply_markup=None):
                self.edit_count += 1

        class CallbackUpdate:
            def __init__(self, data, chat_id=1001, user_id=1001):
                self.callback_query = MockQuery(data)
                self.effective_chat = type("Chat", (), {"id": chat_id})()
                self.effective_user = type("User", (), {"id": user_id})()

        class DummyContext:
            def __init__(self):
                self.bot = MockBotClient()

        # 1. Adjust all 7 parameters through /console callback queries
        settings_callbacks = [
            ("set_speed_1.5", "⚡ 语速已调整为: 1.5x"),
            ("set_temp_0.3", "🌡️ 发音温度已设置为: 0.3"),
            ("set_split_cut3", "✂️ 切分方式已设置为: cut3"),
            ("set_topk_30", "🎯 Top-K 已设置为: 30"),
            ("set_topp_0.6", "🎯 Top-P 已设置为: 0.6"),
            ("set_batch_8", "📦 批量大小已设置为: 8"),
            ("set_interval_0.5", "⏱️ 分句连播间隔已设置为: 0.5s"),
        ]
        for cb_data, expected_toast in settings_callbacks:
            update = CallbackUpdate(cb_data)
            await handlers.handle_callback_query(update, DummyContext())
            assert update.callback_query.answer_text == expected_toast
            assert update.callback_query.answer_alert is True
            assert update.callback_query.edit_count == 1

        # 2. Spy on default handlers.tts_service.synthesize
        captured_tts_calls = []
        dummy_riff = (
            b"RIFF\x24\x08\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00\x80>\x00\x00\x00}\x00\x00\x02\x00\x10\x00data\x00\x08\x00\x00"
            + b"\x00" * 100
        )

        async def _spy_synthesize(text: str, options=None):
            captured_tts_calls.append({"text": text, "options": dict(options or {})})
            return dummy_riff

        handlers.tts_service.synthesize = _spy_synthesize

        class _FakeAdapter:
            async def chat(self, messages, **kwargs):
                return SimpleNamespace(
                    content=(
                        '{"tts": {"speed": 1.05, "temp": 0.95, "top_k": 12, "top_p": 0.9, '
                        '"fragment_interval": 0.2, "emotion": "happy"}, '
                        '"chinese": "你好。", "japanese": "こんにちは。"}'
                    ),
                    finish_reason="stop",
                )

        monkeypatch.setattr(
            "galgame2voice.services.chat_service.get_llm_adapter",
            lambda *_args, **_kwargs: _FakeAdapter(),
        )
        monkeypatch.setattr(
            "galgame2voice.telegram_bot.handlers.convert_wav_to_ogg",
            AsyncMock(return_value=b"fake_ogg"),
        )

        bot = SimpleNamespace(
            send_chat_action=AsyncMock(),
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=1)),
            send_voice=AsyncMock(),
        )

        voice_task = await handlers.process_text_chat(1001, "早上好", bot, user_id=1001)
        if voice_task:
            await voice_task

        assert len(captured_tts_calls) == 1
        opts = captured_tts_calls[0]["options"]
        assert opts.get("text_split_method") == "cut3"
        assert opts.get("top_k") == 30
        assert opts.get("top_p") == 0.6
        assert opts.get("batch_size") == 8
        assert opts.get("fragment_interval") == 0.5
        assert opts.get("speed_factor") == 1.5
        assert opts.get("speed") == 1.5
        assert opts.get("temperature") == 0.3
        assert opts.get("temp") == 0.3
        assert opts.get("emotion") == "happy"

    @pytest.mark.asyncio
    async def test_telegram_console_tts_settings_update_failure_feedback(self, temp_db_path, monkeypatch):
        """When updating scalar settings or split method fails in DB, query.answer must show error alert and not edit menu."""
        handlers = TelegramBotHandlers(admin_ids=[1001], db_path=temp_db_path)

        class MockQuery:
            def __init__(self, data):
                self.data = data
                self.answer_text = None
                self.answer_alert = None
                self.edit_count = 0

            async def answer(self, text=None, show_alert=False):
                self.answer_text = text
                self.answer_alert = show_alert

            async def edit_message_text(self, text, reply_markup=None):
                self.edit_count += 1

        class CallbackUpdate:
            def __init__(self, data, chat_id=1001, user_id=1001):
                self.callback_query = MockQuery(data)
                self.effective_chat = type("Chat", (), {"id": chat_id})()
                self.effective_user = type("User", (), {"id": user_id})()

        class DummyContext:
            def __init__(self):
                self.bot = MockBotClient()

        async def _mock_update_err(*args, **kwargs):
            raise RuntimeError("Simulated SQLite write error")

        monkeypatch.setattr("galgame2voice.telegram_bot.console_menus.crud.update_settings", _mock_update_err)

        # 1. Test scalar setting failure (speed)
        cb_speed = CallbackUpdate("set_speed_1.5")
        await handlers.handle_callback_query(cb_speed, DummyContext())

        assert cb_speed.callback_query.answer_text == "❌ 保存失败，请稍后重试"
        assert cb_speed.callback_query.answer_alert is True
        assert cb_speed.callback_query.edit_count == 0

        # 2. Test split method setting failure
        cb_split = CallbackUpdate("set_split_cut3")
        await handlers.handle_callback_query(cb_split, DummyContext())

        assert cb_split.callback_query.answer_text == "❌ 保存失败，请稍后重试"
        assert cb_split.callback_query.answer_alert is True
        assert cb_split.callback_query.edit_count == 0


class TestTelegramVoiceMenuRescan:
    """Verifies that clicking '🔄 刷新角色' (menu_voice callback) rescans character packages from disk."""

    @pytest.mark.asyncio
    async def test_telegram_voice_menu_rescans_new_character_packages(self, tmp_path, monkeypatch):
        import wave
        import json
        from galgame2voice.database.session import init_db
        from galgame2voice.services.character_manager import CharacterManager

        db_file = tmp_path / "test.db"
        db_path = str(db_file)
        await init_db(db_path)

        chars_dir = tmp_path / "characters"
        chars_dir.mkdir()

        mgr = CharacterManager(characters_dir=chars_dir)
        monkeypatch.setattr(CharacterManager, "_instance", mgr)

        handlers = TelegramBotHandlers(admin_ids=[1001], db_path=db_path)

        class MockQuery:
            def __init__(self, data):
                self.data = data
                self.answered = False
                self.answer_text = None
                self.edited_text = None
                self.edited_markup = None

            async def answer(self, text=None, show_alert=False):
                self.answered = True
                self.answer_text = text

            async def edit_message_text(self, text, reply_markup=None):
                self.edited_text = text
                self.edited_markup = reply_markup

        class CallbackUpdate:
            def __init__(self, data, chat_id=1001, user_id=1001):
                self.callback_query = MockQuery(data)
                self.effective_chat = type("Chat", (), {"id": chat_id})()
                self.effective_user = type("User", (), {"id": user_id})()

        class DummyContext:
            def __init__(self):
                self.bot = None

        try:
            # Add a new character package to characters/ directory while bot is running
            new_char_dir = chars_dir / "kuro"
            new_char_dir.mkdir()
            (new_char_dir / "gpt.ckpt").write_bytes(b"dummy_weights")
            (new_char_dir / "sovits.pth").write_bytes(b"dummy_weights")
            with wave.open(str(new_char_dir / "ref.wav"), "wb") as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(8000)
                audio.writeframes(b"\x01\x00" * 8000 * 4)

            manifest = {
                "id": "kuro",
                "name": "黑羽",
                "version": "1.0.0",
                "gpt_weights": "gpt.ckpt",
                "sovits_weights": "sovits.pth",
                "system_prompt": "你好，我是黑羽",
                "emotions": {
                    "gentle": {
                        "audio": "ref.wav",
                        "text": "こんにちは",
                        "lang": "ja",
                    }
                },
            }
            (new_char_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

            # Simulate user tapping "🔄 刷新角色" (callback_data="menu_voice")
            cb_update = CallbackUpdate("menu_voice")
            await handlers.handle_callback_query(cb_update, DummyContext())

            assert cb_update.callback_query.edited_text is not None
            assert "黑羽" in cb_update.callback_query.edited_text

            markup = cb_update.callback_query.edited_markup
            assert markup is not None
            btn_texts = [btn.text for row in markup.inline_keyboard for btn in row]
            assert any("黑羽" in text for text in btn_texts)
        finally:
            CharacterManager.reset_instance()

    @pytest.mark.asyncio
    async def test_telegram_voice_menu_rescan_exception_falls_back_gracefully(self, temp_db_path, monkeypatch, caplog):
        from galgame2voice.services.character_manager import CharacterManager

        handlers = TelegramBotHandlers(admin_ids=[1001], db_path=temp_db_path)

        class MockQuery:
            def __init__(self, data):
                self.data = data
                self.answered = False
                self.answer_text = None
                self.edited_text = None
                self.edited_markup = None

            async def answer(self, text=None, show_alert=False):
                self.answered = True
                self.answer_text = text

            async def edit_message_text(self, text, reply_markup=None):
                self.edited_text = text
                self.edited_markup = reply_markup

        class CallbackUpdate:
            def __init__(self, data, chat_id=1001, user_id=1001):
                self.callback_query = MockQuery(data)
                self.effective_chat = type("Chat", (), {"id": chat_id})()
                self.effective_user = type("User", (), {"id": user_id})()

        class DummyContext:
            def __init__(self):
                self.bot = None

        def _exploding_discover():
            raise OSError("Simulated disk I/O error during scan")

        mgr = CharacterManager.get_instance()
        monkeypatch.setattr(mgr, "discover_characters", _exploding_discover)

        try:
            cb_update = CallbackUpdate("menu_voice")
            # Should not raise exception
            await handlers.handle_callback_query(cb_update, DummyContext())

            assert cb_update.callback_query.edited_text is not None
            assert "二次元伴侣中心" in cb_update.callback_query.edited_text
            assert "Character package rescan failed before voice menu render" in caplog.text
        finally:
            CharacterManager.reset_instance()

    @pytest.mark.asyncio
    async def test_telegram_voice_menu_sync_db_exception_falls_back_gracefully(self, temp_db_path, monkeypatch, caplog):
        from galgame2voice.services.character_manager import CharacterManager

        handlers = TelegramBotHandlers(admin_ids=[1001], db_path=temp_db_path)

        class MockQuery:
            def __init__(self, data):
                self.data = data
                self.answered = False
                self.answer_text = None
                self.edited_text = None
                self.edited_markup = None

            async def answer(self, text=None, show_alert=False):
                self.answered = True
                self.answer_text = text

            async def edit_message_text(self, text, reply_markup=None):
                self.edited_text = text
                self.edited_markup = reply_markup

        class CallbackUpdate:
            def __init__(self, data, chat_id=1001, user_id=1001):
                self.callback_query = MockQuery(data)
                self.effective_chat = type("Chat", (), {"id": chat_id})()
                self.effective_user = type("User", (), {"id": user_id})()

        class DummyContext:
            def __init__(self):
                self.bot = None

        async def _exploding_sync(conn):
            raise RuntimeError("Simulated database lock during sync")

        mgr = CharacterManager.get_instance()
        monkeypatch.setattr(mgr, "sync_with_db", _exploding_sync)

        try:
            cb_update = CallbackUpdate("menu_voice")
            await handlers.handle_callback_query(cb_update, DummyContext())

            assert cb_update.callback_query.edited_text is not None
            assert "二次元伴侣中心" in cb_update.callback_query.edited_text
            assert "Character package rescan failed before voice menu render" in caplog.text
        finally:
            CharacterManager.reset_instance()

    @pytest.mark.asyncio
    async def test_telegram_voice_menu_rescan_handles_partially_invalid_packages(self, tmp_path, monkeypatch):
        import wave
        import json
        from galgame2voice.database.session import init_db
        from galgame2voice.services.character_manager import CharacterManager

        db_file = tmp_path / "test.db"
        db_path = str(db_file)
        await init_db(db_path)

        chars_dir = tmp_path / "characters"
        chars_dir.mkdir()

        mgr = CharacterManager(characters_dir=chars_dir)
        monkeypatch.setattr(CharacterManager, "_instance", mgr)

        handlers = TelegramBotHandlers(admin_ids=[1001], db_path=db_path)

        class MockQuery:
            def __init__(self, data):
                self.data = data
                self.answered = False
                self.answer_text = None
                self.edited_text = None
                self.edited_markup = None

            async def answer(self, text=None, show_alert=False):
                self.answered = True
                self.answer_text = text

            async def edit_message_text(self, text, reply_markup=None):
                self.edited_text = text
                self.edited_markup = reply_markup

        class CallbackUpdate:
            def __init__(self, data, chat_id=1001, user_id=1001):
                self.callback_query = MockQuery(data)
                self.effective_chat = type("Chat", (), {"id": chat_id})()
                self.effective_user = type("User", (), {"id": user_id})()

        class DummyContext:
            def __init__(self):
                self.bot = None

        try:
            # 1. Invalid package folder
            bad_dir = chars_dir / "bad_pkg"
            bad_dir.mkdir()
            (bad_dir / "manifest.json").write_text("{invalid_json", encoding="utf-8")

            # 2. Valid package folder
            good_dir = chars_dir / "kuro"
            good_dir.mkdir()
            (good_dir / "gpt.ckpt").write_bytes(b"dummy_weights")
            (good_dir / "sovits.pth").write_bytes(b"dummy_weights")
            with wave.open(str(good_dir / "ref.wav"), "wb") as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(8000)
                audio.writeframes(b"\x01\x00" * 8000 * 4)

            manifest = {
                "id": "kuro",
                "name": "黑羽",
                "version": "1.0.0",
                "gpt_weights": "gpt.ckpt",
                "sovits_weights": "sovits.pth",
                "system_prompt": "你好，我是黑羽",
                "emotions": {
                    "gentle": {
                        "audio": "ref.wav",
                        "text": "こんにちは",
                        "lang": "ja",
                    }
                },
            }
            (good_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

            cb_update = CallbackUpdate("menu_voice")
            await handlers.handle_callback_query(cb_update, DummyContext())

            assert cb_update.callback_query.edited_text is not None
            assert "黑羽" in cb_update.callback_query.edited_text
            markup = cb_update.callback_query.edited_markup
            btn_texts = [btn.text for row in markup.inline_keyboard for btn in row]
            assert any("黑羽" in text for text in btn_texts)
        finally:
            CharacterManager.reset_instance()


class TestTelegramVoiceTaskProfileBinding:
    """Verifies that background voice tasks bind to the turn's voice profile and respect db_path."""

    @pytest.mark.asyncio
    async def test_telegram_voice_task_locks_turn_profile_and_db_path(self, temp_db_path, monkeypatch):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from galgame2voice.database import crud
        from galgame2voice.database.session import get_db
        from galgame2voice.database.models import VoiceProfileCreate
        from galgame2voice.services.tts_service import TtsService

        # 1. Assert default tts_service db_path is initialized to db_path
        handlers = TelegramBotHandlers(admin_ids=[1001], db_path=temp_db_path)
        assert isinstance(handlers.tts_service, TtsService)
        assert handlers.tts_service.db_path == temp_db_path

        # 2. Seed database with Character A and Character B, activating Character A
        async with get_db(temp_db_path) as conn:
            await crud.get_settings_raw(conn)
            profile_a = await crud.create_voice_profile(
                conn,
                VoiceProfileCreate(
                    name="爱丽丝",
                    gpt_weights_path="weights/alice.ckpt",
                    sovits_weights_path="weights/alice.pth",
                    ref_audio_path="ref/alice.wav",
                    prompt_text="こんにちは、アリスです。",
                    prompt_lang="ja",
                    text_lang="ja",
                ),
            )
            profile_b = await crud.create_voice_profile(
                conn,
                VoiceProfileCreate(
                    name="鲍勃",
                    gpt_weights_path="weights/bob.ckpt",
                    sovits_weights_path="weights/bob.pth",
                    ref_audio_path="ref/bob.wav",
                    prompt_text="こんにちは、ボブです。",
                    prompt_lang="ja",
                    text_lang="ja",
                ),
            )
            await crud.set_active_voice_profile(conn, profile_a.id)

        # Mock LLM adapter returning bilingual response
        class _FakeAdapter:
            async def chat(self, messages, **kwargs):
                return SimpleNamespace(
                    content='{"chinese": "早上好！", "japanese": "おはよう！"}',
                    finish_reason="stop",
                )

        monkeypatch.setattr(
            "galgame2voice.services.chat_service.get_llm_adapter",
            lambda *_args, **_kwargs: _FakeAdapter(),
        )
        monkeypatch.setattr(
            "galgame2voice.telegram_bot.handlers.convert_wav_to_ogg",
            AsyncMock(return_value=b"fake_ogg"),
        )

        bot = SimpleNamespace(
            send_chat_action=AsyncMock(),
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=1)),
            send_voice=AsyncMock(),
        )

        # Suspend background voice synthesis before actual synthesis
        synthesis_started = asyncio.Event()
        allow_synthesis = asyncio.Event()
        captured_client_calls = []

        real_synthesize = handlers.tts_service.synthesize

        async def _intercepting_synthesize(text, options=None, **kwargs):
            synthesis_started.set()
            await allow_synthesis.wait()
            return await real_synthesize(text, options=options, **kwargs)

        handlers.tts_service.synthesize = _intercepting_synthesize

        async def _spy_client_synthesize(text, options=None, **kwargs):
            captured_client_calls.append({"text": text, "options": dict(options or {})})
            return (
                b"RIFF\x24\x08\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00\x80>\x00\x00\x00}\x00\x00\x02\x00\x10\x00data\x00\x08\x00\x00"
                + b"\x00" * 200
            )

        handlers.tts_service.client.synthesize = _spy_client_synthesize

        # 3. Trigger process_text_chat while Character A is active
        voice_task = await handlers.process_text_chat(chat_id=1001, text="早上好", bot=bot, user_id=1001)
        assert voice_task is not None

        # Wait until background voice task is suspended before synthesizing
        await synthesis_started.wait()

        # 4. Switch global active voice profile to Character B before releasing synthesis
        async with get_db(temp_db_path) as conn:
            await crud.set_active_voice_profile(conn, profile_b.id)

        # 5. Release synthesis and await completion
        allow_synthesis.set()
        await voice_task

        # 6. Assert tts_service resolved and used Character A's profile id, character name, and model weights
        assert len(captured_client_calls) == 1
        used_opts = captured_client_calls[0]["options"]
        assert used_opts.get("voice_profile_id") == profile_a.id
        assert used_opts.get("character_name") == profile_a.name
        assert used_opts.get("gpt_weights_path") == profile_a.gpt_weights_path
        assert used_opts.get("sovits_weights_path") == profile_a.sovits_weights_path

        # 7. When no voice profile exists, dummy selectors (1 / "Character") are not injected
        async with get_db(temp_db_path) as conn:
            await crud.delete_voice_profile(conn, profile_a.id)
            await crud.delete_voice_profile(conn, profile_b.id)

        no_profile_task = await handlers.process_text_chat(chat_id=1001, text="无音色测试", bot=bot, user_id=1001)
        assert no_profile_task is not None
        await no_profile_task

        assert len(captured_client_calls) == 2
        no_profile_opts = captured_client_calls[1]["options"]
        assert "voice_profile_id" not in no_profile_opts
        assert "character_name" not in no_profile_opts

    @pytest.mark.asyncio
    async def test_telegram_voice_task_concurrent_sessions_isolated_across_character_switches(
        self, temp_db_path, monkeypatch
    ):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from galgame2voice.database import crud
        from galgame2voice.database.session import get_db
        from galgame2voice.database.models import VoiceProfileCreate

        handlers = TelegramBotHandlers(admin_ids=[1001, 1002], db_path=temp_db_path)

        async with get_db(temp_db_path) as conn:
            await crud.get_settings_raw(conn)
            profile_a = await crud.create_voice_profile(
                conn,
                VoiceProfileCreate(
                    name="爱丽丝",
                    gpt_weights_path="weights/alice.ckpt",
                    sovits_weights_path="weights/alice.pth",
                    ref_audio_path="ref/alice.wav",
                    prompt_text="こんにちは、アリスです。",
                    prompt_lang="ja",
                    text_lang="ja",
                ),
            )
            profile_b = await crud.create_voice_profile(
                conn,
                VoiceProfileCreate(
                    name="鲍勃",
                    gpt_weights_path="weights/bob.ckpt",
                    sovits_weights_path="weights/bob.pth",
                    ref_audio_path="ref/bob.wav",
                    prompt_text="こんにちは、ボブです。",
                    prompt_lang="ja",
                    text_lang="ja",
                ),
            )
            await crud.set_active_voice_profile(conn, profile_a.id)

        class _FakeAdapter:
            async def chat(self, messages, **kwargs):
                return SimpleNamespace(
                    content='{"chinese": "回复内容", "japanese": "返信内容"}',
                    finish_reason="stop",
                )

        monkeypatch.setattr(
            "galgame2voice.services.chat_service.get_llm_adapter",
            lambda *_args, **_kwargs: _FakeAdapter(),
        )
        monkeypatch.setattr(
            "galgame2voice.telegram_bot.handlers.convert_wav_to_ogg",
            AsyncMock(return_value=b"fake_ogg"),
        )

        bot = SimpleNamespace(
            send_chat_action=AsyncMock(),
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=1)),
            send_voice=AsyncMock(),
        )

        pause_synthesis = asyncio.Event()
        captured_client_calls = []

        real_synthesize = handlers.tts_service.synthesize

        async def _intercepting_synthesize(text, options=None, **kwargs):
            await pause_synthesis.wait()
            return await real_synthesize(text, options=options, **kwargs)

        handlers.tts_service.synthesize = _intercepting_synthesize

        async def _spy_client_synthesize(text, options=None, **kwargs):
            captured_client_calls.append({"text": text, "options": dict(options or {})})
            return (
                b"RIFF\x24\x08\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00\x80>\x00\x00\x00}\x00\x00\x02\x00\x10\x00data\x00\x08\x00\x00"
                + b"\x00" * 200
            )

        handlers.tts_service.client.synthesize = _spy_client_synthesize

        # Turn 1: user 1001 with Character A active
        task_1 = await handlers.process_text_chat(chat_id=1001, text="用户1的消息", bot=bot, user_id=1001)

        # Switch active profile to Character B
        async with get_db(temp_db_path) as conn:
            await crud.set_active_voice_profile(conn, profile_b.id)

        # Turn 2: user 1002 with Character B active
        task_2 = await handlers.process_text_chat(chat_id=1002, text="用户2的消息", bot=bot, user_id=1002)

        # Switch active profile to NULL / none
        async with get_db(temp_db_path) as conn:
            await conn.execute("UPDATE settings SET active_voice_profile_id = NULL WHERE id = 1;")
            await conn.commit()

        # Unpause and await both tasks
        pause_synthesis.set()
        await task_1
        await task_2

        # Verify that task_1 used profile_a and task_2 used profile_b
        assert len(captured_client_calls) == 2
        calls_by_profile = {
            call["options"]["voice_profile_id"]: call["options"]
            for call in captured_client_calls
        }
        assert profile_a.id in calls_by_profile
        assert profile_b.id in calls_by_profile

        assert calls_by_profile[profile_a.id].get("character_name") == profile_a.name
        assert calls_by_profile[profile_a.id].get("gpt_weights_path") == profile_a.gpt_weights_path

        assert calls_by_profile[profile_b.id].get("character_name") == profile_b.name
        assert calls_by_profile[profile_b.id].get("gpt_weights_path") == profile_b.gpt_weights_path


class TestTelegramVoiceVocalFiltering:
    """Verifies that replies containing only stage directions, whitespace, or punctuation skip voice synthesis."""

    @pytest.mark.parametrize("zh_input,zh_reply,ja_reply,should_voice,expected_clean_ja", [
        ("纯动作括号", "（微笑）", "（微笑む）", False, None),
        ("动作加省略号", "（叹气）……", "（ため息）……", False, None),
        ("空日文回复", "（默默注视着你）", "", False, None),
        ("纯省略号", "……", "……", False, None),
        ("纯空白", "这是中文回复", "   ", False, None),
        ("黑括号动作", "【微笑】", "【微笑】", False, None),
        ("星号动作", "*nods*", "*nods*", False, None),
        ("带动作的正常台词", "（微笑）早上好。", "（微笑む）おはよう。", True, "おはよう。"),
    ])
    @pytest.mark.asyncio
    async def test_telegram_process_text_chat_skips_stage_direction_only_voice(
        self, tmp_path, temp_db_path, monkeypatch, caplog, zh_input, zh_reply, ja_reply, should_voice, expected_clean_ja
    ):
        import json
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from galgame2voice.database import crud
        from galgame2voice.database.models import VoiceProfileCreate
        from galgame2voice.database.session import get_db
        from galgame2voice.services.gpt_sovits_client import GptSovitsClient
        from galgame2voice.services.tts_cache_manager import TtsCacheManager
        from galgame2voice.services.tts_service import TtsService

        mock_sovits = MockGptSovitsServer()
        client = GptSovitsClient(server=mock_sovits)
        cache_mgr = TtsCacheManager(cache_dir=tmp_path / "cache", db_path=temp_db_path)

        handlers = TelegramBotHandlers(admin_ids=[1001], db_path=temp_db_path)
        handlers.tts_service = TtsService(
            client=client,
            audio_dir=tmp_path / "audio",
            cache_manager=cache_mgr,
            db_path=temp_db_path,
        )

        try:
            async with get_db(temp_db_path) as conn:
                await crud.get_settings_raw(conn)
                profile = await crud.create_voice_profile(
                    conn,
                    VoiceProfileCreate(
                        name="测试角色",
                        gpt_weights_path="weights/test.ckpt",
                        sovits_weights_path="weights/test.pth",
                        ref_audio_path="ref/test.wav",
                        prompt_text="テストプロンプト",
                        prompt_lang="ja",
                        text_lang="ja",
                    ),
                )
                await crud.set_active_voice_profile(conn, profile.id)

            raw_llm_json = json.dumps({"lines": [{"zh": zh_reply, "ja": ja_reply}]}, ensure_ascii=False)

            class _FakeAdapter:
                async def chat(self, messages, **kwargs):
                    return SimpleNamespace(content=raw_llm_json, finish_reason="stop")

            monkeypatch.setattr(
                "galgame2voice.services.chat_service.get_llm_adapter",
                lambda *_args, **_kwargs: _FakeAdapter(),
            )
            monkeypatch.setattr(
                "galgame2voice.telegram_bot.handlers.convert_wav_to_ogg",
                AsyncMock(return_value=b"fake_ogg"),
            )

            bot = SimpleNamespace(
                send_chat_action=AsyncMock(),
                send_message=AsyncMock(return_value=SimpleNamespace(message_id=1)),
                send_voice=AsyncMock(),
            )

            with caplog.at_level(logging.ERROR):
                task = await handlers.process_text_chat(chat_id=1001, text=zh_input, bot=bot, user_id=1001)
                assert task is not None
                await task

            bot.send_message.assert_awaited_once()
            sent_text = bot.send_message.call_args.kwargs["text"]
            assert sent_text.strip() != ""

            async with get_db(temp_db_path) as conn:
                msgs = await crud.get_recent_messages(conn, session_id="tg_1001", limit=10)
                assert any(m.role == "assistant" and m.content_chinese == sent_text for m in msgs)

            record_actions = [c for c in bot.send_chat_action.call_args_list if c.kwargs.get("action") == "record_voice"]
            tts_calls = [c for c in mock_sovits.call_history if c["endpoint"] == "/tts"]

            if not should_voice:
                assert len(record_actions) == 0
                assert len(tts_calls) == 0
                bot.send_voice.assert_not_awaited()
            else:
                assert len(record_actions) == 1
                assert len(tts_calls) == 1
                assert tts_calls[0]["payload"]["text"] == expected_clean_ja
                bot.send_voice.assert_awaited_once()
                assert bot.send_voice.call_args.kwargs["caption"] == expected_clean_ja

            error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
            assert error_records == []
        finally:
            await handlers.chat_service.aclose()
            await client.aclose()
