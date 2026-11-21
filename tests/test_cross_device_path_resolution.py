"""
Cross-device deployment tests for reference audio path portability:
1. resolve_existing_audio_path: absolute / project_root-relative / audio_dir-relative resolution.
2. to_project_relative_path: paths under project_root stored portable; external paths untouched.
"""

from types import SimpleNamespace
from pathlib import Path

import pytest

from galgame2voice.utils.path_guard import (
    resolve_existing_audio_path,
    to_project_relative_path,
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
