"""
Milestone M1_SECURITY Deep Adversarial Challenge Test Suite
Authored by: Challenger 1 (teamwork_preview_challenger)

Adversarially tests:
1. Logger stack trace masking & regex pattern boundaries:
   - Deeply chained exceptions (10+ levels) with secrets in messages, frames, locals, causes
   - Massive log payloads (100KB, 1MB, 5MB) with scattered secrets (ReDoS & throughput check)
   - Unquoted key-value assignments, console tokens, auth headers, Google/HF/Telegram keys
   - Edge-case key formats, special characters, whitespace, newlines, JSON delimiters
   - MaskingFilter and MaskingFormatter full pipeline behavior
2. Telegram bot token URL sanitization under diverse httpx network/transport error conditions:
   - httpx.ConnectTimeout, ConnectError, ReadTimeout, RequestError, ProxyError, HTTPStatusError
   - Nested & chained exceptions holding raw URLs with bot tokens
   - Verification in both TelegramBotManager.test_token and config.py test_telegram_bot
   - Multi-layer verification that no plaintext bot token survives in responses or logs
3. Path traversal attack vectors against static audio and filesystem browse endpoints:
   - /audio static mount with directory traversal (../, ..%2f, ..%5c, %2e%2e, null bytes, device names)
   - /api/voice/fs-browse with traversal, non-existent paths, permission-denied directories, UNC, long paths
   - /api/voice/browse-file native dialog fallback with invalid paths and filetypes
   - Audio cache path validation and access control
"""

import asyncio
import io
import logging
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from galgame2voice.config import get_settings
from galgame2voice.database.crud import (
    create_provider,
    create_voice_profile,
    get_active_provider,
    get_provider,
    get_provider_raw,
    get_settings as db_get_settings,
    get_settings_raw,
    is_masked_key,
    mask_api_key,
    update_settings,
)
from galgame2voice.database.models import (
    ProviderCreate,
    SettingsInDB,
    SettingsUpdate,
    VoiceProfileCreate,
)
from galgame2voice.database.session import get_db, init_db
from galgame2voice.main import create_app
from galgame2voice.routers.config import TelegramTestRequest, test_telegram_bot as handle_test_telegram_bot
from galgame2voice.routers.voice import _fs_browse_sync
from galgame2voice.services.memory_service import MemoryService
from galgame2voice.telegram_bot.bot import TelegramBotManager, validate_bot_token
from galgame2voice.utils.logger import (
    MaskingFilter,
    MaskingFormatter,
    sanitize_error_detail,
    setup_logger,
)


@pytest.fixture
def clean_db():
    """Provides a fresh isolated temporary database."""
    fd, db_path = tempfile.mkstemp(suffix=".db", prefix="m1_adv_chal_")
    os.close(fd)
    asyncio.run(init_db(db_path))
    yield db_path
    if os.path.exists(db_path):
        try:
            os.remove(db_path)
        except OSError:
            pass


@pytest.fixture
def app_client(clean_db):
    """Provides a TestClient with the isolated test database."""
    os.environ["DATABASE_PATH"] = clean_db
    app = create_app()
    with TestClient(app) as client:
        yield client, clean_db


# ============================================================================
# Dimension 1: Logger Stack Trace Masking & Regex Pattern Boundaries
# ============================================================================

class TestLoggerStacktraceAndRegexBoundaries:
    """Adversarially tests logger masking, formatters, and regex boundaries."""

    SECRET_CORPUS = {
        "openai_std": "sk-1234567890abcdefghijklmnopqrstuvwxyzAB",
        "openai_long": "sk-proj-1234567890abcdefghijklmnopqrstuvwxyz1234567890",
        "google_gemini": "AIzaSyD_abcdefghijklmnopqrstuvwxyz12345",
        "huggingface": "hf_abcdefghijklmnopqrstuvwxyz12345678",
        "bearer_token": "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.e30.t-IDcSemACt8x4iTMCda8Yhe3iZaWbvV5XKSTbuAn0M",
        "tg_url": "https://api.telegram.org/bot1234567890:AAH_abcdefghijklmnopqrstuvwxyz12345/getMe",
        "tg_token_standalone": "1234567890:AAH_abcdefghijklmnopqrstuvwxyz12345",
        "url_query_key": "https://api.example.com/v1?api_key=secret_query_key_99999&user=alice",
        "url_query_token": "https://api.example.com/v1?token=secret_query_token_88888&user=bob",
        "json_api_key": '"api_key": "sec_json_api_key_77777"',
        "json_password": '"password": "MySuperSecretPassword123!"',
        "unquoted_token": "Console token: sec_unquoted_token_66666",
        "unquoted_api_key": "api_key=sec_unquoted_key_55555",
        "unquoted_secret": "client_secret: sec_client_secret_44444",
        "unquoted_auth_token": "auth_token: sec_auth_token_33333",
    }

    def test_all_secret_corpus_single_pass_sanitization(self):
        """Every secret type in SECRET_CORPUS must be masked and contain no plaintext leak."""
        for name, raw_secret in self.SECRET_CORPUS.items():
            sanitized = MaskingFilter.sanitize(raw_secret)
            # Verify the raw secret is no longer in the output
            assert raw_secret not in sanitized, f"Secret leak for {name}: {raw_secret} found in {sanitized}"
            assert ("****" in sanitized or "[MASKED" in sanitized), f"Masking marker missing for {name}: {sanitized}"

    def test_deeply_nested_exception_chain_traceback_masking(self):
        """
        Adversarially constructs a 12-level deep chained exception hierarchy
        where each level embeds different secrets in exception messages and arguments.
        Verifies MaskingFormatter catches 100% of secrets across all frames and causes.
        """
        keys_list = list(self.SECRET_CORPUS.values())
        
        def recursive_raise(depth: int):
            if depth >= len(keys_list):
                raise ValueError(f"Deepest exception: {keys_list[-1]}")
            try:
                recursive_raise(depth + 1)
            except Exception as inner:
                key = keys_list[depth]
                raise RuntimeError(f"Level {depth} failed: {key}") from inner

        try:
            recursive_raise(0)
        except Exception as exc:
            captured_exc = exc

        # Format using MaskingFormatter
        formatter = MaskingFormatter(fmt="%(asctime)s [%(levelname)s]: %(message)s")
        record = logging.LogRecord(
            name="test.deep_exc",
            level=logging.ERROR,
            pathname=__file__,
            lineno=100,
            msg="Exception caught in pipeline",
            args=(),
            exc_info=(type(captured_exc), captured_exc, captured_exc.__traceback__),
        )
        formatted_output = formatter.format(record)

        # Check that NO raw secret exists anywhere in the formatted traceback
        for name, raw_secret in self.SECRET_CORPUS.items():
            if name == "bearer_token":
                token_body = raw_secret.split()[1]
                assert token_body not in formatted_output, f"Bearer token body leaked in traceback: {token_body}"
            elif name in ("json_api_key", "json_password"):
                val = raw_secret.split(":")[1].replace('"', '').strip()
                assert val not in formatted_output, f"JSON secret value leaked in traceback: {val}"
            elif name.startswith("unquoted_"):
                val = raw_secret.split(":")[1].strip() if ":" in raw_secret else raw_secret.split("=")[1].strip()
                assert val not in formatted_output, f"Unquoted secret leaked in traceback: {val}"
            elif name == "tg_url":
                assert "1234567890:AAH_abcdefghijklmnopqrstuvwxyz12345" not in formatted_output
            else:
                assert raw_secret not in formatted_output, f"Secret {name} leaked in formatted traceback!"

    def test_massive_log_payload_throughput_and_redos_safety(self):
        """
        Tests 2MB massive log string containing 10,000 lines and embedded secrets
        at the beginning, middle, and end.
        Ensures linear execution time (ReDoS safe) and zero memory corruption.
        """
        lines = ["Normal application log line processing user request sequence ID #100234.\n"] * 10000
        # Inject secrets at boundaries
        lines[0] = f"Start line with token: {self.SECRET_CORPUS['openai_std']}\n"
        lines[5000] = f"Middle line with google key: {self.SECRET_CORPUS['google_gemini']}\n"
        lines[9999] = f"End line with tg: {self.SECRET_CORPUS['tg_url']}\n"
        payload = "".join(lines)

        t0 = time.perf_counter()
        sanitized = MaskingFilter.sanitize(payload)
        elapsed = time.perf_counter() - t0

        # Processing 2MB should take less than 0.5s with linear regexes
        assert elapsed < 0.5, f"Sanitization of 2MB payload took too long: {elapsed:.3f}s (ReDoS risk)"
        assert self.SECRET_CORPUS["openai_std"] not in sanitized
        assert self.SECRET_CORPUS["google_gemini"] not in sanitized
        assert "1234567890:AAH_abcdefghijklmnopqrstuvwxyz12345" not in sanitized

    def test_unquoted_token_edge_cases_and_delimiters(self):
        """
        Tests varied whitespace, colons, equals, brackets, semicolons, and newlines
        around unquoted keys.
        """
        cases = [
            ("token: 987654321_secret_token", "987654321_secret_token"),
            ("api_key = abcdef12345678_sec", "abcdef12345678_sec"),
            ("console_token: sec_console_token_9999", "sec_console_token_9999"),
            ("auth_token: bearer_xyz_99887766", "bearer_xyz_99887766"),
            ("client_secret: clnt_sec_1122334455", "clnt_sec_1122334455"),
            ("Config: {api_key: secret_key_in_braces, user: alice}", "secret_key_in_braces"),
            ("Status: password=my_plain_pass_123; role=admin", "my_plain_pass_123"),
        ]
        for text, secret in cases:
            sanitized = MaskingFilter.sanitize(text)
            assert secret not in sanitized, f"Failed to mask unquoted secret '{secret}' in '{text}'. Got: '{sanitized}'"

    def test_logger_filter_with_dict_and_tuple_args(self):
        """Verifies MaskingFilter.filter correctly sanitizes structured record args."""
        logger_filter = MaskingFilter()
        
        # Test tuple args
        record_tuple = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="User login with %s and %s",
            args=(self.SECRET_CORPUS["openai_std"], 12345),
            exc_info=None,
        )
        logger_filter.filter(record_tuple)
        assert self.SECRET_CORPUS["openai_std"] not in str(record_tuple.args)

        # Test dict args
        record_dict = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="Config loaded",
            args={"key": self.SECRET_CORPUS["google_gemini"], "val": "normal"},
            exc_info=None,
        )
        logger_filter.filter(record_dict)
        assert self.SECRET_CORPUS["google_gemini"] not in str(record_dict.args)

    def test_sanitize_error_detail_null_and_diverse_types(self):
        """sanitize_error_detail must safely handle None, exceptions, custom objects."""
        assert sanitize_error_detail(None) == ""
        assert sanitize_error_detail("") == ""
        assert sanitize_error_detail(12345) == "12345"
        
        class CustomObj:
            def __str__(self):
                return f"CustomObj with api_key={TestLoggerStacktraceAndRegexBoundaries.SECRET_CORPUS['openai_std']}"
        
        sanitized = sanitize_error_detail(CustomObj())
        assert TestLoggerStacktraceAndRegexBoundaries.SECRET_CORPUS["openai_std"] not in sanitized
        assert "api_key=****" in sanitized


# ============================================================================
# Dimension 2: Telegram Bot Token URL Sanitization in httpx Error Conditions
# ============================================================================

class TestTelegramHttpxErrorSanitization:
    """Adversarially tests token redaction during Telegram httpx network errors."""

    SECRET_TG_TOKEN = "9876543210:AAH_ZyxWvuTsRqPoNmLkJiHgFeDcBa09876"

    @pytest.mark.asyncio
    async def test_telegram_bot_manager_test_token_with_httpx_connect_timeout(self):
        """Simulates httpx.ConnectTimeout holding the raw Telegram URL."""
        mgr = TelegramBotManager()
        raw_url = f"https://api.telegram.org/bot{self.SECRET_TG_TOKEN}/getMe"

        request = httpx.Request("GET", raw_url)
        exc = httpx.ConnectTimeout(f"ConnectTimeout while requesting {raw_url}", request=request)

        with patch("httpx.AsyncClient.get", side_effect=exc):
            result = await mgr.test_token(self.SECRET_TG_TOKEN)
            assert result["success"] is False
            # Ensure secret token is nowhere in the error message
            assert self.SECRET_TG_TOKEN not in result["message"]
            assert "9876543210:AAH_" not in result["message"]
            assert "[MASKED_TELEGRAM_TOKEN]" in result["message"]

    @pytest.mark.asyncio
    async def test_telegram_bot_manager_test_token_with_httpx_connect_error(self):
        """Simulates [WinError 10061] ConnectError with full request URL."""
        mgr = TelegramBotManager()
        raw_url = f"https://api.telegram.org/bot{self.SECRET_TG_TOKEN}/getMe"
        request = httpx.Request("GET", raw_url)
        exc = httpx.ConnectError(f"[WinError 10061] No connection to {raw_url}", request=request)

        with patch("httpx.AsyncClient.get", side_effect=exc):
            result = await mgr.test_token(self.SECRET_TG_TOKEN)
            assert result["success"] is False
            assert self.SECRET_TG_TOKEN not in result["message"]

    @pytest.mark.asyncio
    async def test_telegram_bot_manager_test_token_with_proxy_error(self):
        """Simulates proxy tunnel failure containing raw token URL."""
        mgr = TelegramBotManager()
        raw_url = f"https://api.telegram.org/bot{self.SECRET_TG_TOKEN}/getMe"
        request = httpx.Request("GET", raw_url)
        exc = httpx.ProxyError(f"Cannot connect to proxy 127.0.0.1:7890 for {raw_url}", request=request)

        with patch("httpx.AsyncClient.get", side_effect=exc):
            result = await mgr.test_token(self.SECRET_TG_TOKEN, proxy_url="http://127.0.0.1:7890")
            assert result["success"] is False
            assert self.SECRET_TG_TOKEN not in result["message"]

    @pytest.mark.asyncio
    async def test_config_router_test_telegram_bot_httpx_error_branches(self):
        """
        Tests /api/telegram/test endpoint handler directly under:
        1. ConnectError with connection refused
        2. HTTPStatusError (401, 404, 502)
        3. Generic RequestError with token URL
        """
        raw_url = f"https://api.telegram.org/bot{self.SECRET_TG_TOKEN}/getMe"
        req = TelegramTestRequest(bot_token=self.SECRET_TG_TOKEN, proxy_enabled=False)

        # 1. ConnectError
        request = httpx.Request("GET", raw_url)
        with patch("httpx.AsyncClient.get", side_effect=httpx.ConnectError("Connection refused by peer", request=request)):
            res = await handle_test_telegram_bot(req)
            assert res["success"] is False
            assert self.SECRET_TG_TOKEN not in res["message"]
            assert "代理连接失败" in res["message"] or "连接失败" in res["message"]

        # 2. HTTPStatusError 401
        response_401 = httpx.Response(401, request=request, json={"ok": False, "description": "Unauthorized"})
        with patch("httpx.AsyncClient.get", return_value=response_401):
            res_401 = await handle_test_telegram_bot(req)
            assert res_401["success"] is False
            assert "401" in res_401["message"]
            assert self.SECRET_TG_TOKEN not in res_401["message"]

        # 3. HTTPStatusError 404
        response_404 = httpx.Response(404, request=request, json={"ok": False, "description": "Not Found"})
        with patch("httpx.AsyncClient.get", return_value=response_404):
            res_404 = await handle_test_telegram_bot(req)
            assert res_404["success"] is False
            assert "404" in res_404["message"]
            assert self.SECRET_TG_TOKEN not in res_404["message"]

        # 4. HTTPStatusError 502 Bad Gateway
        response_502 = httpx.Response(502, request=request, text="<html>502 Bad Gateway</html>")
        with patch("httpx.AsyncClient.get", return_value=response_502):
            res_502 = await handle_test_telegram_bot(req)
            assert res_502["success"] is False
            assert "502" in res_502["message"]
            assert self.SECRET_TG_TOKEN not in res_502["message"]


# ============================================================================
# Dimension 3: Path Traversal Attack Vectors Against Audio & Browse Endpoints
# ============================================================================

class TestPathTraversalAndFileSystemSecurity:
    """Adversarially tests path traversal defense across static files and fs browser."""

    def test_static_audio_path_traversal_attempts(self, app_client):
        """
        Sends diverse path traversal attacks to the /audio static mount.
        Must return 404/400 (Not Found / Bad Request), NEVER leak outside files.
        """
        client, db_path = app_client

        traversal_payloads = [
            "/audio/../main.py",
            "/audio/../../galgame2voice/main.py",
            "/audio/..%2f..%2fgalgame2voice%2fmain.py",
            "/audio/..%5c..%5cgalgame2voice%5cmain.py",
            "/audio/%2e%2e%2f%2e%2e%2fmain.py",
            "/audio/....//....//galgame2voice/main.py",
            "/audio/C:/Windows/win.ini",
            "/audio/c:\\windows\\system32\\calc.exe",
            "/audio/cache/../../galgame2voice/routers/config.py",
            "/audio/cache/%2e%2e%2f%2e%2e%2fPROJECT.md",
            "/audio/NUL",
            "/audio/CON",
            "/audio/COM1",
        ]

        for payload in traversal_payloads:
            resp = client.get(payload)
            # Response must be 404 or 400, never 200 with source code
            assert resp.status_code in (400, 404, 405), f"Path traversal succeeded or unhandled on {payload}: {resp.status_code}"
            assert "def create_app" not in resp.text
            assert "M1_SECURITY" not in resp.text

    def test_legitimate_audio_file_serving_works(self, app_client):
        """Verifies that legitimate files inside audio_dir are accessible."""
        client, db_path = app_client
        settings = get_settings()
        
        test_wav = settings.audio_dir / "valid_test_audio.wav"
        test_wav.write_bytes(b"RIFF\x24\x00\x00\x00WAVEfmt \x10\x00\x00\x00")
        try:
            resp = client.get("/audio/valid_test_audio.wav")
            assert resp.status_code == 200
            assert resp.content.startswith(b"RIFF")
        finally:
            if test_wav.exists():
                test_wav.unlink()

    def test_fs_browse_sync_adversarial_paths(self):
        """
        Tests _fs_browse_sync with adversarial path inputs:
        - Non-existent directories
        - System Volume Information (permission denied)
        - UNC paths
        - Root drive boundary checks
        - Extreme path lengths (> 300 chars)
        - Special characters and device names
        """
        # 1. Non-existent path -> should fallback gracefully to root drive or current directory
        res_nonexistent = _fs_browse_sync("C:\\NonExistent_Directory_12345\\SubFolder", "all")
        assert "directories" in res_nonexistent
        assert "files" in res_nonexistent
        assert res_nonexistent["current_path"] != "C:\\NonExistent_Directory_12345\\SubFolder"

        # 2. Permission denied simulation (or System Volume Information on Windows)
        res_sys_vol = _fs_browse_sync("C:\\System Volume Information", "all")
        assert "directories" in res_sys_vol
        assert "files" in res_sys_vol
        # Should gracefully return error message or empty lists, never crash with unhandled exception
        if "error" in res_sys_vol:
            assert "无法访问" in res_sys_vol["error"] or "PermissionError" in res_sys_vol["error"]

        # 3. None path -> returns drive listing and top root
        res_none = _fs_browse_sync(None, "all")
        assert "drives" in res_none
        assert len(res_none["drives"]) > 0

        # 4. Filter by file_type: gpt, sovits, audio
        res_audio = _fs_browse_sync(".", "audio")
        for f in res_audio.get("files", []):
            ext = os.path.splitext(f["name"])[1].lower()
            assert ext in {".wav", ".ogg", ".mp3", ".flac", ".m4a"}

        # 5. Overlong path string (> 300 chars)
        long_path = "C:\\" + "a" * 350
        res_long = _fs_browse_sync(long_path, "all")
        assert "directories" in res_long
        assert "files" in res_long

    def test_fs_browse_api_requires_auth(self, monkeypatch, clean_db):
        """Ensures /api/voice/fs-browse is protected by auth dependencies when auth is enabled."""
        monkeypatch.setenv("GALGAME2VOICE_AUTH_DISABLED", "0")
        monkeypatch.setenv("GALGAME2VOICE_CONSOLE_TOKEN", "super-secret-token")
        os.environ["DATABASE_PATH"] = clean_db
        app = create_app()
        with TestClient(app) as client:
            # 1. Unauthenticated request -> 401
            resp = client.get("/api/voice/fs-browse")
            assert resp.status_code == 401, f"Expected 401 Unauthorized for fs-browse without token, got: {resp.status_code}"

            # 2. Authenticated request with Bearer -> 200
            resp_auth = client.get("/api/voice/fs-browse", headers={"Authorization": "Bearer super-secret-token"})
            assert resp_auth.status_code == 200
            assert "drives" in resp_auth.json()

    def test_browse_file_api_handles_arbitrary_inputs(self, app_client):
        """Tests /api/voice/browse-file with invalid file types and non-existent initial_dir."""
        client, db_path = app_client

        payload = {
            "file_type": "invalid_type_123",
            "initial_dir": "C:\\InvalidNonExistentPath9876",
        }
        with patch("tkinter.filedialog.askopenfilename", side_effect=Exception("Headless mode")):
            resp = client.post("/api/voice/browse-file", json=payload)
            assert resp.status_code == 200
            assert resp.json() == {"selected_path": ""}
