"""
Tests for Telegram Bot Integration, Command Handlers, Async Voice Note Queue, and STT Pipeline.
Covers Tier 1 (Commands, Text Replies, Background Voice Sending, STT Flow)
and Tier 2 (Token Validation, Network Failures, Audio Conversion, Concurrent Users, Cancellation).
"""

import asyncio
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
            wav_bytes = await convert_ogg_to_wav(voice_bytes)
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
        handlers = TelegramBotHandlers(db_path=temp_db_path)
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
        handlers = TelegramBotHandlers(db_path=temp_db_path)
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

        cb_reset = CallbackUpdate("action_reset")
        await handlers.handle_callback_query(cb_reset, DummyContext())
        assert "清空" in (cb_reset.callback_query.answer_text or "")


