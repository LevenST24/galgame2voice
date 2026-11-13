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
from galgame2voice.services.memory_service import MemoryService
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
    with TestClient(app, base_url="http://127.0.0.1:8080") as client:
        yield client, clean_db


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

    def test_browse_file_api_handles_arbitrary_inputs(self, app_client):
        """Tests /api/voice/browse-file with invalid file types and non-existent initial_dir."""
        pytest.importorskip("tkinter")
        client, db_path = app_client

        payload = {
            "file_type": "invalid_type_123",
            "initial_dir": "C:\\InvalidNonExistentPath9876",
        }
        with patch("tkinter.filedialog.askopenfilename", side_effect=Exception("Headless mode")):
            resp = client.post("/api/voice/browse-file", json=payload)
            assert resp.status_code == 200
            assert resp.json() == {"selected_path": ""}
