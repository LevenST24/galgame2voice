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
        (PermissionError(errno.EACCES, "Permission denied"), NO_WRITE_PERMISSION_MESSAGE),
        (OSError(errno.ENOSPC, "No space left on device"), DISK_FULL_MESSAGE),
        (sqlite3.OperationalError("no such column: foo"), None),
        (ValueError("database is locked"), None),
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
    with patch.object(crud, "upsert_session", AsyncMock(side_effect=OSError(errno.ENOSPC, "No space left on device"))):
        resp = await app_client.post("/api/chat/sessions", json={"id": "s_disk", "title": "T"})
    assert resp.status_code == 507
    assert resp.json()["detail"] == DISK_FULL_MESSAGE


@pytest.mark.asyncio
async def test_provider_save_storage_error_is_not_reported_as_invalid_parameters(app_client):
    with patch.object(crud, "update_provider", AsyncMock(side_effect=PermissionError(errno.EACCES, "Permission denied"))):
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
