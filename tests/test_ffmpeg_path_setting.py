"""FFMPEG_PATH / FFMPEG_BIN may name the ffmpeg program or the folder holding it."""

import logging
import sys

import pytest

from galgame2voice.utils import audio_converter
from galgame2voice.utils.audio_converter import find_ffmpeg, reset_ffmpeg_cache

EXE = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"


@pytest.fixture
def no_other_ffmpeg(monkeypatch):
    reset_ffmpeg_cache()
    monkeypatch.setenv("PATH", "")
    monkeypatch.delenv("FFMPEG_PATH", raising=False)
    monkeypatch.delenv("FFMPEG_BIN", raising=False)
    monkeypatch.setattr(audio_converter, "_find_ffmpeg_in_project_dirs", lambda *a: None)
    monkeypatch.setattr(audio_converter, "_find_ffmpeg_in_python_env", lambda *a: None)
    monkeypatch.setitem(sys.modules, "imageio_ffmpeg", None)
    yield
    reset_ffmpeg_cache()


def _make_exe(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    path.chmod(0o755)
    return path


def test_folder_holding_ffmpeg_is_accepted(tmp_path, monkeypatch, no_other_ffmpeg):
    exe = _make_exe(tmp_path / "ffmpeg-7.1" / "bin" / EXE)
    monkeypatch.setenv("FFMPEG_PATH", str(exe.parent))
    assert find_ffmpeg() == str(exe.resolve())


def test_unzipped_top_folder_finds_its_bin(tmp_path, monkeypatch, no_other_ffmpeg):
    exe = _make_exe(tmp_path / "ffmpeg-7.1" / "bin" / EXE)
    monkeypatch.setenv("FFMPEG_PATH", str(tmp_path / "ffmpeg-7.1"))
    assert find_ffmpeg() == str(exe.resolve())


def test_quoted_path_from_copy_as_path_is_accepted(tmp_path, monkeypatch, no_other_ffmpeg):
    exe = _make_exe(tmp_path / "tools" / EXE)
    monkeypatch.setenv("FFMPEG_BIN", f'"{exe}"')
    assert find_ffmpeg() == str(exe.resolve())


def test_unusable_setting_is_logged(tmp_path, monkeypatch, no_other_ffmpeg, caplog):
    monkeypatch.setenv("FFMPEG_PATH", str(tmp_path / "missing"))
    with caplog.at_level(logging.WARNING, logger="galgame2voice.utils.audio_converter"):
        assert find_ffmpeg() is None
    assert "does not point to ffmpeg" in caplog.text
