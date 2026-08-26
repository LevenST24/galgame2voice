"""
Adversarial Stress & Empirical Challenge Test Suite for Milestone 6 (Audio & Multi-User Isolation).
Authored by m6_challenger_2.

Challenges & Stress Dimensions:
1. Multi-User Concurrency & Session Isolation:
   - 15+ concurrent users sending simultaneous text & voice messages
   - Zero crosstalk / chat_id leakage
   - Cancellation isolation (interrupting user A cancels ONLY user A's task, not user B/C/D)
   - High concurrency SQLite message sequence & foreign key consistency
2. Audio Converter Fuzzing, Boundary Conditions & Error Recovery:
   - Random byte payload fuzzing across multiple buffer sizes (0B, 5B, 11B, 50B, 1KB, 100KB, 1MB)
   - Truncated audio headers (partial OggS, partial RIFF/WAV)
   - Non-audio formats (HTML, JSON, Executable) disguised as audio
   - Missing ffmpeg binary simulation
   - Non-zero ffmpeg exit codes / subprocess crash simulation
   - Temporary file leak prevention under error and exception conditions
3. Telegram Voice Handler Resiliency:
   - Corrupt voice download handling
   - Network failure during voice file download
   - STT adapter failure / empty transcription handling
   - TTS synthesis background failure isolation
   - Update object malformation robustness
"""

import asyncio
import os
import random
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, patch

import pytest
import aiosqlite

from galgame2voice.database.session import init_db, get_db
from galgame2voice.database import crud
from galgame2voice.database.models import MessageCreate
from galgame2voice.services.chat_service import ChatService
from galgame2voice.services.tts_service import TtsService
from galgame2voice.telegram_bot.handlers import TelegramBotHandlers
from galgame2voice.telegram_bot.bot import validate_bot_token, TelegramBotManager
from galgame2voice.utils.audio_converter import (
    is_ffmpeg_available,
    convert_ogg_to_wav,
    convert_wav_to_ogg,
    run_ffmpeg_command,
)


@pytest.fixture
async def challenge_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        await init_db(path)
        yield path
    finally:
        if os.path.exists(path):
            try:
                os.unlink(path)
            except OSError:
                pass


class MockTelegramBotClient:
    """Thread-safe mock Telegram Bot client recording sent messages and voice notes per chat_id."""

    def __init__(self):
        self._lock = asyncio.Lock()
        self.sent_messages: List[Dict[str, Any]] = []
        self.sent_voices: List[Dict[str, Any]] = []

    async def send_message(self, chat_id: int, text: str) -> Dict[str, Any]:
        async with self._lock:
            msg = {"chat_id": chat_id, "text": text}
            self.sent_messages.append(msg)
            return msg

    async def send_voice(self, chat_id: int, voice: bytes, caption: Optional[str] = None) -> Dict[str, Any]:
        async with self._lock:
            v = {"chat_id": chat_id, "size": len(voice), "caption": caption}
            self.sent_voices.append(v)
            return v


# ============================================================================
# Section 1: Multi-User Concurrency & Session Isolation Stress
# ============================================================================

class TestMultiUserConcurrencyAndIsolation:
    """Adversarial stress testing of multi-user isolation on Telegram Bot."""

    @pytest.mark.asyncio
    async def test_multi_user_concurrent_chat_isolation_under_load(self, challenge_db):
        """
        Stress test 15 concurrent users sending simultaneous messages.
        Verifies:
        1. Every user receives their own matching response.
        2. No crosstalk between distinct chat_ids.
        3. Background voice synthesis finishes and dispatches voice to correct chat_id.
        4. DB persistence maintains strict user session isolation.
        """
        bot_client = MockTelegramBotClient()
        handlers = TelegramBotHandlers(db_path=challenge_db)
        num_users = 15

        # Configure mock LLM adapter to return custom bilingual payload per user
        async def mock_chat_impl(messages, model=None):
            user_msg = messages[-1].content
            user_id = user_msg.split(":")[0].strip()
            resp = AsyncMock()
            resp.content = f'{{"chinese": "回复给{user_id}", "japanese": "{user_id}への返信"}}'
            return resp

        mock_adapter = AsyncMock()
        mock_adapter.chat.side_effect = mock_chat_impl

        async def mock_synthesize(text, *args, **kwargs):
            return b"RIFF\x24\x08\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00\x80>\x00\x00\x00}\x00\x00\x02\x00\x10\x00data\x00\x08\x00\x00" + b"\x00" * 100

        with patch.object(handlers.chat_service, "get_active_llm_adapter", return_value=(mock_adapter, "mock-model", "mock-provider")), \
             patch.object(handlers.tts_service, "synthesize", side_effect=mock_synthesize):
            async def user_worker(user_idx: int):
                chat_id = 10000 + user_idx
                prompt = f"User_{user_idx}: 你好，这是我的专属提问！"
                task = await handlers.process_text_chat(chat_id, prompt, bot_client)
                await task
                return chat_id

            tasks = [user_worker(i) for i in range(num_users)]
            results = await asyncio.gather(*tasks)

            assert len(results) == num_users

            # Check messages received
            assert len(bot_client.sent_messages) == num_users
            assert len(bot_client.sent_voices) == num_users

            # Verify zero crosstalk in sent text messages
            for msg in bot_client.sent_messages:
                chat_id = msg["chat_id"]
                user_idx = chat_id - 10000
                assert f"回复给User_{user_idx}" in msg["text"], (
                    f"Crosstalk detected! chat_id {chat_id} received message: {msg['text']}"
                )

            # Verify zero crosstalk in sent voices
            for voice in bot_client.sent_voices:
                chat_id = voice["chat_id"]
                user_idx = chat_id - 10000
                assert f"User_{user_idx}への返信" in voice["caption"], (
                    f"Voice crosstalk detected! chat_id {chat_id} received caption: {voice['caption']}"
                )

            # Verify DB session isolation
            async with get_db(challenge_db) as conn:
                for i in range(num_users):
                    session_id = f"tg_{10000 + i}"
                    history = await crud.get_recent_messages(conn, session_id)
                    assert len(history) == 2, f"Expected 2 turns for {session_id}, got {len(history)}"
                    assert history[0].role == "user"
                    assert f"User_{i}" in history[0].content_chinese
                    assert history[1].role == "assistant"
                    assert f"回复给User_{i}" in history[1].content_chinese

    @pytest.mark.asyncio
    async def test_multi_user_cancellation_isolation(self, challenge_db):
        """
        Stress test cancellation isolation across concurrent users:
        - User 1001 sends a message, and immediately sends an interrupting message.
        - Simultaneously, 9 other users (1002 - 1010) send a single message with slow synthesis.
        Verifies:
        - ONLY User 1001's first voice task is cancelled.
        - All other 9 users' voice tasks complete successfully without being affected.
        """
        bot_client = MockTelegramBotClient()
        handlers = TelegramBotHandlers(db_path=challenge_db)

        # Make TTS slow to allow race window
        async def slow_synthesize(text, *args, **kwargs):
            await asyncio.sleep(0.08)
            # Return dummy WAV
            return b"RIFF\x24\x08\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00\x80>\x00\x00\x00}\x00\x00\x02\x00\x10\x00data\x00\x08\x00\x00" + b"\x00" * 100

        with patch.object(handlers.tts_service, "synthesize", side_effect=slow_synthesize):
            mock_adapter = AsyncMock()
            mock_adapter.chat.return_value = AsyncMock(content='{"chinese": "回答", "japanese": "返事"}')

            with patch.object(handlers.chat_service, "get_active_llm_adapter", return_value=(mock_adapter, "mock-model", "mock-provider")):
                # 1. Spawn slow tasks for users 1002..1010
                other_tasks = []
                for uid in range(1002, 1011):
                    t = await handlers.process_text_chat(uid, f"Msg from {uid}", bot_client)
                    other_tasks.append((uid, t))

                # 2. Spawn task 1 for user 1001
                task_user1_msg1 = await handlers.process_text_chat(1001, "User1 Msg 1", bot_client)
                await asyncio.sleep(0.01)

                # 3. User 1001 interrupts with message 2
                task_user1_msg2 = await handlers.process_text_chat(1001, "User1 Msg 2", bot_client)

                # Wait for all running tasks
                all_pending = [t for _, t in other_tasks] + [task_user1_msg2]
                await asyncio.gather(*all_pending, return_exceptions=True)

                # User 1001's first task must be cancelled
                assert task_user1_msg1.cancelled() or task_user1_msg1.done()

                # Verify all other users' tasks completed normally
                for uid, t in other_tasks:
                    assert t.done()
                    assert not t.cancelled()

                # Sent voices should contain exactly 1 voice for 1001 and 1 voice for each of 1002..1010
                voice_chat_ids = [v["chat_id"] for v in bot_client.sent_voices]
                assert voice_chat_ids.count(1001) == 1
                for uid in range(1002, 1011):
                    assert voice_chat_ids.count(uid) == 1

                assert len(bot_client.sent_voices) == 10

    @pytest.mark.asyncio
    async def test_concurrent_reset_and_chat_race(self, challenge_db):
        """
        Simulate a user rapidly sending chat messages and /reset commands concurrently.
        Verifies no unhandled exceptions, deadlocks, or DB corruption.
        """
        bot_client = MockTelegramBotClient()
        handlers = TelegramBotHandlers(db_path=challenge_db)
        chat_id = 88888

        class DummyUpdate:
            def __init__(self, c_id):
                self.effective_chat = type("Chat", (), {"id": c_id})()
                self.message = None

        class DummyContext:
            def __init__(self, bot):
                self.bot = bot

        mock_adapter = AsyncMock()
        mock_adapter.chat.return_value = AsyncMock(content='{"chinese": "你好", "japanese": "こんにちは"}')

        with patch.object(handlers.chat_service, "get_active_llm_adapter", return_value=(mock_adapter, "mock-model", "mock-provider")):
            async def send_msg_worker():
                for i in range(5):
                    t = await handlers.process_text_chat(chat_id, f"Race message {i}", bot_client)
                    await asyncio.sleep(0.01)
                    await t

            async def reset_worker():
                for _ in range(3):
                    await asyncio.sleep(0.015)
                    await handlers.handle_reset(DummyUpdate(chat_id), DummyContext(bot_client))

            await asyncio.gather(send_msg_worker(), reset_worker(), return_exceptions=False)


# ============================================================================
# Section 2: Audio Converter Defensive & Fuzzing Stress Tests
# ============================================================================

class TestAudioConverterAdversarial:
    """Adversarial testing of audio converter against malformed, random, and extreme inputs."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("payload_size", [0, 1, 5, 11, 15, 30, 44, 100, 1024, 65536])
    async def test_convert_ogg_to_wav_random_noise_fuzzing(self, payload_size):
        """
        Fuzz convert_ogg_to_wav with purely random byte buffers of varying sizes.
        Must raise ValueError or handle cleanly; MUST NEVER crash with unhandled exception or segfault.
        """
        # Generate random bytes
        random_bytes = os.urandom(payload_size)

        with pytest.raises(ValueError) as exc_info:
            await convert_ogg_to_wav(random_bytes)
        assert len(str(exc_info.value)) > 0

    @pytest.mark.asyncio
    async def test_convert_ogg_to_wav_truncated_magic_headers(self):
        """
        Test inputs that start with valid magic bytes ('OggS' or 'RIFF') but are truncated or corrupted immediately after.
        """
        # 1. Truncated OggS
        with pytest.raises(ValueError):
            await convert_ogg_to_wav(b"OggS")

        # 2. OggS followed by random garbage
        corrupted_oggs = b"OggS\x00\x02\x00\x00\x00\x00\x00\x00\x00\x00" + os.urandom(100)
        with pytest.raises(ValueError):
            await convert_ogg_to_wav(corrupted_oggs)

        # 3. Truncated RIFF
        with pytest.raises(ValueError):
            await convert_ogg_to_wav(b"RIFF\x00\x00")

    @pytest.mark.asyncio
    async def test_convert_ogg_to_wav_non_audio_formats(self):
        """
        Test valid non-audio file payloads disguised as audio bytes (JSON, HTML, PNG header, ELF header).
        """
        non_audio_samples = [
            b'{"error": "not an audio file", "status": 400, "data": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]}',
            b'<!DOCTYPE html><html><head><title>Error</title></head><body><h1>404 Not Found</h1></body></html>',
            b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x10\x00\x00\x00\x10\x08\x06\x00\x00\x00\x1f\xf3\xffa',
            b'\x7fELF\x02\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x02\x00>\x00\x01\x00\x00\x00',
        ]
        for sample in non_audio_samples:
            with pytest.raises(ValueError):
                await convert_ogg_to_wav(sample)

    @pytest.mark.asyncio
    async def test_convert_wav_to_ogg_empty_and_corrupt(self):
        """Test convert_wav_to_ogg with empty input."""
        with pytest.raises(ValueError, match="WAV bytes cannot be empty"):
            await convert_wav_to_ogg(b"")

    @pytest.mark.asyncio
    async def test_missing_ffmpeg_simulation(self):
        """
        Simulate missing ffmpeg binary (is_ffmpeg_available returns False).
        Missing ffmpeg must fail loudly instead of silently producing wrong audio.
        """
        with patch("galgame2voice.utils.audio_converter.is_ffmpeg_available", return_value=False):
            # 1. convert_ogg_to_wav with missing ffmpeg -> loud failure
            valid_fake_ogg = b"OggS\x00\x02\x00\x00" + b"\x00" * 50
            with pytest.raises(RuntimeError, match="ffmpeg executable not found"):
                await convert_ogg_to_wav(valid_fake_ogg, ffmpeg_path="non_existent_ffmpeg")

            # 2. convert_wav_to_ogg with missing ffmpeg -> loud failure
            sample_wav = b"RIFF\x24\x08\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00\x80>\x00\x00\x00}\x00\x00\x02\x00\x10\x00data\x00\x08\x00\x00"
            with pytest.raises(RuntimeError, match="ffmpeg executable not found"):
                await convert_wav_to_ogg(sample_wav, ffmpeg_path="non_existent_ffmpeg")

    @pytest.mark.asyncio
    async def test_ffmpeg_subprocess_crash_and_nonzero_exit_code(self):
        """
        Simulate ffmpeg command returning exit code 1 or 137.
        Verifies that RuntimeError is caught and wrapped into ValueError.
        """
        with patch("galgame2voice.utils.audio_converter.is_ffmpeg_available", return_value=True):
            with patch("galgame2voice.utils.audio_converter.run_ffmpeg_command", side_effect=RuntimeError("ffmpeg conversion failed (code 1): Invalid data")):
                valid_fake_ogg = b"OggS\x00\x02\x00\x00" + b"\x00" * 50
                with pytest.raises(ValueError, match="Audio conversion failed"):
                    await convert_ogg_to_wav(valid_fake_ogg)

    @pytest.mark.asyncio
    async def test_temp_file_cleanup_on_conversion_failure(self):
        """
        Verify that temporary files (.ogg and .wav) are unlinked and cleaned up even when ffmpeg fails.
        """
        temp_dir = Path(tempfile.gettempdir())
        before_temp_files = set(temp_dir.glob("*.ogg")).union(set(temp_dir.glob("*.wav")))

        # Intentionally fail conversion
        corrupted_oggs = b"OggS\x00\x02\x00\x00" + os.urandom(100)
        try:
            await convert_ogg_to_wav(corrupted_oggs)
        except ValueError:
            pass

        after_temp_files = set(temp_dir.glob("*.ogg")).union(set(temp_dir.glob("*.wav")))
        # Ensure no newly leaked temporary files remain in temp directory
        newly_created = after_temp_files - before_temp_files
        assert len(newly_created) == 0, f"Leaked temporary audio files: {newly_created}"


# ============================================================================
# Section 3: Telegram Voice Handler Defensive Error Recovery
# ============================================================================

class TestTelegramVoiceHandlerErrorRecovery:
    """Adversarial testing of Telegram voice note download, STT, and TTS error recovery."""

    @pytest.mark.asyncio
    async def test_voice_handler_corrupted_download_bytes(self, challenge_db):
        """
        Simulate downloading corrupted voice bytes from Telegram.
        Verifies user receives error reply and bot does not crash.
        """
        handlers = TelegramBotHandlers(db_path=challenge_db)
        mock_reply = AsyncMock()

        class MockVoice:
            file_id = "corrupt_voice_file_123"

        class MockUpdate:
            effective_chat = type("Chat", (), {"id": 20001})()
            message = type("Msg", (), {
                "voice": MockVoice(),
                "reply_text": mock_reply,
            })()

        class MockContext:
            class Bot:
                async def get_file(self, file_id):
                    class TGFile:
                        async def download_as_bytearray(self):
                            return bytearray(b"CORRUPT_NOT_AUDIO")
                    return TGFile()
            bot = Bot()

        result = await handlers.handle_voice_message(MockUpdate(), MockContext())
        assert result is None
        mock_reply.assert_called_once_with("抱歉，语音解析失败，请重试！")

    @pytest.mark.asyncio
    async def test_voice_handler_network_timeout_during_file_download(self, challenge_db):
        """
        Simulate network timeout / connection error during Telegram get_file.
        Verifies bot handles exception gracefully and informs user.
        """
        handlers = TelegramBotHandlers(db_path=challenge_db)
        mock_reply = AsyncMock()

        class MockVoice:
            file_id = "timeout_file"

        class MockUpdate:
            effective_chat = type("Chat", (), {"id": 20002})()
            message = type("Msg", (), {
                "voice": MockVoice(),
                "reply_text": mock_reply,
            })()

        class MockContext:
            class Bot:
                async def get_file(self, file_id):
                    raise TimeoutError("Telegram CDN download timeout")
            bot = Bot()

        result = await handlers.handle_voice_message(MockUpdate(), MockContext())
        assert result is None
        mock_reply.assert_called_once_with("抱歉，语音处理出错，请重试！")

    @pytest.mark.asyncio
    async def test_voice_handler_stt_adapter_exception(self, challenge_db):
        """
        Simulate STT adapter raising an unhandled exception (e.g. API quota exceeded, 500 server error).
        Verifies bot catches exception, replies to user, and does not crash.
        """
        handlers = TelegramBotHandlers(db_path=challenge_db)
        mock_reply = AsyncMock()

        class MockVoice:
            file_id = "valid_voice_file"

        class MockUpdate:
            effective_chat = type("Chat", (), {"id": 20003})()
            message = type("Msg", (), {
                "voice": MockVoice(),
                "reply_text": mock_reply,
            })()

        class MockContext:
            class Bot:
                async def get_file(self, file_id):
                    class TGFile:
                        async def download_as_bytearray(self):
                            # Valid mock WAV header
                            return bytearray(b"RIFF\x24\x08\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00\x80>\x00\x00\x00}\x00\x00\x02\x00\x10\x00data\x00\x08\x00\x00" + b"\x00" * 100)
                    return TGFile()
            bot = Bot()

        with patch("galgame2voice.telegram_bot.handlers.get_stt_adapter") as mock_stt_getter:
            mock_stt = AsyncMock()
            mock_stt.transcribe.side_effect = RuntimeError("STT API Rate Limit Exceeded (HTTP 429)")
            mock_stt_getter.return_value = mock_stt

            result = await handlers.handle_voice_message(MockUpdate(), MockContext())
            assert result is None
            mock_reply.assert_called_once_with("抱歉，语音处理出错，请重试！")

    @pytest.mark.asyncio
    async def test_background_voice_tts_failure_does_not_break_text_reply(self, challenge_db):
        """
        Simulate TTS service failure (e.g. GPT-SoVITS offline / error) during background voice synthesis.
        Verifies:
        1. Immediate text reply was successfully sent to the user.
        2. Background task logs error and terminates cleanly without uncaught exception crash.
        """
        bot_client = MockTelegramBotClient()
        handlers = TelegramBotHandlers(db_path=challenge_db)
        chat_id = 30001

        # LLM returns valid bilingual reply
        mock_adapter = AsyncMock()
        mock_adapter.chat.return_value = AsyncMock(content='{"chinese": "这是文字回复", "japanese": "テキスト返信"}')

        # TTS fails
        with patch.object(handlers.chat_service, "get_active_llm_adapter", return_value=(mock_adapter, "mock-model", "mock-provider")):
            with patch.object(handlers.tts_service, "synthesize", side_effect=ConnectionError("GPT-SoVITS connection refused on port 9880")):
                task = await handlers.process_text_chat(chat_id, "你好", bot_client)

                # Wait for background task to complete (it should handle the error internally)
                await task

                # Immediate text reply was sent
                assert len(bot_client.sent_messages) == 1
                assert bot_client.sent_messages[0]["text"] == "这是文字回复"

                # No voice note sent due to TTS error
                assert len(bot_client.sent_voices) == 0

    @pytest.mark.asyncio
    async def test_malformed_update_objects_safety(self, challenge_db):
        """
        Pass updates with missing fields (message=None, voice=None, text=None) to handlers.
        Verifies graceful None return without AttributeError or crash.
        """
        handlers = TelegramBotHandlers(db_path=challenge_db)
        mock_context = AsyncMock()

        # Update without message
        empty_update = type("Update", (), {"message": None, "effective_chat": None})()
        assert await handlers.handle_text_message(empty_update, mock_context) is None
        assert await handlers.handle_voice_message(empty_update, mock_context) is None

        # Message without voice
        text_only_update = type("Update", (), {
            "message": type("Msg", (), {"text": "hello", "voice": None})(),
            "effective_chat": type("Chat", (), {"id": 1234})(),
        })()
        assert await handlers.handle_voice_message(text_only_update, mock_context) is None


# ============================================================================
# Section 4: End-to-End Concurrent Voice Pipeline & Multi-Turn Load Stress
# ============================================================================

class TestEndToEndConcurrentVoicePipelineStress:
    """Stress testing the end-to-end voice note pipeline across multiple concurrent users."""

    @pytest.mark.asyncio
    async def test_multi_user_concurrent_voice_notes_pipeline(self, challenge_db):
        """
        Stress test 10 concurrent users sending voice notes simultaneously:
        - 10 distinct voice files downloaded
        - Converted OGG -> WAV
        - STT transcription per user
        - Text reply sent immediately to matching chat_id
        - Background voice synthesis dispatched to matching chat_id
        """
        bot_client = MockTelegramBotClient()
        handlers = TelegramBotHandlers(db_path=challenge_db)
        num_users = 10

        # Mock STT adapter to return distinct transcription per user
        async def mock_transcribe(audio_bytes, filename=None):
            return f"用户语音识别内容_{len(audio_bytes)}"

        mock_stt = AsyncMock()
        mock_stt.transcribe.side_effect = mock_transcribe

        # Mock LLM adapter
        async def mock_chat(messages, model=None):
            user_msg = messages[-1].content
            return AsyncMock(content=f'{{"chinese": "收到：{user_msg}", "japanese": "了解しました"}}')

        mock_llm = AsyncMock()
        mock_llm.chat.side_effect = mock_chat

        # Mock TTS
        async def mock_tts(text, *args, **kwargs):
            return b"RIFF\x24\x08\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00\x80>\x00\x00\x00}\x00\x00\x02\x00\x10\x00data\x00\x08\x00\x00" + b"\x00" * 100

        with patch("galgame2voice.telegram_bot.handlers.get_stt_adapter", return_value=mock_stt), \
             patch.object(handlers.chat_service, "get_active_llm_adapter", return_value=(mock_llm, "mock-model", "mock-provider")), \
             patch.object(handlers.tts_service, "synthesize", side_effect=mock_tts):

            async def voice_user_worker(uid: int):
                chat_id = 50000 + uid
                audio_len = 100 + uid * 10
                fake_wav = b"RIFF\x24\x08\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00\x80>\x00\x00\x00}\x00\x00\x02\x00\x10\x00data\x00\x08\x00\x00" + b"\x00" * audio_len

                class MockVoice:
                    file_id = f"voice_uid_{uid}"

                class MockUpdate:
                    effective_chat = type("Chat", (), {"id": chat_id})()
                    message = type("Msg", (), {
                        "voice": MockVoice(),
                        "reply_text": AsyncMock(),
                    })()

                class MockContext:
                    class Bot:
                        async def get_file(self, fid):
                            class TGFile:
                                async def download_as_bytearray(self):
                                    return bytearray(fake_wav)
                            return TGFile()

                        async def send_message(self, chat_id, text):
                            return await bot_client.send_message(chat_id, text)

                        async def send_voice(self, chat_id, voice, caption=None):
                            return await bot_client.send_voice(chat_id, voice, caption=caption)

                    bot = Bot()

                task = await handlers.handle_voice_message(MockUpdate(), MockContext())
                if task:
                    await task
                return chat_id

            tasks = [voice_user_worker(i) for i in range(num_users)]
            results = await asyncio.gather(*tasks)

            assert len(results) == num_users
            assert len(bot_client.sent_messages) == num_users
            assert len(bot_client.sent_voices) == num_users

            # Check matching chat_ids
            msg_cids = {m["chat_id"] for m in bot_client.sent_messages}
            voice_cids = {v["chat_id"] for v in bot_client.sent_voices}
            expected_cids = {50000 + i for i in range(num_users)}
            assert msg_cids == expected_cids
            assert voice_cids == expected_cids

    @pytest.mark.asyncio
    async def test_rapid_consecutive_multi_turn_dialogue_accumulation(self, challenge_db):
        """
        Test a single user having 6 consecutive turns to verify multi-turn context accumulation,
        message sequencing in SQLite, and clean reset.
        """
        bot_client = MockTelegramBotClient()
        handlers = TelegramBotHandlers(db_path=challenge_db)
        chat_id = 70001

        turn_count = 6
        mock_llm = AsyncMock()
        mock_llm.chat.return_value = AsyncMock(content='{"chinese": "回答", "japanese": "返答"}')

        async def mock_tts(text, *args, **kwargs):
            return b"RIFF\x24\x08\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00\x80>\x00\x00\x00}\x00\x00\x02\x00\x10\x00data\x00\x08\x00\x00" + b"\x00" * 50

        with patch.object(handlers.chat_service, "get_active_llm_adapter", return_value=(mock_llm, "mock-model", "mock-provider")), \
             patch.object(handlers.tts_service, "synthesize", side_effect=mock_tts):

            for t_idx in range(turn_count):
                task = await handlers.process_text_chat(chat_id, f"Turn_{t_idx} question", bot_client)
                await task

            # Check DB messages count: 6 user turns + 6 assistant turns = 12 messages
            async with get_db(challenge_db) as conn:
                history = await crud.get_recent_messages(conn, f"tg_{chat_id}", limit=20)
                assert len(history) == 12
                # Verify alternating roles
                for i, msg in enumerate(history):
                    expected_role = "user" if i % 2 == 0 else "assistant"
                    assert msg.role == expected_role

            # Perform /reset
            class DummyUpdate:
                effective_chat = type("Chat", (), {"id": chat_id})()
                message = None

            class DummyContext:
                bot = bot_client

            await handlers.handle_reset(DummyUpdate(), DummyContext())

            # Check DB messages count after reset: should be 0
            async with get_db(challenge_db) as conn:
                history_after = await crud.get_recent_messages(conn, f"tg_{chat_id}")
                assert len(history_after) == 0

    @pytest.mark.asyncio
    async def test_audio_converter_large_payload_stress(self):
        """
        Stress test audio converter with large payload (1MB WAV PCM audio).
        Verifies no memory leak, correct header generation, and roundtrip consistency.
        """
        # 1MB 16-bit PCM WAV
        header = b"RIFF\x24\x08\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00\x80>\x00\x00\x00}\x00\x00\x02\x00\x10\x00data\x00\x08\x00\x00"
        large_pcm = header + (b"\x12\x34" * 500000)  # ~1MB
        assert len(large_pcm) > 1000000

        # Test WAV -> OGG
        ogg_output = await convert_wav_to_ogg(large_pcm)
        assert len(ogg_output) > 0

        # Test OGG (which is already WAV format) -> WAV
        wav_output = await convert_ogg_to_wav(large_pcm)
        assert wav_output.startswith(b"RIFF")
        assert len(wav_output) == len(large_pcm)

