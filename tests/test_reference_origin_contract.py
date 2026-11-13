"""Formal regression tests for unified reference audio origin and ownership contracts.

Verifies:
1. Real HTTP API creates and updates profiles with external/engine reference paths.
2. Character rescan preserves external references and empty prompts, verified via a fresh DB connection.
3. Rescan idempotency across multiple runs.
4. Voice resolver preserves external references (including non-manifested refs/ paths) without local fallback.
5. Missing package-owned references and cross-package references still trigger gentle recovery with paired text.
6. TTS service explicit overrides are preserved identically on both production and stand-in clients.
7. Local audio with invalid duration (<3s or >10s) triggers fallback.
8. Real AudioIdentity and TtsCacheManager generate distinct cache keys for different external paths and engine URLs.
"""

import json
import struct
import wave
from pathlib import Path
from types import SimpleNamespace

import httpx
from httpx import ASGITransport, AsyncClient
import pytest

from galgame2voice.config import get_settings
from galgame2voice.database import crud
from galgame2voice.database.models import VoiceProfileCreate, VoiceProfileUpdate
from galgame2voice.database.session import get_database_path, get_db, init_db
from galgame2voice.main import create_app
from galgame2voice.services.character_manager import CharacterManager, get_character_manager
from galgame2voice.services.gpt_sovits_client import GptSovitsClient
from galgame2voice.services.tts_cache_manager import TtsCacheManager
from galgame2voice.services.tts_service import TtsService
from galgame2voice.services.voice_resolver import get_voice_resolver


def _create_wav(path: Path, duration_sec: float = 4.0, sample_rate: int = 16000, freq: int = 440) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    n_frames = int(sample_rate * duration_sec)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        frames = bytearray()
        step = max(1, sample_rate // (freq * 2))
        for i in range(n_frames):
            val = int(2000 * (1 if (i // step) % 2 == 0 else -1))
            frames.extend(struct.pack("<h", val))
        w.writeframes(frames)
    return path


class DummyEngine:
    async def handle_request(self, method, path, **kwargs):
        return SimpleNamespace(status_code=200, json=lambda: {"code": 0})


@pytest.fixture
async def origin_env(tmp_path, monkeypatch):
    db_file = tmp_path / "voices.db"
    monkeypatch.setenv("GALGAME2VOICE_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("GALGAME2VOICE_CHARACTERS_DIR", str(tmp_path / "characters"))
    monkeypatch.setenv("GALGAME2VOICE_AUDIO_DIR", str(tmp_path / "audio"))
    monkeypatch.setenv("GALGAME2VOICE_DB_PATH", str(db_file))
    monkeypatch.setenv("GALGAME_DB_PATH", str(db_file))
    monkeypatch.setenv("GALGAME2VOICE_SKIP_MEM_CHECK", "1")
    get_settings.cache_clear()
    CharacterManager.reset_instance()
    get_voice_resolver().invalidate()

    chars_dir = tmp_path / "characters" / "shiki"
    chars_dir.mkdir(parents=True, exist_ok=True)

    gentle_wav = _create_wav(chars_dir / "refs" / "gentle.wav", duration_sec=4.0, freq=440)
    gpt_file = chars_dir / "weights" / "char.ckpt"
    sovits_file = chars_dir / "weights" / "char.pth"
    gpt_file.parent.mkdir(parents=True, exist_ok=True)
    gpt_file.write_bytes(b"synthetic gpt weights")
    sovits_file.write_bytes(b"synthetic sovits weights")

    # Second package for cross-character testing
    other_dir = tmp_path / "characters" / "other"
    other_dir.mkdir(parents=True, exist_ok=True)
    other_wav = _create_wav(other_dir / "refs" / "joy.wav", duration_sec=4.0, freq=880)
    other_gpt = other_dir / "weights" / "char.ckpt"
    other_sovits = other_dir / "weights" / "char.pth"
    other_gpt.parent.mkdir(parents=True, exist_ok=True)
    other_gpt.write_bytes(b"other synthetic gpt")
    other_sovits.write_bytes(b"other synthetic sovits")

    manifest_shiki = {
        "id": "shiki",
        "name": "四季夏目",
        "version": "1.0.0",
        "gpt_weights": "weights/char.ckpt",
        "sovits_weights": "weights/char.pth",
        "emotions": {
            "gentle": {
                "audio": "refs/gentle.wav",
                "text": "四季夏目、よろしくお願いします。（gentle）",
                "lang": "ja",
            },
        },
    }
    (chars_dir / "manifest.json").write_text(json.dumps(manifest_shiki, ensure_ascii=False), encoding="utf-8")

    manifest_other = {
        "id": "other",
        "name": "其他角色",
        "version": "1.0.0",
        "gpt_weights": "weights/char.ckpt",
        "sovits_weights": "weights/char.pth",
        "emotions": {
            "joy": {
                "audio": "refs/joy.wav",
                "text": "楽しいです！（joy）",
                "lang": "ja",
            },
        },
    }
    (other_dir / "manifest.json").write_text(json.dumps(manifest_other, ensure_ascii=False), encoding="utf-8")

    mgr = get_character_manager(characters_dir=tmp_path / "characters")
    discovered = mgr.discover_characters()
    assert len(discovered) == 2

    await init_db(db_file)

    # Production client with mock HTTP boundary
    async def _mock_http_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0, "message": "OK"})

    transport = httpx.MockTransport(_mock_http_handler)
    http_async_client = httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:9880")
    client_prod = GptSovitsClient(server=None, client=http_async_client)
    client_standin = GptSovitsClient(server=DummyEngine())

    cache = TtsCacheManager(cache_dir=tmp_path / "audio" / "cache", db_path=str(db_file))
    service_prod = TtsService(client=client_prod, cache_manager=cache, db_path=str(db_file), audio_dir=tmp_path / "audio")
    service_standin = TtsService(client=client_standin, cache_manager=cache, db_path=str(db_file), audio_dir=tmp_path / "audio")

    # Assert get_database_path and TtsService.db_path point to the exact same temporary DB file
    assert str(get_database_path()) == str(db_file)
    assert str(service_prod.db_path) == str(db_file)
    assert str(service_standin.db_path) == str(db_file)

    app = create_app()

    yield SimpleNamespace(
        root=tmp_path,
        chars_dir=chars_dir,
        other_dir=other_dir,
        other_wav=other_wav,
        db_path=db_file,
        gentle_wav=gentle_wav,
        gpt_file=gpt_file,
        sovits_file=sovits_file,
        mgr=mgr,
        cache=cache,
        client_prod=client_prod,
        client_standin=client_standin,
        service_prod=service_prod,
        service_standin=service_standin,
        app=app,
    )
    await cache.aclose()
    await client_prod.aclose()
    await client_standin.aclose()
    await http_async_client.aclose()
    CharacterManager.reset_instance()
    get_voice_resolver().invalidate()


@pytest.mark.asyncio
async def test_api_create_and_update_with_external_references(origin_env):
    """Real HTTP API creates and updates profiles with external references and unmanifested refs/ paths.

    Verifies returned ID is directly queried from service's database and used for TTS parameter preparation.
    """
    transport = ASGITransport(app=origin_env.app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as client:
        # 1. Create with audio/custom_remote_ref.wav
        resp1 = await client.post("/api/voice/profiles", json={
            "name": "四季夏目",
            "gpt_weights_path": str(origin_env.gpt_file),
            "sovits_weights_path": str(origin_env.sovits_file),
            "ref_audio_path": "audio/custom_remote_ref.wav",
            "prompt_text": "远端参考音频文本",
            "prompt_lang": "zh",
            "refer_language": "zh",
            "text_lang": "ja",
        })
        assert resp1.status_code in (200, 201)
        p1 = resp1.json()["profile"]
        pid = p1["id"]
        assert p1["ref_audio_path"] == "audio/custom_remote_ref.wav"
        assert p1["prompt_text"] == "远端参考音频文本"

        # Verify ID directly read from service's DB (no secondary CRUD substitute)
        async with get_db(origin_env.service_prod.db_path) as conn:
            db_row = await crud.get_voice_profile(conn, pid)
            assert db_row is not None
            assert db_row.id == pid
            assert db_row.ref_audio_path == "audio/custom_remote_ref.wav"
            assert db_row.prompt_text == "远端参考音频文本"
            assert db_row.prompt_lang == "zh"

        # Verify TTS context / parameter preparation directly from service using the HTTP ID
        opts1_prod = await origin_env.service_prod._populate_voice_profile_opts({"voice_profile_id": pid})
        assert opts1_prod["ref_audio_path"] == "audio/custom_remote_ref.wav"
        assert opts1_prod["prompt_text"] == "远端参考音频文本"
        assert opts1_prod["prompt_lang"] == "zh"

        opts1_standin = await origin_env.service_standin._populate_voice_profile_opts({"voice_profile_id": pid})
        assert opts1_standin["ref_audio_path"] == "audio/custom_remote_ref.wav"
        assert opts1_standin["prompt_text"] == "远端参考音频文本"
        assert opts1_standin["prompt_lang"] == "zh"

        # 2. Update with unmanifested refs/engine_only.wav
        resp2 = await client.put(f"/api/voice/profiles/{pid}", json={
            "ref_audio_path": "refs/engine_only.wav",
            "prompt_text": "引擎端独立参考文本",
            "prompt_lang": "zh",
        })
        assert resp2.status_code == 200
        p2 = resp2.json()["profile"]
        assert p2["ref_audio_path"] == "refs/engine_only.wav"
        assert p2["prompt_text"] == "引擎端独立参考文本"

        # Verify updated row read directly from service's DB
        async with get_db(origin_env.service_prod.db_path) as conn:
            db_row2 = await crud.get_voice_profile(conn, pid)
            assert db_row2 is not None
            assert db_row2.ref_audio_path == "refs/engine_only.wav"
            assert db_row2.prompt_text == "引擎端独立参考文本"
            assert db_row2.prompt_lang == "zh"

        # Invalidate cached context and verify TTS opts preparation reflects update
        get_voice_resolver().invalidate(profile_id=pid, db_path=str(origin_env.db_path))
        opts2_prod = await origin_env.service_prod._populate_voice_profile_opts({"voice_profile_id": pid})
        assert opts2_prod["ref_audio_path"] == "refs/engine_only.wav"
        assert opts2_prod["prompt_text"] == "引擎端独立参考文本"

        opts2_standin = await origin_env.service_standin._populate_voice_profile_opts({"voice_profile_id": pid})
        assert opts2_standin["ref_audio_path"] == "refs/engine_only.wav"
        assert opts2_standin["prompt_text"] == "引擎端独立参考文本"

        # 3. Path traversal attack is rejected
        resp_bad = await client.put(f"/api/voice/profiles/{pid}", json={
            "ref_audio_path": "../../etc/shadow.wav",
        })
        assert resp_bad.status_code == 400


@pytest.mark.asyncio
async def test_rescan_preserves_external_references_with_fresh_connection(origin_env):
    """sync_with_db preserves external and unmanifested refs paths, verified via a fresh DB connection."""
    transport = ASGITransport(app=origin_env.app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as client:
        # Create profiles via HTTP API
        resp1 = await client.post("/api/voice/profiles", json={
            "name": "四季夏目",
            "gpt_weights_path": str(origin_env.gpt_file),
            "sovits_weights_path": str(origin_env.sovits_file),
            "ref_audio_path": "audio/custom_remote_ref.wav",
            "prompt_text": "远端参考文本",
            "prompt_lang": "zh",
            "refer_language": "zh",
            "text_lang": "ja",
        })
        assert resp1.status_code in (200, 201)
        p1_id = resp1.json()["profile"]["id"]

        resp2 = await client.post("/api/voice/profiles", json={
            "name": "其他角色",
            "gpt_weights_path": str(origin_env.other_dir / "weights" / "char.ckpt"),
            "sovits_weights_path": str(origin_env.other_dir / "weights" / "char.pth"),
            "ref_audio_path": "refs/engine_only.wav",
            "prompt_text": "",  # Legitimate empty prompt
            "prompt_lang": "ja",
            "text_lang": "ja",
        })
        assert resp2.status_code in (200, 201)
        p2_id = resp2.json()["profile"]["id"]

        # Run rescan via API
        rescan_resp = await client.post("/api/characters/rescan")
        assert rescan_resp.status_code == 200

    # Original HTTP client and connection closed. Open a BRAND NEW connection:
    async with get_db(origin_env.db_path) as new_conn:
        row1 = await crud.get_voice_profile(new_conn, p1_id)
        assert row1.ref_audio_path == "audio/custom_remote_ref.wav"
        assert row1.prompt_text == "远端参考文本"
        assert row1.prompt_lang == "zh"

        row2 = await crud.get_voice_profile(new_conn, p2_id)
        assert row2.ref_audio_path == "refs/engine_only.wav"
        assert row2.prompt_text == ""
        assert row2.prompt_lang == "ja"

        # Idempotency check: second scan must make 0 updates
        updates_count = await origin_env.mgr.sync_with_db(new_conn)
        assert updates_count == 0


@pytest.mark.asyncio
async def test_resolver_preserves_external_and_unmanifested_refs(origin_env):
    """VoiceProfileResolver preserves external references and unmanifested refs/ without gentle fallback."""
    async with get_db(origin_env.db_path) as conn:
        profile = await crud.create_voice_profile(
            conn,
            VoiceProfileCreate(
                name="四季夏目",
                gpt_weights_path=str(origin_env.gpt_file),
                sovits_weights_path=str(origin_env.sovits_file),
                ref_audio_path="refs/engine_only.wav",
                prompt_text="引擎端独立参考文本",
                prompt_lang="zh",
                text_lang="ja",
            ),
        )

    resolver = get_voice_resolver()
    ctx = await resolver.resolve_context(str(origin_env.db_path), profile_id=profile.id)
    assert ctx is not None
    assert ctx.ref_audio_path == "refs/engine_only.wav"
    assert ctx.prompt_text == "引擎端独立参考文本"
    assert ctx.prompt_lang == "zh"


@pytest.mark.asyncio
async def test_missing_package_owned_and_cross_char_audio_fall_back_to_gentle(origin_env):
    """Missing package-owned references and cross-package references fall back to gentle."""
    deleted_pkg_wav = str(origin_env.chars_dir / "refs" / "deleted_ref.wav")
    async with get_db(origin_env.db_path) as conn:
        # 1. Missing package-owned reference on package "四季夏目"
        p_missing = await crud.create_voice_profile(
            conn,
            VoiceProfileCreate(
                name="四季夏目",
                gpt_weights_path=str(origin_env.gpt_file),
                sovits_weights_path=str(origin_env.sovits_file),
                ref_audio_path=deleted_pkg_wav,
                prompt_text="旧文本",
                prompt_lang="zh",
                text_lang="ja",
            ),
        )

    resolver = get_voice_resolver()

    ctx_missing = await resolver.resolve_context(str(origin_env.db_path), profile_id=p_missing.id)
    assert Path(ctx_missing.ref_audio_path) == origin_env.gentle_wav
    assert ctx_missing.prompt_text == "四季夏目、よろしくお願いします。（gentle）"
    assert ctx_missing.prompt_lang == "ja"

    # 2. Now update profile to cross-character audio (referencing other character package's file)
    async with get_db(origin_env.db_path) as conn:
        await crud.update_voice_profile(
            conn,
            p_missing.id,
            VoiceProfileUpdate(
                ref_audio_path=str(origin_env.other_wav),
                prompt_text="跨角色音频文本",
                prompt_lang="zh",
            ),
        )
    resolver.invalidate(profile_id=p_missing.id, db_path=str(origin_env.db_path))

    ctx_cross = await resolver.resolve_context(str(origin_env.db_path), profile_id=p_missing.id)
    assert Path(ctx_cross.ref_audio_path) == origin_env.gentle_wav
    assert ctx_cross.prompt_text == "四季夏目、よろしくお願いします。（gentle）"
    assert ctx_cross.prompt_lang == "ja"


@pytest.mark.asyncio
async def test_tts_explicit_override_parity_between_prod_and_standin(origin_env):
    """Production and stand-in clients behave identically for external explicit overrides."""
    async with get_db(origin_env.db_path) as conn:
        profile = await crud.create_voice_profile(
            conn,
            VoiceProfileCreate(
                name="四季夏目",
                gpt_weights_path=str(origin_env.gpt_file),
                sovits_weights_path=str(origin_env.sovits_file),
                ref_audio_path=str(origin_env.gentle_wav),
                prompt_text="温柔默认台词",
                prompt_lang="ja",
                text_lang="ja",
            ),
        )

    opts = {
        "voice_profile_id": profile.id,
        "ref_audio_path": "audio/remote_override.wav",
        "prompt_text": "覆盖台词",
        "prompt_lang": "zh",
    }

    res_prod = await origin_env.service_prod._populate_voice_profile_opts(dict(opts))
    res_standin = await origin_env.service_standin._populate_voice_profile_opts(dict(opts))

    # Both production and stand-in must preserve the remote override identically!
    assert res_prod["ref_audio_path"] == "audio/remote_override.wav"
    assert res_prod["prompt_text"] == "覆盖台词"
    assert res_prod["prompt_lang"] == "zh"

    assert res_standin["ref_audio_path"] == "audio/remote_override.wav"
    assert res_standin["prompt_text"] == "覆盖台词"
    assert res_standin["prompt_lang"] == "zh"


@pytest.mark.asyncio
async def test_local_audio_invalid_duration_triggers_fallback(origin_env):
    """When a local reference file exists but duration is outside [3.0, 10.0]s, fallback is triggered."""
    # Create 1.0s audio (too short)
    short_wav = _create_wav(origin_env.root / "audio" / "short.wav", duration_sec=1.0)

    async with get_db(origin_env.db_path) as conn:
        profile = await crud.create_voice_profile(
            conn,
            VoiceProfileCreate(
                name="四季夏目",
                gpt_weights_path=str(origin_env.gpt_file),
                sovits_weights_path=str(origin_env.sovits_file),
                ref_audio_path=str(origin_env.gentle_wav),
                prompt_text="温柔默认台词",
                prompt_lang="ja",
                text_lang="ja",
            ),
        )

    opts = {
        "voice_profile_id": profile.id,
        "ref_audio_path": str(short_wav),
        "prompt_text": "短音频文本",
        "prompt_lang": "zh",
    }
    resolved = await origin_env.service_prod._populate_voice_profile_opts(opts)
    # Must fallback because local file actually exists and measured duration is invalid:
    assert Path(resolved["ref_audio_path"]) == origin_env.gentle_wav
    assert resolved["prompt_text"] == "温柔默认台词"
    assert resolved["prompt_lang"] == "ja"


@pytest.mark.asyncio
async def test_cache_keys_distinguish_external_paths_and_engine_urls(origin_env):
    """AudioIdentity and TtsCacheManager generate distinct cache keys for different external paths and engine URLs."""
    opts_base = {
        "voice_profile_id": 1,
        "gpt_weights_path": str(origin_env.gpt_file),
        "sovits_weights_path": str(origin_env.sovits_file),
        "prompt_text": "测试台词",
        "prompt_lang": "zh",
        "text": "合成文本",
        "text_lang": "zh",
    }

    # 1. Same engine, different external reference paths
    opts1 = {**opts_base, "ref_audio_path": "audio/remote_ref1.wav", "_engine_url": "http://127.0.0.1:9880"}
    opts2 = {**opts_base, "ref_audio_path": "audio/remote_ref2.wav", "_engine_url": "http://127.0.0.1:9880"}
    key1, _, _ = origin_env.cache.compute_cache_key("合成文本", opts1)
    key2, _, _ = origin_env.cache.compute_cache_key("合成文本", opts2)
    assert key1 != key2

    # 2. Same external reference path, different engine URLs
    opts3 = {**opts_base, "ref_audio_path": "audio/remote_ref1.wav", "_engine_url": "http://remote-gpu:9880"}
    key3, _, _ = origin_env.cache.compute_cache_key("合成文本", opts3)
    assert key1 != key3

    # 3. Same reference path, different prompt texts
    opts4 = {**opts_base, "ref_audio_path": "audio/remote_ref1.wav", "prompt_text": "不同提示文本", "_engine_url": "http://127.0.0.1:9880"}
    key4, _, _ = origin_env.cache.compute_cache_key("合成文本", opts4)
    assert key1 != key4

    # 4. Same parameters produce identical cache keys
    key1_repeat, _, _ = origin_env.cache.compute_cache_key("合成文本", opts1)
    assert key1 == key1_repeat


@pytest.mark.asyncio
async def test_unknown_remote_ref_preserved_despite_same_filename_in_other_and_unregistered_packages(origin_env):
    """Unknown remote refs/engine_only.wav is preserved even if another registered character

    and an unregistered directory have a file with the same name.
    """
    # Explicitly create collision files in other registered package and unregistered directory
    _create_wav(origin_env.other_dir / "refs" / "engine_only.wav", duration_sec=4.0, freq=550)
    unreg_dir = origin_env.root / "characters" / "unregistered"
    unreg_dir.mkdir(parents=True, exist_ok=True)
    _create_wav(unreg_dir / "engine_only.wav", duration_sec=4.0, freq=660)
    assert (origin_env.other_dir / "refs" / "engine_only.wav").is_file()
    assert (origin_env.root / "characters" / "unregistered" / "engine_only.wav").is_file()

    async with get_db(origin_env.db_path) as conn:
        profile = await crud.create_voice_profile(
            conn,
            VoiceProfileCreate(
                name="四季夏目",
                gpt_weights_path=str(origin_env.gpt_file),
                sovits_weights_path=str(origin_env.sovits_file),
                ref_audio_path="refs/engine_only.wav",
                prompt_text="引擎端独立参考文本",
                prompt_lang="zh",
                text_lang="ja",
            ),
        )
        await origin_env.mgr.sync_with_db(conn)

    # Fresh connection verification:
    async with get_db(origin_env.db_path) as new_conn:
        row = await crud.get_voice_profile(new_conn, profile.id)
        # Must retain original triplet; must NOT be hijacked to other package or unregistered directory
        assert row.ref_audio_path == "refs/engine_only.wav"
        assert row.prompt_text == "引擎端独立参考文本"
        assert row.prompt_lang == "zh"
        assert "other" not in row.ref_audio_path
        assert "unregistered" not in row.ref_audio_path

    # Resolver verification:
    resolver = get_voice_resolver()
    ctx = await resolver.resolve_context(str(origin_env.db_path), profile_id=profile.id)
    assert ctx.ref_audio_path == "refs/engine_only.wav"
    assert ctx.prompt_text == "引擎端独立参考文本"
    assert ctx.prompt_lang == "zh"


@pytest.mark.asyncio
async def test_same_filename_with_different_undeclared_path_not_swallowed_as_refs_alias(origin_env):
    """Same filename gentle.wav with different undeclared path is not swallowed as a refs/ alias."""
    async with get_db(origin_env.db_path) as conn:
        # Four Seasons Natsume (shiki) declared refs/gentle.wav in manifest.
        # This profile uses a different undeclared path: audio/custom/gentle.wav
        profile = await crud.create_voice_profile(
            conn,
            VoiceProfileCreate(
                name="四季夏目",
                gpt_weights_path=str(origin_env.gpt_file),
                sovits_weights_path=str(origin_env.sovits_file),
                ref_audio_path="audio/custom/gentle.wav",
                prompt_text="外部同名音频文本",
                prompt_lang="zh",
                text_lang="ja",
            ),
        )
        await origin_env.mgr.sync_with_db(conn)

    # Fresh connection verification:
    async with get_db(origin_env.db_path) as new_conn:
        row = await crud.get_voice_profile(new_conn, profile.id)
        assert row.ref_audio_path == "audio/custom/gentle.wav"
        assert row.prompt_text == "外部同名音频文本"
        assert row.prompt_lang == "zh"

    # Resolver verification:
    resolver = get_voice_resolver()
    ctx = await resolver.resolve_context(str(origin_env.db_path), profile_id=profile.id)
    assert ctx.ref_audio_path == "audio/custom/gentle.wav"
    assert ctx.prompt_text == "外部同名音频文本"
    assert ctx.prompt_lang == "zh"
