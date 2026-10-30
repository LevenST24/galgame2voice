"""Parenthesized custom voices stay independent of the character package they resemble."""

import json
from pathlib import Path
import wave

import pytest
from httpx import ASGITransport, AsyncClient

from galgame2voice.config import get_settings
from galgame2voice.main import create_app
from galgame2voice.services.character_manager import CharacterManager
from galgame2voice.services.voice_manager import VoiceManager
from galgame2voice.services.voice_resolver import VoiceProfileResolver


def _wav(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\x01\x00" * 8000 * 4)


def _install_anna(root: Path) -> Path:
    package = root / "characters" / "anna"
    _wav(package / "gentle.wav")
    (package / "gpt.ckpt").write_bytes(b"x" * 5000)
    (package / "sovits.pth").write_bytes(b"x" * 5000)
    (package / "manifest.json").write_text(json.dumps({
        "id": "anna",
        "name": "安娜",
        "aliases": ["Anna", "安娜 (特别)"],
        "gpt_weights": "gpt.ckpt",
        "sovits_weights": "sovits.pth",
        "system_prompt": "原包人设",
        "emotions": {"gentle": {"audio": "gentle.wav", "text": "原包台词", "lang": "ja"}},
    }, ensure_ascii=False), encoding="utf-8")
    return package


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("GALGAME2VOICE_PROJECT_ROOT", str(tmp_path))
    get_settings.cache_clear()
    CharacterManager.reset_instance()
    yield tmp_path
    CharacterManager.reset_instance()
    get_settings.cache_clear()


async def _accept_model_switch(self, *args, **kwargs):
    return True


def _profile_by_name(payload: dict, name: str) -> dict:
    matches = [row for row in payload["profiles"] if row["name"] == name]
    assert len(matches) == 1, payload["profiles"]
    return matches[0]


@pytest.mark.asyncio
async def test_parenthesized_variant_keeps_its_own_identity(project, isolate_test_database, monkeypatch):
    package = _install_anna(project)
    variant_audio = project / "voices" / "v2-ref.wav"
    _wav(variant_audio)
    monkeypatch.setattr(VoiceManager, "switch_profile", _accept_model_switch)

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        scanned = await client.post("/api/characters/rescan")
        assert scanned.status_code == 200, scanned.text
        assert scanned.json()["characters"] == ["安娜"]

        created = await client.post("/api/voice/profiles", json={
            "name": "安娜 (V2)",
            "gpt_weights_path": "characters/anna/gpt.ckpt",
            "sovits_weights_path": "characters/anna/sovits.pth",
            "ref_audio_path": "voices/v2-ref.wav",
            "prompt_text": "V2 的参考台词",
            "prompt_lang": "ja",
            "system_prompt": "变体原人设",
        })
        assert created.status_code == 201, created.text
        variant_id = created.json()["id"]

        switched = await client.post("/api/characters/switch", json={"character_name": "安娜 (V2)"})
        assert switched.status_code == 200, switched.text
        assert switched.json()["character"] == "安娜 (V2)"
        assert switched.json()["character_id"] == variant_id

        saved = await client.put(
            f"/api/characters/{variant_id}/system-prompt",
            json={"system_prompt": "变体新人设"},
        )
        assert saved.status_code == 200, saved.text
        manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["system_prompt"] == "原包人设"
        listed = (await client.get("/api/voice/profiles")).json()
        variant = _profile_by_name(listed, "安娜 (V2)")
        base = _profile_by_name(listed, "安娜")
        assert variant["system_prompt"] == "变体新人设"
        assert base["system_prompt"] == "原包人设"
        assert variant["prompt_text"] == "V2 的参考台词"
        assert "v2-ref.wav" in variant["ref_audio_path"].replace("\\", "/")

        resolver = VoiceProfileResolver()
        context = await resolver.resolve_context(
            db_path=isolate_test_database, character_name="安娜 (V2)",
        )
        assert context is not None
        assert context.profile_id == variant_id
        assert context.name == "安娜 (V2)"
        assert context.prompt_text == "V2 的参考台词"
        assert Path(context.ref_audio_path).name == "v2-ref.wav"
        package_gentle = (package / "gentle.wav").resolve()
        assert all(
            Path(item["ref_audio_path"]).resolve() != package_gentle
            for item in context.emotions.values()
        )

        for query in ("安娜 (Anna)", "安娜（Anna）", "安娜 (anna)", "安娜 (特别)"):
            bilingual = await client.post("/api/characters/switch", json={"character_name": query})
            assert bilingual.status_code == 200, bilingual.text
            assert bilingual.json()["character"] == "安娜"
            assert bilingual.json()["character_id"] == base["id"]
        bilingual_context = await resolver.resolve_context(
            db_path=isolate_test_database, character_name="安娜 (Anna)",
        )
        assert bilingual_context is not None
        assert bilingual_context.profile_id == base["id"]
        assert bilingual_context.name == "安娜"
        assert Path(bilingual_context.emotions["gentle"]["ref_audio_path"]).resolve() == package_gentle

        deleted = await client.delete(f"/api/voice/profiles/{base['id']}")
        assert deleted.status_code == 200, deleted.text
        rescanned = await client.post("/api/characters/rescan")
        assert rescanned.status_code == 200, rescanned.text
        restored = (await client.get("/api/voice/profiles")).json()
        kept = _profile_by_name(restored, "安娜 (V2)")
        recreated = _profile_by_name(restored, "安娜")
        assert kept["id"] == variant_id
        assert kept["prompt_text"] == "V2 的参考台词"
        assert kept["system_prompt"] == "变体新人设"
        assert "v2-ref.wav" in kept["ref_audio_path"].replace("\\", "/")
        assert recreated["id"] != variant_id
        assert "gentle.wav" in recreated["ref_audio_path"].replace("\\", "/")


async def test_unreadable_character_catalog_keeps_the_voice_usable(project, isolate_test_database, monkeypatch):
    """A failing package lookup leaves the voice on its own resources instead of blocking TTS."""
    _install_anna(project)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        scanned = await client.post("/api/characters/rescan")
        assert scanned.status_code == 200, scanned.text
        anna = _profile_by_name((await client.get("/api/voice/profiles")).json(), "安娜")

    def unreadable(self, *args, **kwargs):
        raise OSError("characters/ cannot be read")

    monkeypatch.setattr(CharacterManager, "resolve_identity", unreadable)
    monkeypatch.setattr(CharacterManager, "get_character", unreadable)
    context = await VoiceProfileResolver().resolve_context(db_path=isolate_test_database, profile_id=anna["id"])
    assert context is not None
    assert context.profile_id == anna["id"]
    assert context.name == "安娜"
