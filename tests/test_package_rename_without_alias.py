"""A package whose manifest renames the character (same folder, no alias for the old
name) keeps its voice row, so sessions, affection and memories stay with it."""
import json
import sqlite3

import pytest
from httpx import ASGITransport, AsyncClient

from galgame2voice.config import get_settings
from galgame2voice.database.session import get_database_path, init_db
from galgame2voice.main import create_app
from galgame2voice.services.character_manager import CharacterManager
from test_character_rescan import _install_package


def _rename(pkg_dir, name):
    manifest = pkg_dir / "manifest.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["name"] = name
    manifest.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("GALGAME2VOICE_PROJECT_ROOT", str(tmp_path))
    get_settings.cache_clear()
    CharacterManager.reset_instance()
    return tmp_path


async def _client():
    return AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://127.0.0.1:8080")


@pytest.mark.asyncio
async def test_rename_without_alias_keeps_the_voice_row(project):
    pkg_dir = _install_package(project / "characters", "char_01", "夏目")
    await init_db()
    async with await _client() as client:
        assert (await client.post("/api/characters/rescan")).status_code == 200
        [before] = (await client.get("/api/characters")).json()["characters"]
        session = await client.post("/api/chat/sessions", json={"id": "s1", "title": "t", "voice_profile_id": before["id"]})
        assert session.status_code == 200

        _rename(pkg_dir, "四季夏目")
        assert (await client.post("/api/characters/rescan")).status_code == 200

        chars = (await client.get("/api/characters")).json()["characters"]
        assert [(c["id"], c["name"]) for c in chars] == [(before["id"], "四季夏目")]

        # A second rescan changes nothing.
        assert (await client.post("/api/characters/rescan")).status_code == 200
        assert [(c["id"], c["name"]) for c in (await client.get("/api/characters")).json()["characters"]] == [
            (before["id"], "四季夏目")
        ]


@pytest.mark.asyncio
async def test_two_rows_using_the_package_audio_are_left_alone(project):
    pkg_dir = _install_package(project / "characters", "char_01", "夏目")
    await init_db()
    async with await _client() as client:
        assert (await client.post("/api/characters/rescan")).status_code == 200
        [before] = (await client.get("/api/characters")).json()["characters"]
        db = get_database_path()
        with sqlite3.connect(db) as conn:
            row = conn.execute(
                "SELECT gpt_weights_path, sovits_weights_path, ref_audio_path, prompt_text FROM voice_profiles WHERE id = ?",
                (before["id"],),
            ).fetchone()
            conn.execute(
                "INSERT INTO voice_profiles (name, gpt_weights_path, sovits_weights_path, ref_audio_path, prompt_text) "
                "VALUES ('夏目（自制）', ?, ?, ?, ?)",
                row,
            )

        _rename(pkg_dir, "四季夏目")
        assert (await client.post("/api/characters/rescan")).status_code == 200

        names = {c["name"] for c in (await client.get("/api/characters")).json()["characters"]}
        assert {"夏目", "夏目（自制）", "四季夏目"} <= names
