"""
Cross-device deployment tests for reference audio path portability:
1. resolve_existing_audio_path: absolute / project_root-relative / audio_dir-relative resolution.
2. to_project_relative_path: paths under project_root stored portable; external paths untouched.
3. VoiceManager.switch_profile memory guard raises InsufficientMemoryError (and force bypasses).
"""

from types import SimpleNamespace
from pathlib import Path

import pytest

from galgame2voice.utils.path_guard import (
    resolve_existing_audio_path,
    to_project_relative_path,
)
from galgame2voice.services.voice_manager import (
    VoiceManager,
    InsufficientMemoryError,
    _get_switch_min_free_memory_gb,
)


@pytest.fixture
def fake_settings(tmp_path, monkeypatch):
    """Points path_guard's settings at tmp_path so tests never touch real project dirs."""
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir(exist_ok=True)
    settings = SimpleNamespace(project_root=tmp_path, audio_dir=audio_dir)
    monkeypatch.setattr("galgame2voice.utils.path_guard.get_settings", lambda: settings)
    return settings


class TestResolveExistingAudioPath:
    def test_absolute_existing_path_resolves(self, tmp_path, fake_settings):
        f = tmp_path / "refs" / "ref.ogg"
        f.parent.mkdir(exist_ok=True)
        f.write_bytes(b"x")
        assert resolve_existing_audio_path(str(f)) == f

    def test_absolute_missing_path_returns_none(self, tmp_path, fake_settings):
        assert resolve_existing_audio_path(str(tmp_path / "missing.ogg")) is None

    def test_project_root_relative_resolves(self, tmp_path, fake_settings):
        f = tmp_path / "audio" / "refs" / "ref.ogg"
        (tmp_path / "audio" / "refs").mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"x")
        resolved = resolve_existing_audio_path("audio/refs/ref.ogg")
        assert resolved is not None and resolved.is_file()

    def test_audio_dir_relative_resolves(self, tmp_path, fake_settings):
        f = fake_settings.audio_dir / "ref.ogg"
        f.write_bytes(b"x")
        resolved = resolve_existing_audio_path("ref.ogg")
        assert resolved is not None and resolved.is_file()

    def test_missing_everywhere_returns_none(self, fake_settings):
        assert resolve_existing_audio_path("nope/missing.ogg") is None

    def test_empty_and_none_return_none(self, fake_settings):
        assert resolve_existing_audio_path("") is None
        assert resolve_existing_audio_path(None) is None

    def test_surrounding_quotes_stripped(self, tmp_path, fake_settings):
        f = tmp_path / "ref.ogg"
        f.write_bytes(b"x")
        assert resolve_existing_audio_path(f'"{f}"') == f


class TestToProjectRelativePath:
    def test_absolute_under_project_root_becomes_relative(self, tmp_path, fake_settings):
        f = tmp_path / "audio" / "ref.ogg"
        assert to_project_relative_path(str(f)) == "audio/ref.ogg"

    def test_relative_input_normalized_to_posix(self, tmp_path, fake_settings):
        assert to_project_relative_path("audio/ref.ogg") == "audio/ref.ogg"

    def test_external_path_unchanged(self, tmp_path, fake_settings):
        external = "E:/yuzusoft/game/voice.ogg"
        assert to_project_relative_path(external) == external

    def test_empty_unchanged(self, fake_settings):
        assert to_project_relative_path("") == ""


class TestVoiceManagerMemoryGuard:
    @pytest.mark.asyncio
    async def test_low_memory_raises_insufficient_memory_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "galgame2voice.services.voice_manager.get_system_memory_status",
            lambda: (16.0, 0.8),
        )
        monkeypatch.delenv("GALGAME2VOICE_SKIP_MEM_CHECK", raising=False)

        from galgame2voice.services.gpt_sovits_client import get_gpt_sovits_client
        manager = VoiceManager(db_path=str(tmp_path / "t.db"))

        profile = SimpleNamespace(id=1, name="p", ref_audio_path="", prompt_text="",
                                  prompt_lang="ja", text_lang="ja",
                                  gpt_weights_path="", sovits_weights_path="")
        with pytest.raises(InsufficientMemoryError):
            await manager.switch_profile(profile, persist=False)

    @pytest.mark.asyncio
    async def test_force_bypasses_memory_guard(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "galgame2voice.services.voice_manager.get_system_memory_status",
            lambda: (16.0, 0.1),
        )
        monkeypatch.delenv("GALGAME2VOICE_SKIP_MEM_CHECK", raising=False)

        manager = VoiceManager(db_path=str(tmp_path / "t.db"))

        async def fake_switch(profile_obj, force=False):
            return True

        # Guard must not fire before the client call; force short-circuits the check.
        monkeypatch.setattr(manager.client, "switch_voice_profile", fake_switch)
        profile = SimpleNamespace(id=1, name="p", ref_audio_path="", prompt_text="",
                                  prompt_lang="ja", text_lang="ja",
                                  gpt_weights_path="", sovits_weights_path="")
        assert await manager.switch_profile(profile, persist=False, force=True) is True

    def test_threshold_scales_with_total_memory(self, monkeypatch):
        monkeypatch.delenv("GALGAME2VOICE_MIN_FREE_MEM_GB", raising=False)
        monkeypatch.setattr(
            "galgame2voice.services.voice_manager.get_system_memory_status",
            lambda: (4.0, 2.0),
        )
        # 4GB machine: max(0.8, 4*0.06) = 0.8 — small-RAM machines are not locked out
        assert _get_switch_min_free_memory_gb() == 0.8

        monkeypatch.setattr(
            "galgame2voice.services.voice_manager.get_system_memory_status",
            lambda: (32.0, 2.0),
        )
        # 32GB machine: min(1.5, max(0.8, 32*0.06)) = 1.5 — large-RAM machines capped safely
        assert _get_switch_min_free_memory_gb() == pytest.approx(1.5)

    def test_env_override_wins(self, monkeypatch):
        monkeypatch.setenv("GALGAME2VOICE_MIN_FREE_MEM_GB", "0.5")
        assert _get_switch_min_free_memory_gb() == 0.5
