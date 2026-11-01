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


def _install_test_character(root: Path) -> Path:
    pkg = root / "characters" / "testchar"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "gpt.ckpt").write_bytes(b"synthetic gpt model marker")
    (pkg / "sovits.pth").write_bytes(b"synthetic sovits model marker")
    _make_wav(pkg / "gentle.wav", amplitude=500, duration_s=4.0)
    _make_wav(pkg / "happy.wav", amplitude=1000, duration_s=4.0)
    _make_wav(pkg / "sad.wav", amplitude=1500, duration_s=4.0)
    (pkg / "manifest.json").write_text(
        json.dumps({
            "id": "testchar",
            "name": "TestChar",
            "gpt_weights": "gpt.ckpt",
            "sovits_weights": "sovits.pth",
            "system_prompt": "测试角色人设",
            "emotions": {
                "gentle": {"audio": "gentle.wav", "text": "温柔台词", "lang": "ja"},
                "happy": {"audio": "happy.wav", "text": "开心台词", "lang": "ja"},
                "sad": {"audio": "sad.wav", "text": "悲伤台词", "lang": "ja"},
            },
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    return pkg


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
    monkeypatch.setenv("GALGAME2VOICE_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("GALGAME2VOICE_SKIP_MEM_CHECK", "1")
    get_settings.cache_clear()
    CharacterManager.reset_instance()

    pkg = _install_test_character(tmp_path)
    gentle_wav = str((pkg / "gentle.wav").resolve())
    happy_wav = str((pkg / "happy.wav").resolve())
    sad_wav = str((pkg / "sad.wav").resolve())

    db_path = str(tmp_path / "test.db")
    await init_db(db_path)

    async with get_db(db_path) as conn:
        profile = await crud.create_voice_profile(
            conn,
            VoiceProfileCreate(
                name="TestChar",
                gpt_weights_path=str((pkg / "gpt.ckpt").resolve()),
                sovits_weights_path=str((pkg / "sovits.pth").resolve()),
                ref_audio_path=gentle_wav,
                prompt_text="温柔台词",
                prompt_lang="ja",
                is_default=True,
            ),
        )
        await crud.set_active_voice_profile(conn, profile.id)

    engine = MockEngine()
    client = GptSovitsClient(server=engine)
    assert await client.switch_voice_profile(profile)

    scheduler = TtsScheduler()
    monkeypatch.setattr("galgame2voice.services.tts_service.get_tts_scheduler", lambda: scheduler)

    cache = TtsCacheManager(cache_dir=tmp_path / "cache", db_path=db_path)
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


@pytest.mark.asyncio
async def test_resolved_voice_context_emotions_and_synonyms(tmp_path, monkeypatch):
    monkeypatch.setenv("GALGAME2VOICE_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("GALGAME2VOICE_SKIP_MEM_CHECK", "1")
    get_settings.cache_clear()
    CharacterManager.reset_instance()

    pkg = _install_test_character(tmp_path)
    gentle_wav = str((pkg / "gentle.wav").resolve())
    happy_wav = str((pkg / "happy.wav").resolve())
    sad_wav = str((pkg / "sad.wav").resolve())

    db_path = str(tmp_path / "test.db")
    await init_db(db_path)

    async with get_db(db_path) as conn:
        profile = await crud.create_voice_profile(
            conn,
            VoiceProfileCreate(
                name="TestChar",
                gpt_weights_path=str((pkg / "gpt.ckpt").resolve()),
                sovits_weights_path=str((pkg / "sovits.pth").resolve()),
                ref_audio_path=gentle_wav,
                prompt_text="温柔台词",
                prompt_lang="ja",
                is_default=True,
            ),
        )

    resolver = VoiceProfileResolver()
    ctx = await resolver.resolve_context(db_path=db_path, profile_id=profile.id)
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
    monkeypatch.setenv("GALGAME2VOICE_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("GALGAME2VOICE_SKIP_MEM_CHECK", "1")
    get_settings.cache_clear()
    CharacterManager.reset_instance()

    pkg_dir = tmp_path / "characters" / "fullchar"
    pkg_dir.mkdir(parents=True, exist_ok=True)
    (pkg_dir / "gpt.ckpt").write_bytes(b"synthetic gpt model marker")
    (pkg_dir / "sovits.pth").write_bytes(b"synthetic sovits model marker")

    emotions_dict = {}
    for idx, emo in enumerate(sorted(VALID_EMOTIONS), start=1):
        wav_path = pkg_dir / f"{emo}.wav"
        _make_wav(wav_path, amplitude=idx * 200, duration_s=4.0)
        emotions_dict[emo] = {"audio": f"{emo}.wav", "text": f"{emo} prompt", "lang": "ja"}

    (pkg_dir / "manifest.json").write_text(
        json.dumps({
            "id": "fullchar",
            "name": "FullChar",
            "gpt_weights": "gpt.ckpt",
            "sovits_weights": "sovits.pth",
            "emotions": emotions_dict,
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    db_path = str(tmp_path / "test.db")
    await init_db(db_path)

    async with get_db(db_path) as conn:
        profile = await crud.create_voice_profile(
            conn,
            VoiceProfileCreate(
                name="FullChar",
                gpt_weights_path=str((pkg_dir / "gpt.ckpt").resolve()),
                sovits_weights_path=str((pkg_dir / "sovits.pth").resolve()),
                ref_audio_path=str((pkg_dir / "gentle.wav").resolve()),
                prompt_text="gentle prompt",
                prompt_lang="ja",
                is_default=True,
            ),
        )

    resolver = VoiceProfileResolver()
    ctx = await resolver.resolve_context(db_path=db_path, profile_id=profile.id)
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
    monkeypatch.setenv("GALGAME2VOICE_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("GALGAME2VOICE_SKIP_MEM_CHECK", "1")
    get_settings.cache_clear()
    CharacterManager.reset_instance()

    pkg_dir = tmp_path / "characters" / "customchar"
    pkg_dir.mkdir(parents=True, exist_ok=True)
    (pkg_dir / "gpt.ckpt").write_bytes(b"synthetic gpt model marker")
    (pkg_dir / "sovits.pth").write_bytes(b"synthetic sovits model marker")
    _make_wav(pkg_dir / "gentle.wav", amplitude=500, duration_s=4.0)
    _make_wav(pkg_dir / "yandere.wav", amplitude=1000, duration_s=4.0)
    _make_wav(pkg_dir / "custom_mood.wav", amplitude=1500, duration_s=4.0)

    (pkg_dir / "manifest.json").write_text(
        json.dumps({
            "id": "customchar",
            "name": "CustomChar",
            "gpt_weights": "gpt.ckpt",
            "sovits_weights": "sovits.pth",
            "emotions": {
                "gentle": {"audio": "gentle.wav", "text": "gentle text", "lang": "ja"},
                "yandere": {"audio": "yandere.wav", "text": "yandere text", "lang": "ja"},
                "custom_mood": {"audio": "custom_mood.wav", "text": "custom mood text", "lang": "ja"},
            },
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    db_path = str(tmp_path / "test.db")
    await init_db(db_path)

    async with get_db(db_path) as conn:
        profile = await crud.create_voice_profile(
            conn,
            VoiceProfileCreate(
                name="CustomChar",
                gpt_weights_path=str((pkg_dir / "gpt.ckpt").resolve()),
                sovits_weights_path=str((pkg_dir / "sovits.pth").resolve()),
                ref_audio_path=str((pkg_dir / "gentle.wav").resolve()),
                prompt_text="gentle text",
                prompt_lang="ja",
                is_default=True,
            ),
        )

    resolver = VoiceProfileResolver()
    ctx = await resolver.resolve_context(db_path=db_path, profile_id=profile.id)
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
    monkeypatch.setenv("GALGAME2VOICE_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("GALGAME2VOICE_SKIP_MEM_CHECK", "1")
    get_settings.cache_clear()
    CharacterManager.reset_instance()

    pkg_dir = tmp_path / "characters" / "aliaschar"
    pkg_dir.mkdir(parents=True, exist_ok=True)
    (pkg_dir / "gpt.ckpt").write_bytes(b"synthetic gpt model marker")
    (pkg_dir / "sovits.pth").write_bytes(b"synthetic sovits model marker")
    _make_wav(pkg_dir / "gentle.wav", amplitude=500, duration_s=4.0)
    _make_wav(pkg_dir / "happy_long.wav", amplitude=1000, duration_s=6.0)
    _make_wav(pkg_dir / "joy_short.wav", amplitude=1500, duration_s=4.0)

    (pkg_dir / "manifest.json").write_text(
        json.dumps({
            "id": "aliaschar",
            "name": "AliasChar",
            "gpt_weights": "gpt.ckpt",
            "sovits_weights": "sovits.pth",
            "emotions": {
                "gentle": {"audio": "gentle.wav", "text": "gentle text", "lang": "ja"},
                "happy": {"audio": "happy_long.wav", "text": "canonical happy text", "lang": "ja"},
                "joy": {"audio": "joy_short.wav", "text": "alias joy text", "lang": "ja"},
            },
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    db_path = str(tmp_path / "test.db")
    await init_db(db_path)

    async with get_db(db_path) as conn:
        profile = await crud.create_voice_profile(
            conn,
            VoiceProfileCreate(
                name="AliasChar",
                gpt_weights_path=str((pkg_dir / "gpt.ckpt").resolve()),
                sovits_weights_path=str((pkg_dir / "sovits.pth").resolve()),
                ref_audio_path=str((pkg_dir / "gentle.wav").resolve()),
                prompt_text="gentle text",
                prompt_lang="ja",
                is_default=True,
            ),
        )

    resolver = VoiceProfileResolver()
    ctx = await resolver.resolve_context(db_path=db_path, profile_id=profile.id)
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
