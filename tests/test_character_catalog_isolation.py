"""Regression tests proving test isolation for CharacterManager singleton."""

import json
import wave

from galgame2voice.config import get_settings
from galgame2voice.services.character_manager import CharacterManager, get_character_manager


def _install_synthetic(root, package_id, name, seconds=4):
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
        json.dumps({
            "id": package_id,
            "name": name,
            "gpt_weights": "gpt.ckpt",
            "sovits_weights": "sovits.pth",
            "system_prompt": f"Persona for {name}",
            "emotions": {"gentle": {"audio": "gentle.wav", "text": name, "lang": "ja"}},
        }),
        encoding="utf-8",
    )
    return package


def test_isolation_phase_a(tmp_path, monkeypatch):
    """Phase A: Install synthetic Alice and pollute the singleton."""
    root_a = tmp_path / "env_a"
    monkeypatch.setenv("GALGAME2VOICE_PROJECT_ROOT", str(root_a))
    monkeypatch.setenv("GALGAME2VOICE_CHARACTERS_DIR", str(root_a / "characters"))
    get_settings.cache_clear()
    _install_synthetic(root_a, "alice", "Alice", seconds=4)
    mgr_a = get_character_manager(root_a / "characters")
    mgr_a.discover_characters()
    monkeypatch.setattr(CharacterManager, "_instance", mgr_a)
    assert mgr_a.get_character("alice") is not None


def test_isolation_phase_b(tmp_path, monkeypatch):
    """Phase B: Install synthetic Natsume. Global get_character_manager must match direct CharacterManager."""
    root_b = tmp_path / "env_b"
    monkeypatch.setenv("GALGAME2VOICE_PROJECT_ROOT", str(root_b))
    monkeypatch.setenv("GALGAME2VOICE_CHARACTERS_DIR", str(root_b / "characters"))
    get_settings.cache_clear()
    _install_synthetic(root_b, "natsume", "Natsume", seconds=4)

    direct_mgr = CharacterManager(get_settings().characters_dir)
    direct_mgr.discover_characters()
    assert direct_mgr.get_character("natsume") is not None

    global_mgr = get_character_manager()
    assert global_mgr.characters_dir == get_settings().characters_dir.resolve()
    assert global_mgr.get_character("natsume") is not None
