"""
Adversarial Challenge Test Suite 2 for Galgame2Voice Optimization.

Focus areas:
1. LLM Streaming & Non-Streaming Retry Edge Cases (HTTP 429/503 resilience, 401/403 fail-fast).
2. Two-Tier TTS Cache Concurrency (<1ms hits without GPU lock contention) & 0-byte Corruption Auto-Recovery.
3. Telegram Bot Parameter & Security Gating (Unconfigured provider block, /nickname SQLite persistence & affection context, Zero-leak logger masking).
"""

import asyncio
import io
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from galgame2voice.adapters.base import ChatMessage
from galgame2voice.adapters.llm.anthropic_adapter import AnthropicAdapter
from galgame2voice.adapters.llm.openai_adapter import OpenAICompatibleLLMAdapter
from galgame2voice.database import crud
from galgame2voice.database.models import CharacterAffectionUpdate, ProviderCreate, ProviderUpdate, SettingsUpdate
from galgame2voice.database.session import get_db, init_db
from galgame2voice.services.affection_service import AffectionService
from galgame2voice.services.chat_service import ChatService
from galgame2voice.services.gpt_sovits_client import GptSovitsClient
from galgame2voice.services.memory_service import MemoryService
from galgame2voice.services.session_manager import SessionManager
from galgame2voice.services.tts_cache_manager import TtsCacheManager
from galgame2voice.services.tts_service import TtsService
from galgame2voice.telegram_bot.handlers import TelegramBotHandlers
from galgame2voice.utils.logger import MaskingFilter, setup_logger


# ============================================================================
# 1. LLM Retry Edge Cases (429/503 recovery & 401/403 fail-fast)
# ============================================================================

_ORIG_HTTPX_ASYNC_CLIENT = httpx.AsyncClient


class MockTransportWithFailures(httpx.AsyncBaseTransport):
    """
    Simulates a sequence of HTTP error responses followed by a successful SSE stream or JSON response.
    """

    def __init__(self, failure_statuses: List[int], success_content: str, is_stream: bool = True, retry_after: str = "0.01"):
        self.failure_statuses = list(failure_statuses)
        self.success_content = success_content
        self.is_stream = is_stream
        self.retry_after = retry_after
        self.attempt_count = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.attempt_count += 1
        if self.failure_statuses:
            status = self.failure_statuses.pop(0)
            headers = {"retry-after": self.retry_after} if status in (429, 503) else {}
            if status == 429:
                body = b'{"error": {"message": "Rate limit exceeded (429)", "type": "rate_limit"}}'
            elif status == 503:
                body = b'{"error": {"message": "Service unavailable (503)", "type": "service_unavailable"}}'
            elif status in (401, 403):
                body = b'{"error": {"message": "Invalid authentication credentials (401/403)", "type": "auth_error"}}'
            else:
                body = f'{{"error": "HTTP {status}"}}'.encode("utf-8")
            return httpx.Response(status_code=status, headers=headers, content=body, request=request)

        # Success response
        if self.is_stream:
            headers = {"content-type": "text/event-stream"}
            return httpx.Response(
                status_code=200,
                headers=headers,
                content=self.success_content.encode("utf-8"),
                request=request,
            )
        else:
            headers = {"content-type": "application/json"}
            return httpx.Response(
                status_code=200,
                headers=headers,
                content=self.success_content.encode("utf-8"),
                request=request,
            )


@pytest.mark.asyncio
class TestLlmRetryEdgeCases:
    """Rigorous challenge tests for LLM streaming and non-streaming retry resilience."""

    async def test_openai_stream_chat_recovers_from_429_before_yielding(self):
        """Simulates consecutive HTTP 429 rate limits, verifies backoff recovery before yielding SSE tokens."""
        sse_body = (
            'data: {"choices":[{"delta":{"content":"\\u4f60\\u597d"}}]}\n\n'
            'data: {"choices":[{"delta":{"content":"\\uff0c\\u590f\\u76ee"}}]}\n\n'
            'data: [DONE]\n\n'
        )
        transport = MockTransportWithFailures([429, 429], success_content=sse_body, is_stream=True, retry_after="0.01")
        adapter = OpenAICompatibleLLMAdapter(
            api_key="sk-test-valid-key-12345",
            base_url="https://api.openai.com/v1",
            max_retries=3,
            base_delay=0.01,
        )

        def _client_factory(*args, **kwargs):
            clean_kwargs = {k: v for k, v in kwargs.items() if k != "transport"}
            return _ORIG_HTTPX_ASYNC_CLIENT(transport=transport, **clean_kwargs)

        with patch("httpx.AsyncClient", _client_factory):
            messages = [ChatMessage(role="user", content="hello")]
            tokens = []
            async for token in adapter.stream_chat(messages, model="gpt-4o", base_delay=0.01):
                tokens.append(token)

            assert transport.attempt_count == 3  # 2 failed attempts (429) + 1 successful attempt
            assert tokens == ["你好", "，夏目"]

    async def test_openai_stream_chat_recovers_from_503_service_unavailable(self):
        """Simulates HTTP 503 service unavailable, verifies retry and stream recovery."""
        sse_body = (
            'data: {"choices":[{"delta":{"content":"Resilient"}}]}\n\n'
            'data: {"choices":[{"delta":{"content":" Stream"}}]}\n\n'
            'data: [DONE]\n\n'
        )
        transport = MockTransportWithFailures([503], success_content=sse_body, is_stream=True, retry_after="0.01")
        adapter = OpenAICompatibleLLMAdapter(
            api_key="sk-test-valid-key-12345",
            base_url="https://api.openai.com/v1",
            max_retries=2,
            base_delay=0.01,
        )

        def _client_factory(*args, **kwargs):
            clean_kwargs = {k: v for k, v in kwargs.items() if k != "transport"}
            return _ORIG_HTTPX_ASYNC_CLIENT(transport=transport, **clean_kwargs)

        with patch("httpx.AsyncClient", _client_factory):
            messages = [ChatMessage(role="user", content="hello")]
            tokens = []
            async for token in adapter.stream_chat(messages, model="gpt-4o", base_delay=0.01):
                tokens.append(token)

            assert transport.attempt_count == 2
            assert tokens == ["Resilient", " Stream"]

    async def test_openai_stream_chat_fails_fast_on_401_403(self):
        """Verifies that HTTP 401/403 fails fast on attempt 1 without entering retry loops."""
        transport = MockTransportWithFailures([401, 401, 401], success_content="", is_stream=True)
        adapter = OpenAICompatibleLLMAdapter(
            api_key="sk-invalid-key-9999",
            base_url="https://api.openai.com/v1",
            max_retries=5,
            base_delay=0.01,
        )

        def _client_factory(*args, **kwargs):
            clean_kwargs = {k: v for k, v in kwargs.items() if k != "transport"}
            return _ORIG_HTTPX_ASYNC_CLIENT(transport=transport, **clean_kwargs)

        with patch("httpx.AsyncClient", _client_factory):
            messages = [ChatMessage(role="user", content="hello")]
            with pytest.raises(ValueError, match="Authentication error"):
                async for _ in adapter.stream_chat(messages, model="gpt-4o"):
                    pass

            # Crucial check: only 1 attempt made, failed immediately!
            assert transport.attempt_count == 1

    async def test_openai_non_streaming_chat_recovers_from_429_and_fails_fast_on_401(self):
        """Verifies non-streaming chat completion recovers from 429 and fails fast on 401."""
        json_body = json.dumps({
            "choices": [{"message": {"role": "assistant", "content": "Recovery Non-Stream"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 10, "total_tokens": 15},
        })
        # 1. Test 429 recovery
        transport_429 = MockTransportWithFailures([429], success_content=json_body, is_stream=False, retry_after="0.01")
        adapter = OpenAICompatibleLLMAdapter(
            api_key="sk-test-valid-key-12345",
            base_url="https://api.openai.com/v1",
            max_retries=2,
            base_delay=0.01,
        )

        def _client_factory_429(*args, **kwargs):
            clean_kwargs = {k: v for k, v in kwargs.items() if k != "transport"}
            return _ORIG_HTTPX_ASYNC_CLIENT(transport=transport_429, **clean_kwargs)

        with patch("httpx.AsyncClient", _client_factory_429):
            resp = await adapter.chat([ChatMessage(role="user", content="hello")], model="gpt-4o", base_delay=0.01)
            assert resp.content == "Recovery Non-Stream"
            assert transport_429.attempt_count == 2

        # 2. Test 401 fail-fast
        transport_401 = MockTransportWithFailures([401, 401], success_content="", is_stream=False)
        def _client_factory_401(*args, **kwargs):
            clean_kwargs = {k: v for k, v in kwargs.items() if k != "transport"}
            return _ORIG_HTTPX_ASYNC_CLIENT(transport=transport_401, **clean_kwargs)

        with patch("httpx.AsyncClient", _client_factory_401):
            with pytest.raises(ValueError, match="Authentication error"):
                await adapter.chat([ChatMessage(role="user", content="hello")], model="gpt-4o")
            assert transport_401.attempt_count == 1

    async def test_anthropic_stream_chat_recovers_and_fails_fast(self):
        """Verifies Anthropic adapter stream_chat behaves identically: 429 recovery & 401 fail fast."""
        sse_body = (
            'data: {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Claude"}}\n\n'
            'data: {"type": "content_block_delta", "delta": {"type": "text_delta", "text": " Response"}}\n\n'
        )
        # 1. Test 429 recovery
        transport_429 = MockTransportWithFailures([429], success_content=sse_body, is_stream=True, retry_after="0.01")
        adapter = AnthropicAdapter(
            api_key="sk-ant-test-key-12345",
            max_retries=2,
            base_delay=0.01,
        )

        def _client_factory_429(*args, **kwargs):
            clean_kwargs = {k: v for k, v in kwargs.items() if k != "transport"}
            return _ORIG_HTTPX_ASYNC_CLIENT(transport=transport_429, **clean_kwargs)

        with patch("httpx.AsyncClient", _client_factory_429):
            tokens = []
            async for t in adapter.stream_chat([ChatMessage(role="user", content="hi")], base_delay=0.01):
                tokens.append(t)
            assert transport_429.attempt_count == 2
            assert tokens == ["Claude", " Response"]

        # 2. Test 401 fail fast
        transport_401 = MockTransportWithFailures([401], success_content="", is_stream=True)
        def _client_factory_401(*args, **kwargs):
            clean_kwargs = {k: v for k, v in kwargs.items() if k != "transport"}
            return _ORIG_HTTPX_ASYNC_CLIENT(transport=transport_401, **clean_kwargs)

        with patch("httpx.AsyncClient", _client_factory_401):
            with pytest.raises(ValueError, match="auth failed|authentication failed"):
                async for _ in adapter.stream_chat([ChatMessage(role="user", content="hi")]):
                    pass
            assert transport_401.attempt_count == 1


# ============================================================================
# 2. Two-Tier TTS Cache Concurrency & 0-Byte Corruption Recovery
# ============================================================================

@pytest.mark.asyncio
class TestTtsCacheConcurrencyAndCorruption:
    """Stress tests for concurrent cache hit latency (<1ms) and 0-byte corrupt cache self-healing."""

    async def test_concurrent_cache_hits_latency_under_1ms_and_no_gpu_lock(self, tmp_path):
        """Verifies 50 concurrent cache hits execute with <1ms latency without GPU lock contention."""
        db_file = tmp_path / "test_tts_cache.db"
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)

        await init_db(str(db_file))

        cache_mgr = TtsCacheManager(cache_dir=cache_dir, db_path=str(db_file), max_mem_entries=100)
        mock_client = MagicMock(spec=GptSovitsClient)
        mock_client.lock = asyncio.Lock()
        mock_client.current_refer_audio = "natsume_ref.wav"
        mock_client.current_refer_text = "refer text"
        mock_client.current_refer_language = "ja"
        mock_client.synthesize = AsyncMock(return_value=b"RIFF\x24\x00\x00\x00WAVEfmt \x10\x00\x00\x00data\x00\x00\x00\x00")
        tts_service = TtsService(client=mock_client, audio_dir=tmp_path, cache_manager=cache_mgr)

        sample_text = "おはようございます、今日も一日頑張りましょう！"
        opts = {"ref_audio_path": "natsume_ref.wav", "prompt_text": "refer text"}

        # 1. Warm up the cache with initial synthesis
        initial_bytes = await tts_service.synthesize(sample_text, options=opts, use_cache=True)
        assert len(initial_bytes) > 0
        assert mock_client.synthesize.call_count == 1

        # 2. Benchmark 100 individual cache hits latency distribution directly
        cache_key, _, _ = cache_mgr.compute_cache_key(sample_text, options=opts)
        latencies = []
        for _ in range(100):
            t0 = time.perf_counter()
            hit = await cache_mgr.get(cache_key)
            t_diff = (time.perf_counter() - t0) * 1000.0
            latencies.append(t_diff)
            assert hit is not None
            assert hit[0] == initial_bytes

        avg_latency = sum(latencies) / len(latencies)
        # Verify in-memory cache latency is well below 1.0ms (typically 0.01 - 0.05ms)
        assert avg_latency < 1.0, f"Average cache hit latency {avg_latency:.4f}ms exceeded 1.0ms"

        # 3. Execute 50 concurrent requests against the cached text via tts_service
        tasks = [tts_service.synthesize(sample_text, options=opts, use_cache=True) for _ in range(50)]
        results = await asyncio.gather(*tasks)

        # Assertions
        assert len(results) == 50
        for res in results:
            assert res == initial_bytes
        # Synthesis should NOT have been called again (0 additional GPU calls)
        assert mock_client.synthesize.call_count == 1
        assert not mock_client.lock.locked()

    async def test_zero_byte_corrupt_cache_file_auto_purged_and_resynthesized(self, tmp_path):
        """Verifies that 0-byte corrupted cache files on disk are detected, purged, and safely re-synthesized."""
        db_file = tmp_path / "test_corrupt.db"
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)

        await init_db(str(db_file))

        cache_mgr = TtsCacheManager(cache_dir=cache_dir, db_path=str(db_file))
        valid_wav = b"RIFF\x24\x00\x00\x00WAVEfmt \x10\x00\x00\x00data\x00\x00\x00\x00_VALID_WAV_AUDIO"
        mock_client = MagicMock(spec=GptSovitsClient)
        mock_client.lock = asyncio.Lock()
        mock_client.current_refer_audio = "test_ref.wav"
        mock_client.current_refer_text = "test prompt"
        mock_client.current_refer_language = "ja"
        mock_client.synthesize = AsyncMock(return_value=valid_wav)

        tts_service = TtsService(client=mock_client, audio_dir=tmp_path, cache_manager=cache_mgr)
        test_text = "壊れたキャッシュファイルの自動修復テスト"
        opts = {"ref_audio_path": "test_ref.wav", "prompt_text": "test prompt"}

        cache_key, clean_text, params_hash = cache_mgr.compute_cache_key(test_text, options=opts)
        corrupt_file = cache_dir / f"{cache_key}.wav"
        # Write 0-byte corrupt file
        corrupt_file.write_bytes(b"")
        assert corrupt_file.exists() and corrupt_file.stat().st_size == 0

        # Register metadata in SQLite to simulate a crash during write
        async with get_db(str(db_file)) as conn:
            await crud.upsert_tts_cache_entry(
                conn,
                cache_key=cache_key,
                text=test_text,
                clean_text=clean_text,
                voice_profile_id=1,
                params_hash=params_hash,
                file_path=str(corrupt_file),
                file_size=0,
            )

        # Call synthesize_to_file -> should detect 0-byte corruption, purge it, and re-synthesize
        url_path, file_path, file_size = await tts_service.synthesize_to_file(test_text, options=opts, use_cache=True)

        assert mock_client.synthesize.call_count == 1
        assert file_size == len(valid_wav)
        assert file_path.stat().st_size == len(valid_wav)
        assert file_path.read_bytes() == valid_wav

        # Verify subsequent get succeeds from healthy cache
        hit = await cache_mgr.get(cache_key)
        assert hit is not None
        cached_bytes, _, _ = hit
        assert cached_bytes == valid_wav


# ============================================================================
# 3. Telegram Bot Parameter & Security Gating
# ============================================================================

@pytest.mark.asyncio
class TestTelegramBotParameterAndSecurityGating:
    """Adversarial validation of Telegram unconfigured provider blocking, /nickname persistence, and log masking."""

    async def test_unconfigured_llm_provider_switching_is_blocked_and_alerted(self, tmp_path):
        """Verifies that selecting an LLM provider with an empty API key is strictly blocked and alerted."""
        db_file = tmp_path / "test_tg_gating.db"
        await init_db(str(db_file))
        async with get_db(str(db_file)) as conn:
            # Ensure anthropic provider has empty API key
            await conn.execute("UPDATE providers SET api_key = '', is_active = 0 WHERE id = 'anthropic';")
            # Ensure deepseek is active with a valid key
            await conn.execute("UPDATE providers SET api_key = 'sk-valid-key-9999', is_active = 1 WHERE id = 'deepseek';")
            await crud.set_active_provider(conn, "deepseek")

        handlers = TelegramBotHandlers(db_path=str(db_file))

        # Mock CallbackQuery for switching to unconfigured provider
        mock_query = AsyncMock()
        mock_query.data = "set_model_anthropic"
        mock_query.answer = AsyncMock()
        mock_query.edit_message_text = AsyncMock()

        mock_update = MagicMock()
        mock_update.callback_query = mock_query
        mock_update.effective_chat.id = 99887766

        await handlers.handle_callback_query(mock_update)

        # 1. Verify user received an alert popup
        mock_query.answer.assert_called_once()
        alert_args = mock_query.answer.call_args
        alert_msg = alert_args[0][0] if alert_args[0] else alert_args[1].get("text", "")
        show_alert = alert_args[1].get("show_alert", False)

        assert show_alert is True
        assert "未配置 API Key" in alert_msg or "无法激活" in alert_msg

        # 2. Verify active provider in SQLite was NOT changed
        async with get_db(str(db_file)) as conn:
            active_prov = await crud.get_active_provider(conn)
            assert active_prov.id == "deepseek"
            assert active_prov.id != "anthropic"

    async def test_custom_provider_switching_without_key_is_allowed(self, tmp_path):
        """Verifies that 'custom' local provider switching is allowed even without an API key."""
        db_file = tmp_path / "test_tg_custom.db"
        await init_db(str(db_file))
        async with get_db(str(db_file)) as conn:
            await conn.execute("UPDATE providers SET api_key = '', is_active = 0 WHERE id = 'custom';")

        handlers = TelegramBotHandlers(db_path=str(db_file))
        mock_query = AsyncMock()
        mock_query.data = "set_model_custom"
        mock_query.answer = AsyncMock()
        mock_query.edit_message_text = AsyncMock()

        mock_update = MagicMock()
        mock_update.callback_query = mock_query
        mock_update.effective_chat.id = 11223344

        await handlers.handle_callback_query(mock_update)

        async with get_db(str(db_file)) as conn:
            active_prov = await crud.get_active_provider(conn)
            assert active_prov.id == "custom"

    async def test_nickname_command_persists_to_sqlite_and_updates_affection_context(self, tmp_path):
        """Verifies that /nickname saves custom nickname to SQLite and injects it into LLM affection prompt."""
        db_file = tmp_path / "test_tg_nick.db"
        await init_db(str(db_file))
        async with get_db(str(db_file)) as conn:
            # Seed active voice profile
            await crud.get_active_voice_profile(conn)

        chat_service = ChatService(db_path=str(db_file))
        handlers = TelegramBotHandlers(chat_service=chat_service, db_path=str(db_file))

        test_chat_id = 88776655
        custom_name = "昂晴君"

        # 1. Execute /nickname command
        mock_message = AsyncMock()
        mock_message.text = f"/nickname {custom_name}"
        mock_message.reply_text = AsyncMock()

        mock_update = MagicMock()
        mock_update.message = mock_message
        mock_update.effective_chat.id = test_chat_id

        reply = await handlers.handle_nickname(mock_update)
        assert custom_name in reply
        assert ("更新" in reply or "称呼" in reply)

        # 2. Verify persistence in SQLite character_affection table
        async with get_db(str(db_file)) as conn:
            aff = await crud.get_or_create_character_affection(conn, user_id=str(test_chat_id), character_id=1)
            assert aff.custom_nickname == custom_name

            # Ensure Telegram session is initialized for this chat_id
            session_id = f"tg_{test_chat_id}"
            await crud.get_or_create_session(conn, session_id, channel="telegram", user_id=str(test_chat_id))

            # 3. Verify affection context propagation into ChatService prepare_messages
            messages = await chat_service.prepare_messages(
                conn=conn,
                session_id=session_id,
                user_prompt="你好呀夏目！",
                character_name="四季夏目",
            )

            system_msg = next((m for m in messages if m.role == "system"), None)
            assert system_msg is not None
            assert f"称呼玩家为：{custom_name}" in system_msg.content

    async def test_nickname_command_empty_args_shows_usage_help(self, tmp_path):
        """Verifies that calling /nickname without args outputs usage instructions."""
        db_file = tmp_path / "test_tg_nick_empty.db"
        await init_db(str(db_file))
        handlers = TelegramBotHandlers(db_path=str(db_file))

        mock_message = AsyncMock()
        mock_message.text = "/nickname   "
        mock_message.reply_text = AsyncMock()

        mock_update = MagicMock()
        mock_update.message = mock_message
        mock_update.effective_chat.id = 99112233

        reply = await handlers.handle_nickname(mock_update)
        assert "用法" in reply
        assert "/nickname <你的称呼>" in reply

    async def test_api_key_masking_filter_sanitizes_all_sensitive_tokens(self):
        """Verifies that MaskingFilter catches OpenAI, Bearer, Telegram tokens, and URL params without leakage."""
        filter_instance = MaskingFilter()

        # Sensitive strings to sanitize
        samples = [
            ("Connecting with key sk-proj-1234567890abcdef1234567890 to API", "sk-pro****7890"),
            ("Authorization: Bearer mySecretToken1234567890xyz", "Bearer [MASKED_TOKEN]"),
            ("Bot initialized with token 1234567890:ABCDefgh-123456789012345678901234567890", "1234567890:[MASKED_TELEGRAM_TOKEN]"),
            ('{"api_key": "super_secret_key_value_999"}', '{"api_key": "****"}'),
            ("https://api.provider.com/v1/chat?api_key=secretKey123456", "https://api.provider.com/v1/chat?api_key=[MASKED]"),
        ]

        for raw, expected_substr in samples:
            sanitized = filter_instance.sanitize(raw)
            assert expected_substr in sanitized
            assert "1234567890abcdef1234567890" not in sanitized
            assert "super_secret_key_value_999" not in sanitized
            assert "secretKey123456" not in sanitized

        # Verify LogRecord sanitization in Python logging pipeline
        record = logging.LogRecord(
            name="test_logger",
            level=logging.INFO,
            pathname=__file__,
            lineno=10,
            msg="Sending request with key: %s and header %s",
            args=("sk-proj-9876543210abcdef9876543210", "Bearer secretToken12345678"),
            exc_info=None,
        )

        filter_instance.filter(record)
        assert "9876543210abcdef" not in str(record.args)
        assert "secretToken12345678" not in str(record.args)
