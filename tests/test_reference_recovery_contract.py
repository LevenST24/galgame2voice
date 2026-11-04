"""Reference audio recovery contract tests.

Verifies that when reference audio falls back to a character package's default
emotion (gentle or first available), the prompt text and language are updated
to match the fallback audio, preventing audio-transcript mismatches.
Also validates explicit overrides, valid custom references, and remote path boundaries.
"""

import json
import struct
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

from galgame2voice.config import get_settings
from galgame2voice.database import crud
from galgame2voice.database.models import VoiceProfileCreate
from galgame2voice.database.session import get_db, init_db
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
async def char_env(tmp_path, monkeypatch):
    monkeypatch.setenv("GALGAME2VOICE_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("GALGAME2VOICE_CHARACTERS_DIR", str(tmp_path / "characters"))
    monkeypatch.setenv("GALGAME2VOICE_AUDIO_DIR", str(tmp_path / "audio"))
    monkeypatch.setenv("GALGAME2VOICE_SKIP_MEM_CHECK", "1")
    get_settings.cache_clear()
    CharacterManager.reset_instance()
    get_voice_resolver().invalidate()

    chars_dir = tmp_path / "characters" / "shiki"
    chars_dir.mkdir(parents=True, exist_ok=True)

    gentle_wav = _create_wav(chars_dir / "refs" / "gentle.wav", duration_sec=4.0, freq=440)
    joy_wav = _create_wav(chars_dir / "refs" / "joy.wav", duration_sec=4.0, freq=880)
    custom_wav = _create_wav(tmp_path / "custom" / "valid_custom.wav", duration_sec=4.0, freq=220)

    gpt_file = chars_dir / "weights" / "char.ckpt"
    sovits_file = chars_dir / "weights" / "char.pth"
    gpt_file.parent.mkdir(parents=True, exist_ok=True)
    gpt_file.write_bytes(b"synthetic gpt weights")
    sovits_file.write_bytes(b"synthetic sovits weights")

    manifest_data = {
        "id": "shiki",
        "name": "四季夏目",
        "version": "1.0.0",
        "description": "测试角色描述",
        "gpt_weights": "weights/char.ckpt",
        "sovits_weights": "weights/char.pth",
        "emotions": {
            "gentle": {
                "audio": "refs/gentle.wav",
                "text": "四季夏目、よろしくお願いします。（gentle）",
                "lang": "ja",
            },
            "joy": {
                "audio": "refs/joy.wav",
                "text": "とても嬉しいです！（joy）",
                "lang": "ja",
            },
        },
    }
    (chars_dir / "manifest.json").write_text(json.dumps(manifest_data, ensure_ascii=False), encoding="utf-8")

    mgr = get_character_manager(characters_dir=tmp_path / "characters")
    discovered = mgr.discover_characters()
    assert len(discovered) == 1
    assert discovered[0].is_valid

    db_path = tmp_path / "voices.db"
    await init_db(db_path)

    client = GptSovitsClient(server=DummyEngine())
    cache = TtsCacheManager(cache_dir=tmp_path / "audio" / "cache", db_path=str(db_path))
    service = TtsService(client=client, cache_manager=cache, db_path=str(db_path), audio_dir=tmp_path / "audio")

    yield SimpleNamespace(
        root=tmp_path,
        chars_dir=chars_dir,
        db_path=db_path,
        gentle_wav=gentle_wav,
        joy_wav=joy_wav,
        custom_wav=custom_wav,
        gpt_file=gpt_file,
        sovits_file=sovits_file,
        service=service,
        client=client,
        cache=cache,
    )
    await cache.aclose()
    await client.aclose()
    CharacterManager.reset_instance()
    get_voice_resolver().invalidate()


@pytest.mark.asyncio
async def test_missing_ref_audio_falls_back_to_gentle_with_matching_text_and_lang(char_env):
    """When a profile's ref_audio is missing, falling back to gentle must pair with gentle's text and lang."""
    async with get_db(char_env.db_path) as conn:
        profile = await crud.create_voice_profile(
            conn,
            VoiceProfileCreate(
                name="四季夏目",
                gpt_weights_path=str(char_env.gpt_file),
                sovits_weights_path=str(char_env.sovits_file),
                ref_audio_path=str(char_env.chars_dir / "refs" / "deleted_audio.wav"),  # Missing file
                prompt_text="这是被删除参考的旧文本",
                prompt_lang="zh",
                text_lang="ja",
            ),
        )

    # 1. Verify via TtsService voice options population entry point
    opts = {"voice_profile_id": profile.id}
    resolved = await char_env.service._populate_voice_profile_opts(opts)

    assert Path(resolved["ref_audio_path"]) == char_env.gentle_wav
    assert resolved["prompt_text"] == "四季夏目、よろしくお願いします。（gentle）"
    assert resolved["prompt_lang"] == "ja"

    # 2. Verify via VoiceProfileResolver context
    resolver = get_voice_resolver()
    ctx = await resolver.resolve_context(str(char_env.db_path), profile_id=profile.id)
    assert ctx is not None
    assert Path(ctx.ref_audio_path) == char_env.gentle_wav
    assert ctx.prompt_text == "四季夏目、よろしくお願いします。（gentle）"
    assert ctx.prompt_lang == "ja"


@pytest.mark.asyncio
async def test_missing_ref_audio_falls_back_to_first_emotion_when_no_gentle(char_env):
    """When gentle is absent, falling back to the first available emotion must pair with that emotion's text."""
    # Create a second package without gentle emotion
    other_dir = char_env.root / "characters" / "other"
    other_dir.mkdir(parents=True, exist_ok=True)
    joy_wav = _create_wav(other_dir / "refs" / "joy.wav", duration_sec=4.0)
    gpt_file = other_dir / "weights" / "char.ckpt"
    sovits_file = other_dir / "weights" / "char.pth"
    gpt_file.parent.mkdir(parents=True, exist_ok=True)
    gpt_file.write_bytes(b"synthetic weights")
    sovits_file.write_bytes(b"synthetic weights")

    manifest = {
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
    (other_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

    mgr = get_character_manager(characters_dir=char_env.root / "characters")
    mgr.discover_characters()

    async with get_db(char_env.db_path) as conn:
        profile = await crud.create_voice_profile(
            conn,
            VoiceProfileCreate(
                name="其他角色",
                gpt_weights_path=str(gpt_file),
                sovits_weights_path=str(sovits_file),
                ref_audio_path=str(other_dir / "refs" / "missing.wav"),
                prompt_text="旧文本（不应保留）",
                prompt_lang="zh",
                text_lang="ja",
            ),
        )

    opts = {"voice_profile_id": profile.id}
    resolved = await char_env.service._populate_voice_profile_opts(opts)

    assert Path(resolved["ref_audio_path"]) == joy_wav
    assert resolved["prompt_text"] == "楽しいです！（joy）"
    assert resolved["prompt_lang"] == "ja"


@pytest.mark.asyncio
async def test_profile_with_empty_prompt_and_valid_custom_ref_preserves_empty_transcript(char_env):
    """A profile with a valid custom reference audio and an empty prompt_text preserves the empty transcript."""
    async with get_db(char_env.db_path) as conn:
        profile = await crud.create_voice_profile(
            conn,
            VoiceProfileCreate(
                name="四季夏目",
                gpt_weights_path=str(char_env.gpt_file),
                sovits_weights_path=str(char_env.sovits_file),
                ref_audio_path=str(char_env.custom_wav),  # Valid existing custom reference
                prompt_text="",  # Legitimate empty transcript
                prompt_lang="ja",
                text_lang="ja",
            ),
        )

    opts = {"voice_profile_id": profile.id}
    resolved = await char_env.service._populate_voice_profile_opts(opts)

    assert Path(resolved["ref_audio_path"]) == char_env.custom_wav
    assert resolved["prompt_text"] == ""
    assert resolved["prompt_lang"] == "ja"

    resolver = get_voice_resolver()
    ctx = await resolver.resolve_context(str(char_env.db_path), profile_id=profile.id)
    assert ctx is not None
    assert Path(ctx.ref_audio_path) == char_env.custom_wav
    assert ctx.prompt_text == ""
    assert ctx.prompt_lang == "ja"


@pytest.mark.asyncio
async def test_valid_custom_ref_audio_preserves_custom_text_and_lang(char_env):
    """When a profile has a valid existing custom reference audio, its text and lang must be preserved."""
    async with get_db(char_env.db_path) as conn:
        profile = await crud.create_voice_profile(
            conn,
            VoiceProfileCreate(
                name="四季夏目",
                gpt_weights_path=str(char_env.gpt_file),
                sovits_weights_path=str(char_env.sovits_file),
                ref_audio_path=str(char_env.custom_wav),  # Valid existing file
                prompt_text="自定义有效参考文本",
                prompt_lang="zh",
                text_lang="ja",
            ),
        )

    opts = {"voice_profile_id": profile.id}
    resolved = await char_env.service._populate_voice_profile_opts(opts)

    assert Path(resolved["ref_audio_path"]) == char_env.custom_wav
    assert resolved["prompt_text"] == "自定义有效参考文本"
    assert resolved["prompt_lang"] == "zh"


@pytest.mark.asyncio
async def test_explicit_options_override_takes_precedence(char_env):
    """Explicit ref_audio_path and prompt_text in options override profile defaults."""
    async with get_db(char_env.db_path) as conn:
        profile = await crud.create_voice_profile(
            conn,
            VoiceProfileCreate(
                name="四季夏目",
                gpt_weights_path=str(char_env.gpt_file),
                sovits_weights_path=str(char_env.sovits_file),
                ref_audio_path=str(char_env.chars_dir / "refs" / "deleted_audio.wav"),
                prompt_text="这是被删除参考的旧文本",
                prompt_lang="zh",
                text_lang="ja",
            ),
        )

    override_wav = _create_wav(char_env.root / "override.wav", duration_sec=4.0)
    opts = {
        "voice_profile_id": profile.id,
        "ref_audio_path": str(override_wav),
        "prompt_text": "单次显式覆盖文本",
        "prompt_lang": "en",
    }
    resolved = await char_env.service._populate_voice_profile_opts(opts)

    assert Path(resolved["ref_audio_path"]) == override_wav
    assert resolved["prompt_text"] == "单次显式覆盖文本"
    assert resolved["prompt_lang"] == "en"


@pytest.mark.asyncio
async def test_normal_emotion_selection_pairs_emotion_audio_and_text(char_env):
    """Normal adaptive emotion selection must pair joy audio with joy text."""
    async with get_db(char_env.db_path) as conn:
        profile = await crud.create_voice_profile(
            conn,
            VoiceProfileCreate(
                name="四季夏目",
                gpt_weights_path=str(char_env.gpt_file),
                sovits_weights_path=str(char_env.sovits_file),
                ref_audio_path=str(char_env.gentle_wav),
                prompt_text="温柔默认台词",
                prompt_lang="ja",
                text_lang="ja",
            ),
        )

    opts = {
        "voice_profile_id": profile.id,
        "emotion": "joy",
        "ai_adaptive_voice": True,
    }
    resolved = await char_env.service._populate_voice_profile_opts(opts)

    assert Path(resolved["ref_audio_path"]) == char_env.joy_wav
    assert resolved["prompt_text"] == "とても嬉しいです！（joy）"
    assert resolved["prompt_lang"] == "ja"


@pytest.mark.asyncio
async def test_standalone_remote_reference_path_is_preserved_without_local_exists_check(char_env):
    """Standalone profiles (not bound to local packages) preserve remote paths and transcripts."""
    remote_path = "/remote/gpt_sovits/audio/ref_remote.wav"
    raw = {
        "id": 100,
        "name": "StandaloneRemoteVoice",
        "gpt_weights_path": "/remote/gpt_sovits/models/char.ckpt",
        "sovits_weights_path": "/remote/gpt_sovits/models/char.pth",
        "ref_audio_path": remote_path,
        "prompt_text": "远端参考音频文本",
        "prompt_lang": "zh",
        "text_lang": "zh",
    }
    resolver = get_voice_resolver()
    # pkg=None indicates no local character package claims this voice
    ctx = resolver._build_context(raw, package=None)

    assert ctx.ref_audio_path == remote_path
    assert ctx.prompt_text == "远端参考音频文本"
    assert ctx.prompt_lang == "zh"
