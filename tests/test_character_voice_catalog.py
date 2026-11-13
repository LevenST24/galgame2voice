"""Regression tests for unified CharacterVoiceCatalog state invariants."""

from __future__ import annotations

import asyncio
import errno
import json
from pathlib import Path
import sqlite3
from unittest.mock import AsyncMock
import wave

import httpx
from httpx import ASGITransport, AsyncClient
import pytest

from galgame2voice.config import get_settings
from galgame2voice.database import crud
from galgame2voice.database.session import get_db, init_db
from galgame2voice.main import create_app
from galgame2voice.routers import characters
from galgame2voice.services import tts_scheduler, voice_manager as voice_manager_mod
from galgame2voice.services.character_manager import CharacterManager
from galgame2voice.services.gpt_sovits_client import GptSovitsClient
from galgame2voice.services.voice_manager import WARMUP_PROBE_TEXT, VoiceManager


def _install_character_package(root: Path, package_id: str, name: str, seconds: int = 4) -> Path:
    package = root / "characters" / package_id
    package.mkdir(parents=True, exist_ok=True)
    with wave.open(str(package / "gentle.wav"), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\x01\x00" * 8000 * seconds)
    (package / "gpt.ckpt").write_bytes(b"x" * 5000)
    (package / "sovits.pth").write_bytes(b"x" * 5000)
    (package / "manifest.json").write_text(
        json.dumps(
            {
                "id": package_id,
                "name": name,
                "aliases": [],
                "gpt_weights": "gpt.ckpt",
                "sovits_weights": "sovits.pth",
                "system_prompt": f"Initial persona for {name}",
                "emotions": {"gentle": {"audio": "gentle.wav", "text": name, "lang": "ja"}},
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return package


def _create_dummy_voice_assets(root: Path, slug: str) -> tuple[str, str, str]:
    asset_dir = root / "characters" / f"assets_{slug}"
    asset_dir.mkdir(parents=True, exist_ok=True)
    gpt_path = asset_dir / f"{slug}.ckpt"
    sovits_path = asset_dir / f"{slug}.pth"
    wav_path = asset_dir / f"{slug}.wav"
    gpt_path.write_bytes(b"g" * 128)
    sovits_path.write_bytes(b"s" * 128)
    with wave.open(str(wav_path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\x01\x00" * 8000 * 4)
    return str(gpt_path), str(sovits_path), str(wav_path)


@pytest.mark.asyncio
async def test_delete_voice_profile_during_warmup_cancels_probe_and_never_resurrects_active_profile(
    tmp_path: Path,
    isolate_test_database: str,
    mock_gpt_sovits,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("GALGAME2VOICE_PROJECT_ROOT", str(tmp_path))
    get_settings.cache_clear()
    await init_db(isolate_test_database)

    scheduler = tts_scheduler.TtsScheduler()
    monkeypatch.setattr(tts_scheduler, "get_tts_scheduler", lambda: scheduler)

    sovits_client = GptSovitsClient(server=mock_gpt_sovits)
    manager = VoiceManager(sovits_client, db_path=isolate_test_database)
    monkeypatch.setattr(manager, "_check_switch_memory_guard", lambda force: None)
    monkeypatch.setattr(voice_manager_mod, "release_system_memory", lambda: None)
    monkeypatch.setattr(voice_manager_mod, "_global_voice_manager", manager)
    monkeypatch.setattr(
        sovits_client,
        "check_health",
        AsyncMock(return_value={"connected": True}),
    )

    gpt_fb, sovits_fb, wav_fb = _create_dummy_voice_assets(tmp_path, "fallback")
    gpt_p1, sovits_p1, wav_p1 = _create_dummy_voice_assets(tmp_path, "target_p1")
    gpt_p2, sovits_p2, wav_p2 = _create_dummy_voice_assets(tmp_path, "target_p2")

    app = create_app()
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    try:
        async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8080", timeout=15.0) as http:
            fb_res = await http.post(
                "/api/voice/profiles",
                json={
                    "name": "FallbackDefault",
                    "gpt_weights_path": gpt_fb,
                    "sovits_weights_path": sovits_fb,
                    "ref_audio_path": wav_fb,
                    "prompt_text": "fallback",
                    "prompt_lang": "ja",
                    "is_default": True,
                },
            )
            assert fb_res.status_code == 201, fb_res.text

            p1_res = await http.post(
                "/api/voice/profiles",
                json={
                    "name": "WarmupTargetP1",
                    "gpt_weights_path": gpt_p1,
                    "sovits_weights_path": sovits_p1,
                    "ref_audio_path": wav_p1,
                    "prompt_text": "target p1",
                    "prompt_lang": "ja",
                },
            )
            assert p1_res.status_code == 201, p1_res.text
            p1_id = p1_res.json()["id"]

            # Phase A: Warm-up is actively loading p1 weights inside prepare_and_probe -> switch_voice_profile.
            async with get_db(isolate_test_database) as conn:
                assert await crud.set_active_voice_profile(conn, p1_id)
            manager.active_profile = None
            mock_gpt_sovits.call_history.clear()

            load_entered = asyncio.Event()
            load_release = asyncio.Event()
            orig_handle_request = mock_gpt_sovits.handle_request

            async def gated_load_handle(method: str, path: str, json_data=None, params=None) -> httpx.Response:
                if path == "/set_gpt_weights" and not load_entered.is_set():
                    load_entered.set()
                    await load_release.wait()
                return await orig_handle_request(method, path, json_data=json_data, params=params)

            monkeypatch.setattr(mock_gpt_sovits, "handle_request", gated_load_handle)

            warmup_load_task = manager._spawn_background(manager.warmup_current_profile())
            await asyncio.wait_for(load_entered.wait(), timeout=5.0)

            del_during_load = await http.delete(f"/api/voice/profiles/{p1_id}")
            load_release.set()
            await asyncio.wait_for(warmup_load_task, timeout=5.0)

            if del_during_load.status_code != 409:
                tts_probes = [
                    c for c in mock_gpt_sovits.call_history
                    if c["endpoint"] == "/tts" and c["payload"].get("text") == WARMUP_PROBE_TEXT
                ]
                pytest.fail(
                    f"DELETE while warm-up was loading weights returned {del_during_load.status_code} (expected 409); "
                    f"after load finished: active_profile={getattr(manager.active_profile, 'id', None)}, "
                    f"probe_tts_calls={len(tts_probes)}"
                )

            monkeypatch.setattr(mock_gpt_sovits, "handle_request", orig_handle_request)

            # Phase B: Switch to p1 spawns background warm-up; while warm-up is suspended in health check,
            # DELETE /api/voice/profiles/{p1.id} returns 200, cancels warm-up, clears active_profile,
            # and never runs probe synthesis or activates another profile.
            health_entered = asyncio.Event()
            health_release = asyncio.Event()

            async def gated_health() -> dict[str, bool]:
                health_entered.set()
                await health_release.wait()
                return {"connected": True}

            monkeypatch.setattr(sovits_client, "check_health", gated_health)
            manager.active_profile = None
            mock_gpt_sovits.call_history.clear()

            switch_res = await http.post("/api/voice/switch", json={"profile_id": p1_id})
            assert switch_res.status_code == 200, switch_res.text
            await asyncio.wait_for(health_entered.wait(), timeout=5.0)

            mock_gpt_sovits.call_history.clear()
            del_res = await http.delete(f"/api/voice/profiles/{p1_id}")
            assert del_res.status_code == 200, del_res.text

            health_release.set()
            if manager._bg_tasks:
                await asyncio.wait_for(
                    asyncio.gather(*list(manager._bg_tasks), return_exceptions=True),
                    timeout=5.0,
                )

            assert manager.active_profile is None
            assert [c for c in mock_gpt_sovits.call_history if c["endpoint"] == "/tts"] == []

            # Phase C: Defense-in-depth — if DB row vanishes mid-switch_voice_profile inside prepare_and_probe,
            # post-load verification restores/clears active_profile and skips synthesize(WARMUP_PROBE_TEXT).
            monkeypatch.setattr(
                sovits_client,
                "check_health",
                AsyncMock(return_value={"connected": True}),
            )
            p2_res = await http.post(
                "/api/voice/profiles",
                json={
                    "name": "WarmupTargetP2",
                    "gpt_weights_path": gpt_p2,
                    "sovits_weights_path": sovits_p2,
                    "ref_audio_path": wav_p2,
                    "prompt_text": "target p2",
                    "prompt_lang": "ja",
                },
            )
            assert p2_res.status_code == 201, p2_res.text
            p2_id = p2_res.json()["id"]
            async with get_db(isolate_test_database) as conn:
                assert await crud.set_active_voice_profile(conn, p2_id)
            manager.active_profile = None
            mock_gpt_sovits.call_history.clear()

            mid_entered = asyncio.Event()
            mid_release = asyncio.Event()

            async def gated_sovits_handle(method: str, path: str, json_data=None, params=None) -> httpx.Response:
                if path == "/set_sovits_weights" and not mid_entered.is_set():
                    mid_entered.set()
                    await mid_release.wait()
                return await orig_handle_request(method, path, json_data=json_data, params=params)

            monkeypatch.setattr(mock_gpt_sovits, "handle_request", gated_sovits_handle)
            mid_task = asyncio.create_task(manager.warmup_current_profile())
            await asyncio.wait_for(mid_entered.wait(), timeout=5.0)
            async with get_db(isolate_test_database) as conn:
                assert await crud.delete_voice_profile(conn, p2_id)
            mid_release.set()

            assert await asyncio.wait_for(mid_task, timeout=5.0) is False
            assert manager.active_profile is None
            assert [c for c in mock_gpt_sovits.call_history if c["endpoint"] == "/tts"] == []
    finally:
        await manager.aclose()
        await sovits_client.aclose()


@pytest.mark.asyncio
async def test_delete_voice_profile_unbinds_session_settings_json_voice_profile_id(
    tmp_path: Path,
    isolate_test_database: str,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("GALGAME2VOICE_PROJECT_ROOT", str(tmp_path))
    get_settings.cache_clear()
    await init_db(isolate_test_database)

    gpt_p1, sovits_p1, wav_p1 = _create_dummy_voice_assets(tmp_path, "sess_p1")
    gpt_p2, sovits_p2, wav_p2 = _create_dummy_voice_assets(tmp_path, "sess_p2")

    app = create_app()
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8080", timeout=15.0) as http:
        p1_res = await http.post(
            "/api/voice/profiles",
            json={
                "name": "SessionVoiceOne",
                "gpt_weights_path": gpt_p1,
                "sovits_weights_path": sovits_p1,
                "ref_audio_path": wav_p1,
                "prompt_text": "p1",
                "prompt_lang": "ja",
            },
        )
        assert p1_res.status_code == 201, p1_res.text
        p1_id = p1_res.json()["id"]

        p2_res = await http.post(
            "/api/voice/profiles",
            json={
                "name": "SessionVoiceTwo",
                "gpt_weights_path": gpt_p2,
                "sovits_weights_path": sovits_p2,
                "ref_audio_path": wav_p2,
                "prompt_text": "p2",
                "prompt_lang": "ja",
            },
        )
        assert p2_res.status_code == 201, p2_res.text
        p2_id = p2_res.json()["id"]

        s1_res = await http.post(
            "/api/chat/sessions",
            json={
                "id": "s1",
                "title": "Bound to P1",
                "voice_profile_id": p1_id,
                "settings": {"voiceProfileId": p1_id, "advBgmVolume": 0.5},
            },
        )
        assert s1_res.status_code == 200, s1_res.text

        s2_res = await http.post(
            "/api/chat/sessions",
            json={
                "id": "s2",
                "title": "Bound to P2",
                "voice_profile_id": p2_id,
                "settings": {"voiceProfileId": p2_id, "advBgmVolume": 0.8},
            },
        )
        assert s2_res.status_code == 200, s2_res.text

        del_res = await http.delete(f"/api/voice/profiles/{p1_id}")
        assert del_res.status_code == 200, del_res.text

        async with get_db(isolate_test_database) as conn:
            s1_db = await crud.get_session(conn, "s1")
            s2_db = await crud.get_session(conn, "s2")

        assert s1_db is not None
        assert s1_db.voice_profile_id is None
        assert s1_db.settings_json is not None
        s1_settings = json.loads(s1_db.settings_json)
        assert s1_settings.get("voiceProfileId") is None
        assert s1_settings.get("advBgmVolume") == 0.5

        assert s2_db is not None
        assert s2_db.voice_profile_id == p2_id
        assert json.loads(s2_db.settings_json or "{}") == {
            "voiceProfileId": p2_id,
            "advBgmVolume": 0.8,
        }

        listed_res = await http.get("/api/chat/sessions")
        assert listed_res.status_code == 200, listed_res.text
        by_id = {item["id"]: item for item in listed_res.json()["sessions"]}
        assert by_id["s1"]["voice_profile_id"] is None
        assert by_id["s1"]["settings"].get("voiceProfileId") is None
        assert by_id["s1"]["settings"].get("advBgmVolume") == 0.5
        assert by_id["s2"]["voice_profile_id"] == p2_id
        assert by_id["s2"]["settings"].get("voiceProfileId") == p2_id


@pytest.mark.asyncio
async def test_update_persona_never_exposes_dirty_manifest_on_disk_during_or_after_db_failure(
    tmp_path: Path,
    isolate_test_database: str,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("GALGAME2VOICE_PROJECT_ROOT", str(tmp_path))
    get_settings.cache_clear()
    await init_db(isolate_test_database)

    CharacterManager.reset_instance()
    try:
        pkg_dir = _install_character_package(tmp_path, "ann", "Ann")
        manifest_path = pkg_dir / "manifest.json"
        original_bytes = manifest_path.read_bytes()
        original_prompt = json.loads(original_bytes.decode("utf-8"))["system_prompt"]

        app = create_app()
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8080", timeout=15.0) as http:
            scan_res = await http.post("/api/characters/rescan")
            assert scan_res.status_code == 200, scan_res.text
            profiles = (await http.get("/api/voice/profiles")).json()["profiles"]
            ann = next(row for row in profiles if row["name"] == "Ann")

            observed_during_db_write: dict[str, object] = {}
            real_write_bytes = Path.write_bytes
            real_write_text = Path.write_text

            def readonly_disk_write_bytes(self: Path, *args, **kwargs):
                if self.parent == pkg_dir:
                    raise OSError(errno.ENOSPC, "No space left on device")
                return real_write_bytes(self, *args, **kwargs)

            def readonly_disk_write_text(self: Path, *args, **kwargs):
                if self.parent == pkg_dir:
                    raise OSError(errno.ENOSPC, "No space left on device")
                return real_write_text(self, *args, **kwargs)

            async def fail_update_and_inspect(*args, **kwargs):
                observed_during_db_write["manifest_bytes"] = manifest_path.read_bytes()
                mgr_pkg = characters.get_character_manager().get_character("Ann")
                observed_during_db_write["cached_prompt"] = (
                    mgr_pkg.system_prompt if mgr_pkg is not None else None
                )
                # Simulate disk becoming full/read-only so no post-failure rollback write can succeed.
                monkeypatch.setattr(Path, "write_bytes", readonly_disk_write_bytes)
                monkeypatch.setattr(Path, "write_text", readonly_disk_write_text)
                raise sqlite3.OperationalError("database or disk is full")

            monkeypatch.setattr(
                "galgame2voice.routers.characters.crud.update_voice_profile",
                fail_update_and_inspect,
            )

            dirty_draft = "未提交的人设草稿：写库前及写库失败后都不应出现在正式 manifest.json"
            res = await http.put(
                f"/api/characters/{ann['id']}/system-prompt",
                json={"system_prompt": dirty_draft},
            )

            assert res.status_code == 500, res.text
            assert observed_during_db_write["manifest_bytes"] == original_bytes
            assert observed_during_db_write["cached_prompt"] == original_prompt
            assert manifest_path.read_bytes() == original_bytes
            mgr_after = characters.get_character_manager().get_character("Ann")
            assert mgr_after is not None
            assert mgr_after.system_prompt == original_prompt
            assert not manifest_path.with_name("manifest.json.tmp").exists()
    finally:
        CharacterManager.reset_instance()
