"""
Adversarial Security & Zero-Leakage Hardening Challenge Test Suite.
Authored by Challenger M1_2 for Milestone M1_SECURITY.

Tests:
1. SSE Error Event formatting, exception masking & zero-leakage stream verification.
2. Database Telemetry Path Relative Normalization across OS/drive path formats.
"""

import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import AsyncGenerator, Dict, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from galgame2voice.routers.chat import sse_event_formatter
from galgame2voice.routers.health import DatabaseTelemetry, system_status, router as health_router
from galgame2voice.services.chat_service import ChatService, StreamingBilingualParser
from galgame2voice.utils.logger import MaskingFilter, MaskingFormatter, sanitize_error_detail
from galgame2voice.config import get_settings


# ============================================================================
# Section 2: SSE Error Event Formatting & Zero-Leakage Stream Masking
# ============================================================================

class TestSseErrorFormattingAndZeroLeakage:
    """Adversarial testing of SSE error events and zero-leakage masking."""

    @pytest.mark.asyncio
    async def test_sse_event_formatter_normal_stream(self):
        """Verify sse_event_formatter outputs compliant SSE stream formatting."""
        async def mock_events():
            yield {"event": "text", "data": {"delta_chinese": "你好", "emotion": "happy"}}
            yield {"event": "audio_chunk", "data": {"index": 0, "audio_url": "/audio/cache/1.wav"}}
            yield {"event": "done", "data": {"chinese": "你好", "japanese": "こんにちは"}}

        lines = []
        async for chunk in sse_event_formatter(mock_events()):
            lines.append(chunk)

        assert len(lines) == 3
        assert lines[0] == 'event: text\ndata: {"delta_chinese": "你好", "emotion": "happy"}\n\n'
        assert lines[1] == 'event: audio_chunk\ndata: {"index": 0, "audio_url": "/audio/cache/1.wav"}\n\n'
        assert lines[2] == 'event: done\ndata: {"chinese": "你好", "japanese": "こんにちは"}\n\n'

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "secret_exception,expected_masked_substring,leaked_secret",
        [
            # OpenAI API Key
            (
                ValueError("OpenAI API request failed with key sk-proj-1234567890abcdef1234567890abcdef: quota exceeded"),
                "sk-proj-****cdef",
                "sk-proj-1234567890abcdef1234567890abcdef",
            ),
            # Anthropic API Key
            (
                RuntimeError("Anthropic API Error: sk-ant-api03-abcdefghijklmnopqrstuvwxyz1234567890 invalid auth"),
                "sk-ant-****7890",
                "sk-ant-api03-abcdefghijklmnopqrstuvwxyz1234567890",
            ),
            # Google Gemini Key
            (
                ConnectionError("Failed connecting to https://generativelanguage.googleapis.com/v1beta/models?key=AIzaSyD123456789012345678901234567890123"),
                "AIzaSyD****0123",
                "AIzaSyD123456789012345678901234567890123",
            ),
            # HuggingFace Token
            (
                Exception("HF Hub 401: hf_abcdef1234567890abcdef12345678901234 unauthorized"),
                "hf_abc****1234",
                "hf_abcdef1234567890abcdef12345678901234",
            ),
            # Telegram Bot Token URL
            (
                "httpx.ConnectError: https://api.telegram.org/bot123456789:ABCdef-ghijk_1234567890lmnopqrst/getMe failed",
                "https://api.telegram.org/bot[MASKED_TELEGRAM_TOKEN]/getMe",
                "123456789:ABCdef-ghijk_1234567890lmnopqrst",
            ),
            # Bearer Token
            (
                "401 Unauthorized: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIn0",
                "Bearer [MASKED_TOKEN]",
                "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIn0",
            ),
            # Query Param Secrets
            (
                "Request failed: https://my-llm.internal/v1/chat?api_key=my_secret_token_12345&secret=topsecret_shhh",
                "?api_key=[MASKED]&secret=[MASKED]",
                "my_secret_token_12345",
            ),
        ],
    )
    async def test_sse_event_formatter_catches_and_masks_stream_exceptions(
        self, secret_exception, expected_masked_substring, leaked_secret
    ):
        """
        Adversarial test: when an exception containing sensitive credentials
        is raised inside an SSE generator, sse_event_formatter MUST catch it,
        emit an `event: error` JSON event, and NEVER leak the raw credentials.
        """
        async def failing_stream():
            yield {"event": "text", "data": {"delta_chinese": "开始"}}
            raise RuntimeError(str(secret_exception))

        events = []
        async for chunk in sse_event_formatter(failing_stream()):
            events.append(chunk)

        assert len(events) == 2
        assert "event: text" in events[0]

        error_chunk = events[1]
        assert error_chunk.startswith("event: error\ndata: ")
        assert error_chunk.endswith("\n\n")

        # Parse data JSON
        json_str = error_chunk.replace("event: error\ndata: ", "").strip()
        data = json.loads(json_str)
        assert "error" in data
        err_msg = data["error"]

        # ZERO LEAKAGE ASSERTIONS
        assert leaked_secret not in err_msg, f"LEAKAGE DETECTED! Secret '{leaked_secret}' found in SSE error payload: {err_msg}"
        assert expected_masked_substring in err_msg or "[MASKED" in err_msg or "****" in err_msg

    @pytest.mark.asyncio
    async def test_sse_event_formatter_handles_cancelled_error(self):
        """Verify client disconnection / asyncio.CancelledError is handled gracefully without yielding errors."""
        async def cancelled_stream():
            yield {"event": "text", "data": {"delta_chinese": "你好"}}
            raise asyncio.CancelledError()

        events = []
        async for chunk in sse_event_formatter(cancelled_stream()):
            events.append(chunk)

        assert len(events) == 1
        assert "event: text" in events[0]
        # No error event emitted on client cancellation

    @pytest.mark.asyncio
    async def test_chat_service_stream_chat_zero_leakage_on_tts_failure(self):
        """
        Adversarial test: when TTS synthesis throws an exception with API secrets,
        ChatService.stream_chat MUST emit an audio_chunk_error event with sanitized details.
        """
        mock_tts = MagicMock()
        secret_key = "sk-proj-99998888777766665555444433332222"
        mock_tts.synthesize_to_file = AsyncMock(
            side_effect=RuntimeError(f"TTS Engine internal error with auth token: {secret_key}")
        )
        chat_service = ChatService(tts_service=mock_tts, db_path=":memory:")

        # Mock adapter stream to return a Japanese sentence
        mock_adapter = MagicMock()
        async def mock_stream(*args, **kwargs):
            yield '{"chinese": "你好", "japanese": "こんにちは。'
            yield '"}'
        mock_adapter.stream_chat = mock_stream
        chat_service._get_active_llm_adapter = AsyncMock(return_value=(mock_adapter, "gpt-4o-mini", "openai"))

        events = []
        async for event in chat_service.stream_chat(prompt="你好"):
            events.append(event)

        # Check that audio_chunk_error was emitted
        error_events = [e for e in events if e.get("event") == "audio_chunk_error"]
        assert len(error_events) >= 1, f"Expected audio_chunk_error event, got events: {events}"
        err_data = error_events[0]["data"]
        assert "error" in err_data
        assert secret_key not in err_data["error"], "CRITICAL: Secret key leaked into audio_chunk_error event!"
        assert "****" in err_data["error"] or "[MASKED" in err_data["error"]


# ============================================================================
# Section 3: Database Telemetry Path Relative Normalization
# ============================================================================

class TestDatabaseTelemetryPathNormalization:
    """Adversarial testing of DatabaseTelemetry path normalization across OS platforms."""

    def test_database_telemetry_schema(self):
        """Verify DatabaseTelemetry pydantic model accepts relative path and wal_mode."""
        t = DatabaseTelemetry(status="connected", wal_mode=True, path="data/galgame2voice.db")
        assert t.path == "data/galgame2voice.db"
        assert t.wal_mode is True

    @pytest.mark.parametrize(
        "simulated_project_root,simulated_db_path,expected_relative_path",
        [
            # Standard Windows relative path
            (
                Path("C:/Users/JIYIF/Documents/New project/galgame2voice"),
                Path("C:/Users/JIYIF/Documents/New project/galgame2voice/data/galgame2voice.db"),
                "data/galgame2voice.db",
            ),
            # Custom subdirectory in project
            (
                Path("C:/Users/JIYIF/Documents/New project/galgame2voice"),
                Path("C:/Users/JIYIF/Documents/New project/galgame2voice/custom_data/app.db"),
                "custom_data/app.db",
            ),
            # Linux / POSIX project root
            (
                Path("/home/user/workspace/galgame2voice"),
                Path("/home/user/workspace/galgame2voice/data/galgame2voice.db"),
                "data/galgame2voice.db",
            ),
            # Windows different drive letter (outside project root) -> fallback
            (
                Path("C:/Users/JIYIF/galgame2voice"),
                Path("D:/external_data/test_db.sqlite3"),
                "data/test_db.sqlite3",
            ),
            # POSIX /tmp/ directory (outside project root) -> fallback
            (
                Path("/app/galgame2voice"),
                Path("/tmp/pytest-of-root/temp.db"),
                "data/temp.db",
            ),
            # UNC network share (outside project root) -> fallback
            pytest.param(
                Path("C:/galgame2voice"),
                Path(r"\\remote-server\share\data.db"),
                "data/data.db",
                marks=pytest.mark.skipif(
                    sys.platform != "win32",
                    reason="UNC path parsing is Windows-specific; POSIX treats backslashes as literal characters",
                ),
            ),
        ],
    )
    def test_path_normalization_logic(
        self, simulated_project_root, simulated_db_path, expected_relative_path
    ):
        """
        Verify path normalization logic:
        1. If db_path is within project_root, returns relative_to(project_root).as_posix()
        2. If outside project_root, safely falls back to f"{data_dir_name}/{db_path.name}"
        3. Never leaks full host machine drive letters or user home directory.
        """
        from galgame2voice.routers.health import _resolve_rel_db_path
        rel_db_path = _resolve_rel_db_path(simulated_db_path, simulated_project_root, "data")

        assert rel_db_path == expected_relative_path
        assert not rel_db_path.startswith("C:")
        assert not rel_db_path.startswith("D:")
        assert not rel_db_path.startswith("/home/")
        assert not rel_db_path.startswith("/Users/")
        assert "\\" not in rel_db_path  # Must be normalized POSIX forward slashes

    @pytest.mark.parametrize("configured_folder", ["absolute", "../private"])
    def test_external_data_directory_never_leaks_its_location(self, tmp_path, configured_folder):
        from galgame2voice.routers.health import _resolve_rel_db_path
        external = tmp_path / "private" / "history.db"
        folder = str(external.parent) if configured_folder == "absolute" else configured_folder
        assert _resolve_rel_db_path(external, tmp_path / "app", folder) == "data/history.db"

    async def test_live_system_status_endpoint_returns_normalized_path(self):
        """
        Empirically invoke GET /api/system/status with an ASGI client and verify
        that response['database']['path'] is strictly relative.
        """
        app = FastAPI()
        app.include_router(health_router)

        # Bypass auth for status test
        with patch("galgame2voice.routers.health.require_auth", return_value="admin"):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                response = await client.get("/api/system/status", headers={"X-Auth-Token": "test_token"})

            assert response.status_code == 200, f"Health endpoint returned status {response.status_code}: {response.text}"
            data = response.json()
            assert "database" in data
            db_info = data["database"]
            assert "path" in db_info
            path_val = db_info["path"]

            # Assert path is relative POSIX
            from galgame2voice.config import get_settings
            settings = get_settings()
            # The isolation fixture deliberately keeps the DB outside the repo.
            # The endpoint must conceal that absolute temp path using its fallback.
            if settings.db_path.is_relative_to(settings.project_root):
                expected = settings.db_path.relative_to(settings.project_root).as_posix()
            else:
                expected = f"data/{settings.db_path.name}"
            assert path_val == expected
            assert "C:" not in path_val
            assert "Users" not in path_val
            assert "\\" not in path_val
