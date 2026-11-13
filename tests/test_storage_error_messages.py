"""Disk-full / read-only storage errors surface a clear Chinese message."""

import errno
import json
import sqlite3
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from galgame2voice.database import crud
from galgame2voice.database.models import MessageCreate
from galgame2voice.database.session import get_db
from galgame2voice.main import create_app
from galgame2voice.utils.logger import sanitize_error_detail

from tests.test_line_audio_regeneration import repair  # noqa: F401


@pytest.mark.parametrize("exc", [
    OSError(errno.ENOSPC, "No space left on device"),
    sqlite3.OperationalError("database or disk is full"),
    "[Errno 28] No space left on device",
    "[Errno 28] 设备上没有空间",
    "There is not enough space on the disk",
    "[WinError 112] 磁盘空间不足。",
    "[WinError 39] 磁盘已满。",
    "[WinError 39] The disk is full.",
    OSError(None, "There is not enough space on the disk", None, 112),
    OSError(None, "The disk is full", None, 39),
])
def test_disk_full_is_described_in_chinese(exc):
    assert "磁盘空间不足" in sanitize_error_detail(exc)


@pytest.mark.parametrize("exc", [
    OSError(errno.EROFS, "Read-only file system"),
    sqlite3.OperationalError("attempt to write a readonly database"),
    "[Errno 30] Read-only file system",
    "[Errno 30] 只读文件系统",
])
def test_read_only_is_described_in_chinese(exc):
    assert "只读" in sanitize_error_detail(exc)


def test_storage_error_keeps_sanitized_original_in_brackets():
    detail = sanitize_error_detail("No space left on device, api_key=abcdef123456")
    assert "磁盘空间不足" in detail and "（No space left on device" in detail
    assert "abcdef123456" not in detail


def test_storage_error_sanitization_is_idempotent():
    once = sanitize_error_detail(OSError(errno.ENOSPC, "No space left on device"))
    twice = sanitize_error_detail(once)
    assert twice == once


def test_other_errors_are_unchanged():
    assert sanitize_error_detail(ValueError("boom")) == "boom"
    assert sanitize_error_detail(PermissionError(13, "Permission denied")) == str(PermissionError(13, "Permission denied"))
    winerr32 = "[WinError 32] The process cannot access the file because it is being used by another process."
    assert sanitize_error_detail(winerr32) == winerr32
    errno13 = "[Errno 13] Permission denied"
    assert sanitize_error_detail(errno13) == errno13


async def test_create_voice_profile_reports_full_disk(monkeypatch):
    monkeypatch.setattr(
        "galgame2voice.routers.voice.crud.create_voice_profile",
        AsyncMock(side_effect=sqlite3.OperationalError("database or disk is full")),
    )
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://127.0.0.1:8080") as client:
        response = await client.post("/api/voice/profiles", json={
            "name": "音色", "gpt_weights_path": "weights/a.ckpt", "sovits_weights_path": "weights/a.pth",
            "ref_audio_path": "audio/a.wav", "prompt_text": "こんにちは",
        })
    assert response.status_code == 400
    assert "磁盘空间不足" in response.json()["detail"]


async def test_regenerate_line_reports_full_disk_instead_of_engine_hint(repair, monkeypatch):  # noqa: F811
    async def full_disk(*args, **kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(repair.service, "synthesize_to_file", full_disk)
    response = await repair.client.post("/api/voice/regenerate-line", json=repair.body)
    assert response.status_code == 507
    detail = response.json()["detail"]
    assert "磁盘空间不足" in detail and "原音频与文字已保留" in detail
    assert "检查语音引擎" not in detail


class TestStorageErrorEndpoints:
    @pytest.mark.parametrize(
        ("error_msg", "expected_keyword"),
        [
            ("database or disk is full", "磁盘空间不足"),
            ("attempt to write a readonly database", "只读"),
        ],
    )
    @pytest.mark.parametrize("is_legacy", [False, True])
    @pytest.mark.parametrize("fail_stage", ["execute", "commit"])
    async def test_regenerate_line_db_storage_error_discards_repair_audio(
        self, repair, monkeypatch, error_msg, expected_keyword, is_legacy, fail_stage,  # noqa: F811
    ):
        import aiosqlite

        if is_legacy:
            async with get_db(repair.db) as conn:
                legacy_msg = await crud.add_message(conn, MessageCreate(
                    session_id="repair-session", role="assistant", content_chinese="旧版中文台词",
                    content_japanese="旧版の日本語セリフ。", audio_url="/audio/old-full.wav", lines=None,
                ))
            body = {
                "session_id": "repair-session",
                "message_id": legacy_msg.id,
                "line_index": 0,
                "text": "旧版の日本語セリフ。",
                "voice_profile_id": repair.profiles[1].id,
            }
            orig_lines = None
            orig_audio_url = "/audio/old-full.wav"
            target_msg_id = legacy_msg.id
        else:
            body = repair.body
            orig_lines = repair.lines
            orig_audio_url = repair.message.audio_url or ""
            target_msg_id = repair.message.id

        if fail_stage == "execute":
            real_execute = aiosqlite.Connection.execute

            def mock_execute(self_conn, sql, parameters=None):
                if "UPDATE messages" in str(sql):
                    raise sqlite3.OperationalError(error_msg)
                return real_execute(self_conn, sql, parameters)

            monkeypatch.setattr(aiosqlite.Connection, "execute", mock_execute)
        else:
            async def mock_commit(self_conn):
                raise sqlite3.OperationalError(error_msg)

            monkeypatch.setattr(aiosqlite.Connection, "commit", mock_commit)

        response = await repair.client.post("/api/voice/regenerate-line", json=body)
        assert response.status_code == 507, response.text
        detail = response.json()["detail"]
        assert expected_keyword in detail
        assert "原音频与文字已保留。" in detail
        assert list((repair.root / "audio").glob("repair_*.wav")) == []

        async with get_db(repair.db) as conn:
            row = await (await conn.execute("SELECT lines_json, audio_url FROM messages WHERE id=?", (target_msg_id,))).fetchone()
            if is_legacy:
                assert row["lines_json"] is None
                assert row["audio_url"] == orig_audio_url
            else:
                assert json.loads(row["lines_json"]) == orig_lines
                assert (row["audio_url"] or "") == orig_audio_url

