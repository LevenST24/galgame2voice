"""
Unit and integration tests for Phase 4 Optimizations:
1. LLM Adapter Network Resilience, Exponential Backoff, Jitter & Retry Policies
2. Streaming SSE Parser Malformed Chunk & Multi-Line Fragment Tolerance
3. SQLite Schema Versioning (PRAGMA user_version) & Idempotent Auto-Migrations
"""

import asyncio
import email.utils
import json
import os
import tempfile
import time
from typing import AsyncIterator, List
from unittest.mock import AsyncMock, patch, MagicMock

import pytest
import aiosqlite
import httpx

from galgame2voice.adapters.base import (
    ChatMessage,
    LLMResponse,
    TRANSIENT_STATUS_CODES,
    TRANSIENT_NETWORK_EXCEPTIONS,
    calculate_backoff_delay,
    parse_retry_after,
    extract_stream_token,
    parse_sse_lines,
)
from galgame2voice.adapters.llm.openai_adapter import OpenAICompatibleLLMAdapter
from galgame2voice.adapters.llm.anthropic_adapter import AnthropicAdapter
from galgame2voice.database.session import (
    get_db,
    init_db,
    get_schema_version,
    set_schema_version,
)
from galgame2voice.database.crud import (
    CURRENT_SCHEMA_VERSION,
    run_schema_migrations,
)


# ============================================================================
# 1. LLM Adapter Network Resilience & Retries
# ============================================================================

class TestLLMRetryPolicies:
    """Tests for standardized exponential backoff, jitter, and Retry-After parsing."""

    def test_transient_status_codes(self):
        expected_codes = {408, 429, 500, 502, 503, 504}
        for code in expected_codes:
            assert code in TRANSIENT_STATUS_CODES

    def test_parse_retry_after_numeric(self):
        assert parse_retry_after({"retry-after": "5"}) == 5.0
        assert parse_retry_after({"Retry-After": "2.5"}) == 2.5
        assert parse_retry_after(None) is None
        assert parse_retry_after({}) is None
        assert parse_retry_after({"retry-after": "invalid_val"}) is None

    def test_parse_retry_after_http_date(self):
        future_time = time.time() + 120
        date_str = email.utils.formatdate(future_time, usegmt=True)
        val = parse_retry_after({"retry-after": date_str})
        assert val is not None
        assert 110.0 < val < 130.0

    def test_calculate_backoff_delay_exponential_and_jitter(self):
        for attempt in range(5):
            delay = calculate_backoff_delay(attempt, base_delay=0.1, jitter_min=0.05, jitter_max=0.15)
            min_expected = 0.1 * (2 ** attempt) + 0.05
            max_expected = 0.1 * (2 ** attempt) + 0.15
            assert min_expected <= delay <= max_expected

    def test_calculate_backoff_delay_with_retry_after(self):
        delay = calculate_backoff_delay(0, base_delay=1.0, retry_after=15.0, jitter_min=0.1, jitter_max=0.4)
        assert 15.1 <= delay <= 15.4

    def test_calculate_backoff_delay_max_ceiling(self):
        delay = calculate_backoff_delay(20, base_delay=10.0, retry_after=120.0, max_delay=60.0)
        assert delay <= 60.0

    @pytest.mark.asyncio
    async def test_openai_adapter_chat_retry_on_429(self):
        adapter = OpenAICompatibleLLMAdapter(api_key="sk-test", base_url="https://api.openai.com/v1")
        attempts = 0

        async def mock_post(url, json, headers):
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                return httpx.Response(429, headers={"retry-after": "0.01"}, text="Too Many Requests")
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "Success after 429"}}], "usage": {"total_tokens": 10}},
            )

        with patch("httpx.AsyncClient.post", side_effect=mock_post):
            resp = await adapter.chat(
                messages=[ChatMessage(role="user", content="ping")],
                model="gpt-4o",
                max_retries=3,
                base_delay=0.01,
            )
            assert resp.content == "Success after 429"
            assert attempts == 3

    @pytest.mark.asyncio
    async def test_openai_adapter_chat_retry_on_network_timeout(self):
        adapter = OpenAICompatibleLLMAdapter(api_key="sk-test", base_url="https://api.openai.com/v1")
        attempts = 0

        async def mock_post(url, json, headers):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise httpx.ConnectTimeout("Connection timed out")
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "Success after timeout"}}]},
            )

        with patch("httpx.AsyncClient.post", side_effect=mock_post):
            resp = await adapter.chat(
                messages=[ChatMessage(role="user", content="ping")],
                model="gpt-4o",
                max_retries=2,
                base_delay=0.01,
            )
            assert resp.content == "Success after timeout"
            assert attempts == 2

    @pytest.mark.asyncio
    async def test_anthropic_adapter_chat_retry_on_503(self):
        adapter = AnthropicAdapter(api_key="sk-ant-test", base_url="https://api.anthropic.com/v1")
        attempts = 0

        async def mock_post(url, json, headers):
            nonlocal attempts
            attempts += 1
            if attempts < 2:
                return httpx.Response(503, text="Service Unavailable")
            return httpx.Response(
                200,
                json={"content": [{"type": "text", "text": "Claude back online"}]},
            )

        with patch("httpx.AsyncClient.post", side_effect=mock_post):
            resp = await adapter.chat(
                messages=[ChatMessage(role="user", content="Hi")],
                model="claude-sonnet-4-20250514",
                max_retries=2,
                base_delay=0.01,
            )
            assert resp.content == "Claude back online"
            assert attempts == 2


# ============================================================================
# 2. Streaming SSE Parser Resilience
# ============================================================================

class TestStreamingSSEParser:
    """Tests for SSE line parsing, fragmented payloads, malformed chunks, and error events."""

    def test_extract_stream_token_openai(self):
        chunk = {"choices": [{"delta": {"content": "Hello"}}]}
        assert extract_stream_token(chunk) == "Hello"

        # Direct text choices fallback
        chunk_text = {"choices": [{"text": "World"}]}
        assert extract_stream_token(chunk_text) == "World"

        # Empty delta
        chunk_empty = {"choices": [{"delta": {}}]}
        assert extract_stream_token(chunk_empty) is None

    def test_extract_stream_token_anthropic(self):
        chunk = {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "AnthropicToken"}}
        assert extract_stream_token(chunk) == "AnthropicToken"

        chunk_start = {"type": "message_start"}
        assert extract_stream_token(chunk_start) is None

    def test_extract_stream_token_error_payload(self):
        chunk_err = {"error": {"message": "Rate limit exceeded", "type": "rate_limit_error"}}
        with pytest.raises(RuntimeError, match="Rate limit exceeded"):
            extract_stream_token(chunk_err)

    @pytest.mark.asyncio
    async def test_parse_sse_lines_standard_stream(self):
        lines = [
            'data: {"choices": [{"delta": {"content": "One"}}]}',
            'data: {"choices": [{"delta": {"content": " "}}]}',
            'data: {"choices": [{"delta": {"content": "Two"}}]}',
            'data: [DONE]',
        ]

        async def line_gen():
            for line in lines:
                yield line

        tokens = []
        async for t in parse_sse_lines(line_gen()):
            tokens.append(t)

        assert "".join(tokens) == "One Two"

    @pytest.mark.asyncio
    async def test_parse_sse_lines_fragmented_and_comments(self):
        # Line fragmented across two data lines with comment keepalive
        lines = [
            ": ping keepalive",
            'data: {"choices": [{"delta": ',
            'data: {"content": "Fragmented"}}]}',
            "",
            ': another keepalive',
            'data: {"choices": [{"delta": {"content": " Resolved"}}]}',
            'data: [DONE]',
        ]

        async def line_gen():
            for line in lines:
                yield line

        tokens = []
        async for t in parse_sse_lines(line_gen()):
            tokens.append(t)

        assert "".join(tokens) == "Fragmented Resolved"

    @pytest.mark.asyncio
    async def test_parse_sse_lines_malformed_chunk_tolerance(self):
        # Stream has malformed JSON in chunk 2, should skip chunk 2 without crashing
        lines = [
            'data: {"choices": [{"delta": {"content": "Start "}}]}',
            'data: {corrupted json {[[[',
            'data: {"choices": [{"delta": {"content": "End"}}]}',
            'data: [DONE]',
        ]

        async def line_gen():
            for line in lines:
                yield line

        tokens = []
        async for t in parse_sse_lines(line_gen()):
            tokens.append(t)

        assert "".join(tokens) == "Start End"


# ============================================================================
# 3. Database Schema Versioning & Migrations
# ============================================================================

class TestDatabaseSchemaMigrations:
    """Tests for SQLite PRAGMA user_version tracking, incremental migrations, and idempotency."""

    @pytest.fixture
    async def temp_db_path(self):
        fd, path = tempfile.mkstemp(suffix=".db", prefix="test_phase4_mig_")
        os.close(fd)
        yield path
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass

    @pytest.mark.asyncio
    async def test_fresh_database_initialization_sets_version(self, temp_db_path):
        async with get_db(temp_db_path) as conn:
            v0 = await get_schema_version(conn)
            assert v0 == 0

        await init_db(temp_db_path)

        async with get_db(temp_db_path) as conn:
            v_final = await get_schema_version(conn)
            assert v_final == CURRENT_SCHEMA_VERSION
            assert v_final >= 4

    @pytest.mark.asyncio
    async def test_schema_migrations_idempotency(self, temp_db_path):
        # Run init_db multiple times consecutively
        await init_db(temp_db_path)
        await init_db(temp_db_path)
        await init_db(temp_db_path)

        async with get_db(temp_db_path) as conn:
            v = await get_schema_version(conn)
            assert v == CURRENT_SCHEMA_VERSION

            # Verify core tables and indexes exist
            cur = await conn.execute("SELECT COUNT(*) FROM providers;")
            count = (await cur.fetchone())[0]
            assert count >= 8

            cur = await conn.execute("SELECT COUNT(*) FROM voice_profiles WHERE id = 1;")
            assert (await cur.fetchone())[0] == 1

            cur = await conn.execute("SELECT COUNT(*) FROM settings WHERE id = 1;")
            assert (await cur.fetchone())[0] == 1

    @pytest.mark.asyncio
    async def test_migration_preserves_existing_user_data(self, temp_db_path):
        # 1. Initialize schema
        await init_db(temp_db_path)

        # 2. Insert custom session and message
        async with get_db(temp_db_path) as conn:
            await conn.execute("INSERT INTO sessions (id, channel, user_id) VALUES ('sess_custom', 'web', 'u1');")
            await conn.execute(
                "INSERT INTO messages (session_id, role, content_chinese, content_japanese) "
                "VALUES ('sess_custom', 'user', '你好夏目', 'こんにちは');"
            )
            await conn.commit()

        # 3. Simulate rolling back user_version to 2 and re-running migrations
        async with get_db(temp_db_path) as conn:
            await set_schema_version(conn, 2)
            v_low = await get_schema_version(conn)
            assert v_low == 2

        # 4. Re-run migrations via init_db
        await init_db(temp_db_path)

        # 5. Verify version upgraded to current and custom data is intact
        async with get_db(temp_db_path) as conn:
            v_upgraded = await get_schema_version(conn)
            assert v_upgraded == CURRENT_SCHEMA_VERSION

            cur = await conn.execute("SELECT content_chinese FROM messages WHERE session_id = 'sess_custom';")
            row = await cur.fetchone()
            assert row is not None
            assert row[0] == "你好夏目"

    @pytest.mark.asyncio
    async def test_auto_heal_preserves_bundled_paths_and_repairs_missing(self, temp_db_path):
        from galgame2voice.database.crud import auto_heal_voice_profiles

        # 1. Initialize schema
        await init_db(temp_db_path)

        async with get_db(temp_db_path) as conn:
            await conn.execute(
                "INSERT INTO voice_profiles (name, gpt_weights_path, sovits_weights_path, ref_audio_path, prompt_text) "
                "VALUES ('Bundled Voice', 'gpt.ckpt', 'sovits.pth', 'audio/references/natsume/gentle.ogg', 'Hello');"
            )
            await conn.execute(
                "INSERT INTO voice_profiles (name, gpt_weights_path, sovits_weights_path, ref_audio_path, prompt_text) "
                "VALUES ('Broken Voice', 'gpt.ckpt', 'sovits.pth', 'audio/non_existent_file.ogg', 'Hello');"
            )
            await conn.commit()

            # Run auto-healing
            healed = await auto_heal_voice_profiles(conn)
            assert healed >= 1

            cur = await conn.execute("SELECT ref_audio_path FROM voice_profiles WHERE name = 'Bundled Voice';")
            row = await cur.fetchone()
            assert row[0] == "audio/references/natsume/gentle.ogg"

            cur = await conn.execute("SELECT ref_audio_path FROM voice_profiles WHERE name = 'Broken Voice';")
            row = await cur.fetchone()
            assert "gentle.ogg" in row[0] or "nat002_032.ogg" in row[0]


# ============================================================================
# 4. Deep Edge Cases & Packaging Resilience Tests
# ============================================================================

class TestPhase4DeepEdgeCases:
    """Additional edge case tests for null errors, streaming retry timeouts, and packaging."""

    def test_extract_stream_token_null_or_false_error(self):
        # Provider chunks containing "error": null or "error": false must not raise
        chunk_null = {"error": None, "choices": [{"delta": {"content": "ValidContent"}}]}
        assert extract_stream_token(chunk_null) == "ValidContent"

        chunk_false = {"error": False, "choices": [{"delta": {"content": "StillValid"}}]}
        assert extract_stream_token(chunk_false) == "StillValid"

    @pytest.mark.asyncio
    async def test_parse_sse_lines_multi_fragment_triplet(self):
        # A single SSE token split across three consecutive data lines
        lines = [
            'data: {"choices": [',
            'data: {"delta": ',
            'data: {"content": "MultiFragmentTriplet"}}]}',
            "",
            'data: [DONE]',
        ]

        async def line_gen():
            for line in lines:
                yield line

        tokens = [t async for t in parse_sse_lines(line_gen())]
        assert "".join(tokens) == "MultiFragmentTriplet"

    @pytest.mark.asyncio
    async def test_stream_chat_early_read_timeout_retries_successfully(self):
        adapter = OpenAICompatibleLLMAdapter(api_key="sk-test", base_url="https://api.openai.com/v1")
        attempt_count = 0

        class MockStreamResponse:
            status_code = 200
            headers = {}

            async def aiter_lines(self):
                nonlocal attempt_count
                attempt_count += 1
                if attempt_count == 1:
                    raise httpx.ReadTimeout("Server timed out before first byte")
                yield 'data: {"choices": [{"delta": {"content": "RecoveredStream"}}]}'
                yield 'data: [DONE]'

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

        with patch("httpx.AsyncClient.stream", return_value=MockStreamResponse()):
            tokens = []
            async for tok in adapter.stream_chat(
                messages=[ChatMessage(role="user", content="hello")],
                model="gpt-4o",
                max_retries=2,
                base_delay=0.01,
            ):
                tokens.append(tok)

            assert "".join(tokens) == "RecoveredStream"
            assert attempt_count == 2

    def test_release_packaging_includes_bundled_reference_audios(self):
        from pathlib import Path
        from scripts.package_release import should_include

        # Bundled character reference audios must be included
        assert should_include(Path("audio/references/natsume/gentle.ogg")) is True
        assert should_include(Path("audio/references/natsume/cool.ogg")) is True
        assert should_include(Path("audio/nat002_032.ogg")) is True
        assert should_include(Path("audio/.keep")) is True

        # Transient generated wav chunks and cache files must be excluded
        assert should_include(Path("audio/cache/some_cache.wav")) is False
        assert should_include(Path("audio/chunk_0_1234.wav")) is False
        assert should_include(Path("audio/full_5678.wav")) is False
        assert should_include(Path("logs/app.log")) is False
