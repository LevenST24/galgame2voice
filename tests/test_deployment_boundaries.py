"""First-install, container binding, CRUD identifiers and browser boundaries."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from galgame2voice.config import get_settings
from galgame2voice.database import crud
from galgame2voice.database.models import VoiceProfileCreate
from galgame2voice.database.session import get_db, init_db


async def test_new_install_has_no_machine_specific_voice(tmp_path):
    db = tmp_path / "fresh.db"
    await init_db(db)
    async with get_db(db) as conn:
        assert await crud.list_voice_profiles(conn) == []
        assert await crud.get_active_voice_profile(conn) is None
        assert (await crud.get_settings_raw(conn)).active_voice_profile_id is None
        profile = await crud.create_voice_profile(conn, VoiceProfileCreate(
            name="User voice", gpt_weights_path="user.ckpt", sovits_weights_path="user.pth",
        ))
    await init_db(db)
    async with get_db(db) as conn:
        assert [p.id for p in await crud.list_voice_profiles(conn)] == [profile.id]


@pytest.mark.parametrize("target", ["settings", "provider", "voice"])
async def test_crud_rejects_unknown_sql_columns(tmp_path, target):
    db = tmp_path / "columns.db"
    await init_db(db)
    malicious = MagicMock()
    malicious.model_dump.return_value = {"name = 'changed'; DROP TABLE providers; --": "value"}
    async with get_db(db) as conn:
        profile = await crud.create_voice_profile(conn, VoiceProfileCreate(
            name="User voice", gpt_weights_path="user.ckpt", sovits_weights_path="user.pth",
        ))
        with pytest.raises(ValueError, match="Unsupported"):
            if target == "settings":
                await crud.update_settings(conn, malicious)
            elif target == "provider":
                await crud.update_provider(conn, "deepseek", malicious)
            else:
                await crud.update_voice_profile(conn, profile.id, malicious)
        assert (await crud.get_voice_profile(conn, profile.id)).name == "User voice"
        assert await crud.list_providers(conn)


def test_container_entrypoint_uses_same_binding_settings(monkeypatch):
    import galgame2voice.main as main
    import uvicorn
    monkeypatch.setenv("GALGAME2VOICE_HOST", "0.0.0.0")
    monkeypatch.setenv("GALGAME2VOICE_AUTH_DISABLED", "0")
    get_settings.cache_clear()
    run = MagicMock()
    monkeypatch.setattr(uvicorn, "run", run)
    monkeypatch.setattr(main.sys, "argv", ["galgame2voice"])
    main.run()
    assert run.call_args.kwargs["host"] == get_settings().host == "0.0.0.0"
    monkeypatch.setenv("GALGAME2VOICE_AUTH_DISABLED", "1")
    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="FATAL"):
        main._init_logging_and_safety(get_settings())


def test_directory_browser_rejects_outside_roots_and_hides_parent(tmp_path, monkeypatch):
    from galgame2voice.routers.voice import _fs_browse_sync
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "private"
    outside.mkdir()
    (outside / "should-not-list.ckpt").write_text("private")
    monkeypatch.setenv("GALGAME2VOICE_PROJECT_ROOT", str(root))
    monkeypatch.setenv("DATA_DIR_NAME", "data")
    get_settings.cache_clear()
    rejected = _fs_browse_sync(str(outside), "all")
    assert rejected["files"] == rejected["directories"] == []
    assert "error" in rejected
    allowed = _fs_browse_sync(str(root), "all")
    assert allowed["parent_path"] is None
    assert str(outside) not in allowed["drives"]


def test_directory_browser_supports_declared_external_engine(tmp_path, monkeypatch):
    from galgame2voice.routers.voice import _fs_browse_sync
    root = tmp_path / "project"
    root.mkdir()
    engine = tmp_path / "external-engine"
    engine.mkdir()
    (engine / "voice.ckpt").write_text("model")
    (engine / ".private.ckpt").write_text("hidden")
    monkeypatch.setenv("GALGAME2VOICE_PROJECT_ROOT", str(root))
    monkeypatch.setenv("GPT_SOVITS_DIR", str(engine))
    get_settings.cache_clear()
    result = _fs_browse_sync(str(engine), "gpt")
    assert [item["name"] for item in result["files"]] == ["voice.ckpt"]
    assert result["parent_path"] is None


def test_runtime_singletons_use_isolated_paths(tmp_path):
    from galgame2voice.database.session import get_database_path
    from galgame2voice.services.tts_cache_manager import get_tts_cache_manager
    from galgame2voice.services.voice_manager import get_voice_manager
    settings = get_settings()
    assert settings.data_dir.is_relative_to(tmp_path)
    assert settings.audio_dir.is_relative_to(tmp_path)
    assert Path(get_database_path()) == settings.db_path
    assert get_tts_cache_manager().cache_dir.is_relative_to(tmp_path)
    assert Path(get_voice_manager().db_path).is_relative_to(tmp_path)


@pytest.mark.parametrize("raises", [False, True])
async def test_already_active_voice_reports_persistence_failure(tmp_path, monkeypatch, raises):
    from fastapi import HTTPException
    from galgame2voice.routers import voice
    await init_db(get_settings().db_path)
    async with get_db() as conn:
        profile = await crud.create_voice_profile(conn, VoiceProfileCreate(
            name="User voice", gpt_weights_path="user.ckpt", sovits_weights_path="user.pth",
        ))
    manager = SimpleNamespace(
        active_profile=profile, switch_lock=asyncio.Lock(), is_active_profile=lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(voice, "get_voice_manager", lambda: manager)
    update = AsyncMock(side_effect=OSError("write failed")) if raises else AsyncMock(return_value=False)
    monkeypatch.setattr(voice.crud, "set_active_voice_profile", update)
    with pytest.raises(HTTPException) as result:
        await voice.switch_voice(voice.VoiceSwitchRequest(profile_id=profile.id))
    assert result.value.status_code == 500
    assert result.value.detail == "Failed to persist active voice profile"
