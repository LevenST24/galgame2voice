"""Database and disk failures on save requests explain themselves in Chinese (r67 wrong-input 7)."""

import errno
import sqlite3
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from galgame2voice.database import crud
from galgame2voice.database.session import init_db
from galgame2voice.main import create_app
from galgame2voice.utils.logger import (
    DATABASE_BUSY_MESSAGE,
    DISK_FULL_MESSAGE,
    NO_WRITE_PERMISSION_MESSAGE,
    describe_database_write_error,
)


@pytest.fixture
async def app_client(isolate_test_database):
    await init_db(isolate_test_database)
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as client:
        yield client


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (sqlite3.OperationalError("database is locked"), DATABASE_BUSY_MESSAGE),
        (sqlite3.OperationalError("unable to open database file"), NO_WRITE_PERMISSION_MESSAGE),
        (PermissionError(errno.EACCES, "Permission denied"), NO_WRITE_PERMISSION_MESSAGE),
        (OSError(errno.ENOSPC, "No space left on device"), DISK_FULL_MESSAGE),
        (sqlite3.OperationalError("no such column: foo"), None),
        (ValueError("database is locked"), None),
        (ValueError("no space left on device"), None),
    ],
)
def test_describe_database_write_error(exc, expected):
    assert describe_database_write_error(exc) == expected


@pytest.mark.asyncio
async def test_locked_database_on_settings_save_returns_a_chinese_503(app_client):
    with patch.object(crud, "update_settings", AsyncMock(side_effect=sqlite3.OperationalError("database is locked"))):
        resp = await app_client.post("/api/config", json={"audio_retention_minutes": 60})
    assert resp.status_code == 503
    assert resp.json()["detail"] == DATABASE_BUSY_MESSAGE


@pytest.mark.asyncio
async def test_full_disk_on_session_save_returns_a_chinese_507(app_client):
    with patch.object(crud, "upsert_session", AsyncMock(side_effect=sqlite3.OperationalError("database or disk is full"))):
        resp = await app_client.post("/api/chat/sessions", json={"id": "s_disk", "title": "T"})
    assert resp.status_code == 507
    assert resp.json()["detail"] == DISK_FULL_MESSAGE


@pytest.mark.asyncio
async def test_provider_save_storage_error_is_not_reported_as_invalid_parameters(app_client):
    with patch.object(crud, "update_provider", AsyncMock(side_effect=sqlite3.OperationalError("unable to open database file"))):
        resp = await app_client.post("/api/providers", json={"id": "openai", "chat_model": "gpt-4o"})
    assert resp.status_code == 503
    assert resp.json()["detail"] == NO_WRITE_PERMISSION_MESSAGE
    with patch.object(crud, "create_provider", AsyncMock(side_effect=sqlite3.OperationalError("database is locked"))):
        resp = await app_client.post("/api/providers", json={"id": "brandnew"})
    assert resp.status_code == 503
    assert resp.json()["detail"] == DATABASE_BUSY_MESSAGE


@pytest.mark.asyncio
async def test_other_database_errors_keep_the_generic_500(app_client):
    with patch.object(crud, "upsert_session", AsyncMock(side_effect=sqlite3.OperationalError("no such column: foo"))):
        resp = await app_client.post("/api/chat/sessions", json={"id": "s_other", "title": "T"})
    assert resp.status_code == 500
    assert resp.json()["detail"] == "Internal Server Error"


@pytest.mark.asyncio
async def test_provider_validation_error_with_storage_text_stays_422(app_client):
    resp = await app_client.post("/api/providers", json={"id": "openai", "reasoning_effort": "read-only file system"})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_activate_unseeded_preset_reports_storage_error_not_404(app_client):
    assert (await app_client.delete("/api/providers/openai")).status_code == 200
    with patch.object(crud, "create_provider", AsyncMock(side_effect=sqlite3.OperationalError("database is locked"))):
        resp = await app_client.post("/api/providers/openai/activate")
    assert resp.status_code == 503
    assert resp.json()["detail"] == DATABASE_BUSY_MESSAGE


@pytest.mark.asyncio
async def test_voice_profile_writes_report_locked_database_as_503(app_client):
    locked = AsyncMock(side_effect=sqlite3.OperationalError("database is locked"))
    payload = {
        "name": "音色", "gpt_weights_path": "weights/a.ckpt", "sovits_weights_path": "weights/a.pth",
        "ref_audio_path": "audio/a.wav", "prompt_text": "こんにちは",
    }
    for attr, method, url, body in [
        ("create_voice_profile", "post", "/api/voice/profiles", payload),
        ("update_voice_profile", "put", "/api/voice/profiles/1", {"name": "新名称"}),
        ("delete_voice_profile", "delete", "/api/voice/profiles/1", None),
    ]:
        with patch.object(crud, attr, locked):
            resp = await app_client.request(method, url, json=body)
        assert resp.status_code == 503 and resp.json()["detail"] == DATABASE_BUSY_MESSAGE
