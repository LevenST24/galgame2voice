"""Catalog-owned profile reads and the list endpoint's manifest speech speed."""

from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from galgame2voice.database import crud
from galgame2voice.database.models import VoiceProfileCreate
from galgame2voice.database.session import get_db, init_db
from galgame2voice.main import app
from galgame2voice.services.character_voice_catalog import get_character_voice_catalog
from galgame2voice.services.voice_manager import VoiceManager


def _profile(name: str, *, is_default: bool = False) -> VoiceProfileCreate:
    return VoiceProfileCreate(
        name=name,
        gpt_weights_path=f"{name}.ckpt",
        sovits_weights_path=f"{name}.pth",
        ref_audio_path=f"{name}.wav",
        prompt_text="テスト",
        prompt_lang="ja",
        text_lang="ja",
        is_default=is_default,
    )


@pytest.mark.asyncio
async def test_catalog_reads_profiles_from_the_given_database(tmp_path):
    """Reads must open the caller's database, not whatever get_db() points at."""
    database = tmp_path / "catalog-reads.db"
    await init_db(database)
    async with get_db(database) as conn:
        kept = await crud.create_voice_profile(conn, _profile("保留音色"))
        active = await crud.create_voice_profile(conn, _profile("当前音色", is_default=True))
        assert await crud.set_active_voice_profile(conn, active.id)

    catalog = get_character_voice_catalog()
    missing = [
        name for name in ("list_profiles", "get_profile", "get_active_profile")
        if not hasattr(catalog, name)
    ]
    assert not missing, f"catalog is missing {missing}"

    def open_db():
        return get_db(database)

    listed = await catalog.list_profiles(db_factory=open_db)
    fetched = await catalog.get_profile(kept.id, db_factory=open_db)
    current = await catalog.get_active_profile(db_factory=open_db)

    assert {item.id for item in listed} == {kept.id, active.id}
    assert fetched is not None and fetched.id == kept.id and fetched.name == "保留音色"
    assert current is not None and current.id == active.id and current.name == "当前音色"


def test_voice_manager_no_longer_wraps_profile_crud():
    removed = (
        "list_profiles",
        "get_profile",
        "get_active_profile",
        "create_profile",
        "update_profile",
        "delete_profile",
    )
    still_there = [name for name in removed if hasattr(VoiceManager, name)]
    assert still_there == []


def test_default_tts_speed_reads_a_valid_manifest_only(monkeypatch):
    packages = {
        "有包": SimpleNamespace(
            is_valid=True,
            manifest=SimpleNamespace(default_voice_params=SimpleNamespace(speed=0.88)),
        ),
        "坏包": SimpleNamespace(
            is_valid=False,
            manifest=SimpleNamespace(default_voice_params=SimpleNamespace(speed=0.5)),
        ),
        "无语速字段": SimpleNamespace(
            is_valid=True,
            manifest=SimpleNamespace(id="no_speed", name="无语速字段"),
        ),
    }
    monkeypatch.setattr(
        "galgame2voice.services.character_manager.get_character_manager",
        lambda: SimpleNamespace(get_character=lambda name: packages.get(name)),
    )
    from galgame2voice.services.character_manager import get_character_default_tts_speed

    assert get_character_default_tts_speed("有包") == 0.88
    assert get_character_default_tts_speed("坏包") is None
    assert get_character_default_tts_speed("无语速字段") is None
    assert get_character_default_tts_speed("没有") is None
    assert get_character_default_tts_speed("") is None


@pytest.mark.asyncio
async def test_profile_list_exposes_manifest_default_tts_speed(monkeypatch):
    """The list the new-chat screen already stores must carry the manifest speed."""

    def fake_speed(char_name: str):
        if char_name == "有默认语速":
            return 0.88
        return None

    monkeypatch.setattr(
        "galgame2voice.services.character_manager.get_character_default_tts_speed",
        fake_speed,
        raising=False,
    )
    monkeypatch.setattr(
        "galgame2voice.routers.voice.get_character_default_tts_speed",
        fake_speed,
        raising=False,
    )
    await init_db()
    async with get_db() as conn:
        await crud.create_voice_profile(conn, _profile("有默认语速"))
        await crud.create_voice_profile(conn, _profile("没有角色包"))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/voice/profiles")

    assert response.status_code == 200
    by_name = {item["name"]: item for item in response.json()["profiles"]}
    assert by_name["有默认语速"]["default_tts_speed"] == 0.88
    assert by_name["没有角色包"]["default_tts_speed"] is None


def test_catalog_imports_cleanly_in_fresh_subprocess():
    """Importing the catalog service first must not trigger a circular import with routers."""
    repo_root = Path(__file__).resolve().parent.parent
    result = subprocess.run(
        [sys.executable, "-c", "import galgame2voice.services.character_voice_catalog"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"exit={result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
