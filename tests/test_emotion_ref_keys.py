"""
Regression test: AI adaptive voice emotion reference audio resolution.
Verifies that when ai_adaptive is enabled, emotions like happy and sad switch
to their respective emotion reference audio rather than falling back to default ref audio.
"""

import io
import json
from pathlib import Path
import wave

import httpx
import pytest

from galgame2voice.config import get_settings
from galgame2voice.database import crud
from galgame2voice.database.models import VoiceProfileCreate
from galgame2voice.database.session import get_db, init_db
from galgame2voice.services.character_manager import CharacterManager
from galgame2voice.services.emotion_classifier import VALID_EMOTIONS
from galgame2voice.services.gpt_sovits_client import GptSovitsClient
from galgame2voice.services.tts_cache_manager import TtsCacheManager
from galgame2voice.services.tts_scheduler import TtsScheduler
from galgame2voice.services.tts_service import TtsService
from galgame2voice.services.voice_resolver import VoiceProfileResolver


def _make_wav(path: Path, amplitude: int = 1000, duration_s: float = 4.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(amplitude.to_bytes(2, "little", signed=True) * int(8000 * duration_s))
    return path


def _same_path(p1: str | Path, p2: str | Path) -> bool:
    return Path(p1).resolve() == Path(p2).resolve()


def _setup_env(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("GALGAME2VOICE_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("GALGAME2VOICE_SKIP_MEM_CHECK", "1")
    get_settings.cache_clear()
    CharacterManager.reset_instance()


def _create_character_package(
    root: Path,
    char_id: str,
    name: str,
    emotions: dict[str, dict],
    *,
    system_prompt: str | None = None,
) -> Path:
    pkg = root / "characters" / char_id
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "gpt.ckpt").write_bytes(b"synthetic gpt model marker")
    (pkg / "sovits.pth").write_bytes(b"synthetic sovits model marker")
    manifest_emotions = {}
    for idx, (emo, cfg) in enumerate(emotions.items(), start=1):
        audio = cfg["audio"]
        _make_wav(
            pkg / audio,
            amplitude=cfg.get("amplitude", idx * 500),
            duration_s=cfg.get("duration_s", 4.0),
        )
        manifest_emotions[emo] = {
            "audio": audio,
            "text": cfg["text"],
            "lang": cfg.get("lang", "ja"),
        }
    manifest = {
        "id": char_id,
        "name": name,
        "gpt_weights": "gpt.ckpt",
        "sovits_weights": "sovits.pth",
        "emotions": manifest_emotions,
    }
    if system_prompt is not None:
        manifest["system_prompt"] = system_prompt
    (pkg / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False),
        encoding="utf-8",
    )
    return pkg


def _install_test_character(root: Path) -> Path:
    return _create_character_package(
        root,
        "testchar",
        "TestChar",
        {
            "gentle": {"audio": "gentle.wav", "text": "温柔台词", "lang": "ja"},
            "happy": {"audio": "happy.wav", "text": "开心台词", "lang": "ja"},
            "sad": {"audio": "sad.wav", "text": "悲伤台词", "lang": "ja"},
        },
        system_prompt="测试角色人设",
    )


async def _create_profile(
    db_path: str,
    pkg: Path,
    name: str,
    ref_audio_name: str,
    prompt_text: str,
    *,
    set_active: bool = False,
):
    await init_db(db_path)
    async with get_db(db_path) as conn:
        profile = await crud.create_voice_profile(
            conn,
            VoiceProfileCreate(
                name=name,
                gpt_weights_path=str((pkg / "gpt.ckpt").resolve()),
                sovits_weights_path=str((pkg / "sovits.pth").resolve()),
                ref_audio_path=str((pkg / ref_audio_name).resolve()),
                prompt_text=prompt_text,
                prompt_lang="ja",
                is_default=True,
            ),
        )
        if set_active:
            await crud.set_active_voice_profile(conn, profile.id)
    return profile


async def _resolve_profile_context(
    tmp_path: Path,
    pkg: Path,
    name: str,
    ref_audio_name: str,
    prompt_text: str,
):
    db_path = str(tmp_path / "test.db")
    profile = await _create_profile(db_path, pkg, name, ref_audio_name, prompt_text)
    resolver = VoiceProfileResolver()
    return await resolver.resolve_context(db_path=db_path, profile_id=profile.id)


class MockEngine:
    def __init__(self):
        self.tts_calls = []

    async def handle_request(
        self,
        method: str,
        path: str,
        json_data: dict | None = None,
        params: dict | None = None,
        **kwargs,
    ) -> httpx.Response:
        if path in ("/control", "/health", "/"):
            return httpx.Response(200, json={"status": "running"})
        if path in ("/set_gpt_weights", "/set_sovits_weights", "/set_refer_audio"):
            return httpx.Response(200, json={"code": 0})
        if path == "/tts":
            payload = json_data or params or {}
            self.tts_calls.append(payload)
            buffer = io.BytesIO()
            with wave.open(buffer, "wb") as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(8000)
                audio.writeframes((1000).to_bytes(2, "little", signed=True) * 8000)
            return httpx.Response(200, content=buffer.getvalue(), headers={"Content-Type": "audio/wav"})
        return httpx.Response(404, json={"error": "not found"})


@pytest.mark.asyncio
async def test_ai_adaptive_voice_switches_emotion_references(tmp_path, monkeypatch):
    _setup_env(tmp_path, monkeypatch)

    pkg = _install_test_character(tmp_path)
    gentle_wav = str((pkg / "gentle.wav").resolve())
    happy_wav = str((pkg / "happy.wav").resolve())
    sad_wav = str((pkg / "sad.wav").resolve())

    db_path = str(tmp_path / "test.db")
    profile = await _create_profile(db_path, pkg, "TestChar", "gentle.wav", "温柔台词", set_active=True)

    engine = MockEngine()
    client = GptSovitsClient(server=engine)
    scheduler = TtsScheduler()
    cache = TtsCacheManager(cache_dir=tmp_path / "cache", db_path=db_path)
    try:
        assert await client.switch_voice_profile(profile)

        monkeypatch.setattr("galgame2voice.services.tts_service.get_tts_scheduler", lambda: scheduler)
        tts_service = TtsService(client=client, cache_manager=cache, db_path=db_path, audio_dir=tmp_path / "audio")

        # 1. Synthesize with happy emotion -> happy.wav
        await tts_service.synthesize(
            "こんにちは！",
            options={
                "character_name": "TestChar",
                "ai_adaptive_voice": True,
                "emotion": "happy",
            },
            use_cache=False,
        )
        assert len(engine.tts_calls) == 1
        assert _same_path(engine.tts_calls[-1]["ref_audio_path"], happy_wav)

        # 2. Synthesize with sad emotion -> sad.wav
        await tts_service.synthesize(
            "さようなら……",
            options={
                "character_name": "TestChar",
                "ai_adaptive_voice": True,
                "emotion": "sad",
            },
            use_cache=False,
        )
        assert len(engine.tts_calls) == 2
        assert _same_path(engine.tts_calls[-1]["ref_audio_path"], sad_wav)

        # 3. Synthesize with synonym "joy" -> happy.wav
        await tts_service.synthesize(
            "嬉しいです！",
            options={
                "character_name": "TestChar",
                "ai_adaptive_voice": True,
                "emotion": "joy",
            },
            use_cache=False,
        )
        assert len(engine.tts_calls) == 3
        assert _same_path(engine.tts_calls[-1]["ref_audio_path"], happy_wav)

        # 4. Synthesize with Chinese synonym "开心" -> happy.wav
        await tts_service.synthesize(
            "今天真开心！",
            options={
                "character_name": "TestChar",
                "ai_adaptive_voice": True,
                "emotion": "开心",
            },
            use_cache=False,
        )
        assert len(engine.tts_calls) == 4
        assert _same_path(engine.tts_calls[-1]["ref_audio_path"], happy_wav)

        # 5. Synthesize with unmapped emotion -> falls back to default gentle.wav
        await tts_service.synthesize(
            "未定义的情绪",
            options={
                "character_name": "TestChar",
                "ai_adaptive_voice": True,
                "emotion": "mysterious_unknown",
            },
            use_cache=False,
        )
        assert len(engine.tts_calls) == 5
        assert _same_path(engine.tts_calls[-1]["ref_audio_path"], gentle_wav)

        # 6. Synthesize with ai_adaptive_voice=False -> keeps default gentle.wav
        await tts_service.synthesize(
            "自适应关闭",
            options={
                "character_name": "TestChar",
                "ai_adaptive_voice": False,
                "emotion": "happy",
            },
            use_cache=False,
        )
        assert len(engine.tts_calls) == 6
        assert _same_path(engine.tts_calls[-1]["ref_audio_path"], gentle_wav)
    finally:
        try:
            await scheduler.aclose()
        finally:
            try:
                await cache.aclose()
            finally:
                await client.aclose()


@pytest.mark.asyncio
async def test_resolved_voice_context_emotions_and_synonyms(tmp_path, monkeypatch):
    _setup_env(tmp_path, monkeypatch)

    pkg = _install_test_character(tmp_path)
    gentle_wav = str((pkg / "gentle.wav").resolve())
    happy_wav = str((pkg / "happy.wav").resolve())
    sad_wav = str((pkg / "sad.wav").resolve())

    ctx = await _resolve_profile_context(tmp_path, pkg, "TestChar", "gentle.wav", "温柔台词")
    assert ctx is not None

    # Exact canonical keys in emotions_map
    assert set(ctx.emotions.keys()) == {"gentle", "happy", "sad"}

    # Canonical and synonym lookups via get_emotion_ref
    happy_ref = ctx.get_emotion_ref("happy")
    assert happy_ref is not None
    assert _same_path(happy_ref["ref_audio_path"], happy_wav)

    assert ctx.get_emotion_ref("joy") == happy_ref
    assert ctx.get_emotion_ref("开心") == happy_ref
    assert ctx.get_emotion_ref("cheerful") == happy_ref

    sad_ref = ctx.get_emotion_ref("sad")
    assert sad_ref is not None
    assert _same_path(sad_ref["ref_audio_path"], sad_wav)

    assert ctx.get_emotion_ref("sorrow") == sad_ref
    assert ctx.get_emotion_ref("悲伤") == sad_ref

    gentle_ref = ctx.get_emotion_ref("gentle")
    assert gentle_ref is not None
    assert _same_path(gentle_ref["ref_audio_path"], gentle_wav)
    assert ctx.get_emotion_ref("温柔") == gentle_ref

    # Unknown and empty inputs return None
    assert ctx.get_emotion_ref("nonexistent_emotion") is None
    assert ctx.get_emotion_ref("") is None
    assert ctx.get_emotion_ref(None) is None


@pytest.mark.asyncio
async def test_all_valid_emotions_resolution(tmp_path, monkeypatch):
    """Verifies that all 7 VALID_EMOTIONS are resolved without duplicates or omissions."""
    _setup_env(tmp_path, monkeypatch)

    emotions_dict = {
        emo: {"audio": f"{emo}.wav", "text": f"{emo} prompt", "lang": "ja", "amplitude": idx * 200}
        for idx, emo in enumerate(sorted(VALID_EMOTIONS), start=1)
    }
    pkg_dir = _create_character_package(tmp_path, "fullchar", "FullChar", emotions_dict)

    ctx = await _resolve_profile_context(tmp_path, pkg_dir, "FullChar", "gentle.wav", "gentle prompt")
    assert ctx is not None

    # All 7 VALID_EMOTIONS must be in ctx.emotions, exactly 7 entries (no duplicate keys)
    assert set(ctx.emotions.keys()) == VALID_EMOTIONS
    assert len(ctx.emotions) == 7

    for emo in VALID_EMOTIONS:
        ref = ctx.get_emotion_ref(emo)
        assert ref is not None
        assert _same_path(ref["ref_audio_path"], pkg_dir / f"{emo}.wav")
        assert ref["prompt_text"] == f"{emo} prompt"


@pytest.mark.asyncio
async def test_manifest_custom_emotions_resolution(tmp_path, monkeypatch):
    """Verifies that custom manifest emotions (and unmapped keys like yandere) resolve properly and are not shadowed by gentle."""
    _setup_env(tmp_path, monkeypatch)

    pkg_dir = _create_character_package(
        tmp_path,
        "customchar",
        "CustomChar",
        {
            "gentle": {"audio": "gentle.wav", "text": "gentle text", "lang": "ja"},
            "yandere": {"audio": "yandere.wav", "text": "yandere text", "lang": "ja"},
            "custom_mood": {"audio": "custom_mood.wav", "text": "custom mood text", "lang": "ja"},
        },
    )

    ctx = await _resolve_profile_context(tmp_path, pkg_dir, "CustomChar", "gentle.wav", "gentle text")
    assert ctx is not None

    # gentle, yandere, and custom_mood must all be resolved
    assert "gentle" in ctx.emotions
    assert "yandere" in ctx.emotions
    assert "custom_mood" in ctx.emotions

    yan_ref = ctx.get_emotion_ref("yandere")
    assert yan_ref is not None
    assert _same_path(yan_ref["ref_audio_path"], pkg_dir / "yandere.wav")
    assert yan_ref["prompt_text"] == "yandere text"

    custom_ref = ctx.get_emotion_ref("custom_mood")
    assert custom_ref is not None
    assert _same_path(custom_ref["ref_audio_path"], pkg_dir / "custom_mood.wav")
    assert custom_ref["prompt_text"] == "custom mood text"


@pytest.mark.asyncio
async def test_conflicting_aliases_same_canonical_archetype(tmp_path, monkeypatch):
    """Verifies that if both canonical emotion (happy) and alias (joy) exist in manifest, only one entry is stored in emotions_map."""
    _setup_env(tmp_path, monkeypatch)

    pkg_dir = _create_character_package(
        tmp_path,
        "aliaschar",
        "AliasChar",
        {
            "gentle": {"audio": "gentle.wav", "text": "gentle text", "lang": "ja"},
            "happy": {"audio": "happy_long.wav", "text": "canonical happy text", "lang": "ja", "duration_s": 6.0},
            "joy": {"audio": "joy_short.wav", "text": "alias joy text", "lang": "ja"},
        },
    )

    ctx = await _resolve_profile_context(tmp_path, pkg_dir, "AliasChar", "gentle.wav", "gentle text")
    assert ctx is not None

    # Exactly one entry for happy canonical archetype
    assert "happy" in ctx.emotions
    assert "joy" not in ctx.emotions
    assert len(ctx.emotions) == 2  # gentle + happy

    happy_ref = ctx.get_emotion_ref("happy")
    joy_ref = ctx.get_emotion_ref("joy")
    assert happy_ref is not None
    assert joy_ref == happy_ref
    assert _same_path(happy_ref["ref_audio_path"], pkg_dir / "happy_long.wav")
    assert happy_ref["prompt_text"] == "canonical happy text"


def test_fallback_first_available_emotion_preserves_identity_when_no_gentle(tmp_path):
    """
    Verifies that when a character package lacks 'gentle' emotion and an emotion's audio file
    becomes missing after discovery, fallback resolves to the first available emotion
    (e.g. 'happy') and returns the real fallback audio path, prompt text/lang, and matched emotion 'happy'
    (instead of hardcoded 'gentle').
    """
    pkg_dir = _create_character_package(
        tmp_path,
        "nogentlechar",
        "NoGentleChar",
        {
            "happy": {"audio": "happy.wav", "text": "开心台词", "lang": "ja", "amplitude": 1000},
            "sad": {"audio": "sad.wav", "text": "悲伤台词", "lang": "ja", "amplitude": 1500},
        },
    )
    happy_wav = pkg_dir / "happy.wav"
    sad_wav = pkg_dir / "sad.wav"

    mgr = CharacterManager(tmp_path / "characters")
    mgr.discover_characters()

    # Remove ONLY the sad.wav test fixture audio file after discovery
    sad_wav.unlink()

    # Query 'sad' -> missing sad.wav should fall back to first available 'happy'
    res = mgr.resolve_emotion_audio_path("nogentlechar", "sad")
    assert res is not None
    assert _same_path(res["ref_audio_path"], happy_wav)
    assert res["prompt_text"] == "开心台词"
    assert res["prompt_lang"] == "ja"
    assert res["emotion"] == "happy"
