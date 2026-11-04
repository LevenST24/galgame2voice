"""Tests for authoritative reference caching and identity resolution contracts.

Verifies:
1. Different external reference paths under authoritative profile resolution do not
   fold onto default character package reference files, producing distinct cache keys.
2. Identical external reference options produce stable cache keys.
3. Modification of a default character package reference file does not trigger
   validate_sources errors for unknown remote reference paths.
4. Modification of a genuinely resolved local reference file still triggers
   validate_sources errors.
5. Legacy resolution without authoritative profile resolution still preserves package fallback.
"""

import asyncio
import json
from pathlib import Path
import struct
from types import SimpleNamespace
import wave

import pytest

from galgame2voice.config import get_settings
from galgame2voice.database import crud
from galgame2voice.database.models import VoiceProfileCreate
from galgame2voice.database.session import get_db, init_db
from galgame2voice.services.audio_identity import AudioIdentity
from galgame2voice.services.character_manager import CharacterManager
from galgame2voice.services.tts_cache_manager import TtsCacheManager
from galgame2voice.services.voice_resolver import get_voice_resolver
from galgame2voice.utils.path_guard import resolve_existing_audio_path


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


@pytest.fixture
async def cache_env(tmp_path, monkeypatch):
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

    # Create default character package with a real 4-second WAV
    chars_dir = tmp_path / "characters" / "shiki"
    chars_dir.mkdir(parents=True, exist_ok=True)
    gentle_wav = _create_wav(chars_dir / "refs" / "gentle.wav", duration_sec=4.0, freq=440)

    gpt_file = chars_dir / "weights" / "char.ckpt"
    sovits_file = chars_dir / "weights" / "char.pth"
    gpt_file.parent.mkdir(parents=True, exist_ok=True)
    gpt_file.write_bytes(b"synthetic gpt weights")
    sovits_file.write_bytes(b"synthetic sovits weights")

    manifest = {
        "id": "shiki",
        "name": "四季夏目",
        "version": "1.0.0",
        "is_default": True,
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
    (chars_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

    await init_db(db_file)
    async with get_db(db_file) as conn:
        profile = await crud.create_voice_profile(
            conn,
            VoiceProfileCreate(
                name="四季夏目",
                gpt_weights_path=str(gpt_file),
                sovits_weights_path=str(sovits_file),
                ref_audio_path="refs/gentle.wav",
                prompt_text="四季夏目、よろしくお願いします。（gentle）",
                prompt_lang="ja",
                text_lang="ja",
            ),
        )

    cache = TtsCacheManager(cache_dir=tmp_path / "audio" / "cache", db_path=str(db_file))
    client = SimpleNamespace(base_url="http://127.0.0.1:9880")

    yield SimpleNamespace(
        root=tmp_path,
        chars_dir=chars_dir,
        gentle_wav=gentle_wav,
        gpt_file=gpt_file,
        sovits_file=sovits_file,
        db_file=db_file,
        profile=profile,
        cache=cache,
        client=client,
    )
    await cache.aclose()


@pytest.mark.asyncio
async def test_different_remote_refs_distinct_cache_keys(cache_env):
    """Two different remote audio paths under authoritative profile resolution

    must produce different cache keys and different reference fingerprints,
    rather than collapsing onto the default package's gentle.wav.
    """
    opts_base = {
        "voice_profile_id": cache_env.profile.id,
        "gpt_weights_path": str(cache_env.gpt_file),
        "sovits_weights_path": str(cache_env.sovits_file),
        "prompt_text": "测试台词",
        "prompt_lang": "zh",
        "text_lang": "ja",
        "_engine_url": "http://127.0.0.1:9880",
        "_profile_prompt_resolved": True,
    }

    opts_a = {**opts_base, "ref_audio_path": "refs/remote_a/gentle.wav"}
    opts_b = {**opts_base, "ref_audio_path": "refs/remote_b/gentle.wav"}

    id_a = AudioIdentity.capture(opts_a)
    id_b = AudioIdentity.capture(opts_b)

    # Reference fingerprints must differ
    assert id_a.reference.fingerprint() != id_b.reference.fingerprint()
    assert id_a.reference.path != id_b.reference.path

    # Cache keys must differ
    key_a, _, _ = cache_env.cache.compute_cache_key("合成台词", opts_a)
    key_b, _, _ = cache_env.cache.compute_cache_key("合成台词", opts_b)
    assert key_a != key_b

    # Repeated computation must be stable
    key_a_repeat, _, _ = cache_env.cache.compute_cache_key("合成台词", opts_a)
    assert key_a == key_a_repeat


@pytest.mark.asyncio
async def test_default_package_modification_does_not_invalidate_unknown_remote_ref(cache_env):
    """Modifying default character package's gentle.wav must not cause validate_sources

    to fail for an unknown external reference path.
    """
    opts = {
        "voice_profile_id": cache_env.profile.id,
        "gpt_weights_path": str(cache_env.gpt_file),
        "sovits_weights_path": str(cache_env.sovits_file),
        "ref_audio_path": "refs/remote_a/gentle.wav",
        "prompt_text": "测试台词",
        "prompt_lang": "zh",
        "text_lang": "ja",
        "_engine_url": "http://127.0.0.1:9880",
        "_profile_prompt_resolved": True,
    }

    identity = AudioIdentity.capture(opts)

    # Modify the default package gentle.wav
    await asyncio.sleep(0.01)
    cache_env.gentle_wav.write_bytes(_create_wav(cache_env.gentle_wav, duration_sec=4.0, freq=880).read_bytes())

    # Validation must succeed without raising ValueError
    identity.validate_sources(opts, cache_env.client)


@pytest.mark.asyncio
async def test_truly_explicit_local_reference_modification_still_invalidates(cache_env):
    """Modifying a genuinely resolved local file must still invalidate the identity."""
    local_ref = cache_env.root / "explicit_local.wav"
    _create_wav(local_ref, duration_sec=4.0, freq=440)

    opts = {
        "voice_profile_id": cache_env.profile.id,
        "gpt_weights_path": str(cache_env.gpt_file),
        "sovits_weights_path": str(cache_env.sovits_file),
        "ref_audio_path": str(local_ref),
        "prompt_text": "测试台词",
        "prompt_lang": "zh",
        "text_lang": "ja",
        "_engine_url": "http://127.0.0.1:9880",
        "_profile_prompt_resolved": True,
    }

    identity = AudioIdentity.capture(opts)
    assert identity.reference.revision is not None

    # Modify the actual local file
    await asyncio.sleep(0.01)
    _create_wav(local_ref, duration_sec=4.0, freq=880)

    with pytest.raises(ValueError, match="音色文件在等待合成期间发生变化"):
        identity.validate_sources(opts, cache_env.client)


@pytest.mark.asyncio
async def test_legacy_package_fallback_preserved_without_profile_resolved(cache_env):
    """Without _profile_prompt_resolved, legacy caller paths that match a package

    continue to resolve via package search, preserving backward compatibility.
    """
    resolved = resolve_existing_audio_path("refs/gentle.wav")
    assert resolved is not None
    assert resolved.resolve() == cache_env.gentle_wav.resolve()

    # AudioIdentity without _profile_prompt_resolved retains legacy fallback
    legacy_opts = {
        "ref_audio_path": "refs/gentle.wav",
        "gpt_weights_path": str(cache_env.gpt_file),
        "sovits_weights_path": str(cache_env.sovits_file),
    }
    identity = AudioIdentity.capture(legacy_opts)
    assert identity.reference.revision is not None
