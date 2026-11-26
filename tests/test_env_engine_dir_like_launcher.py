"""GPT_SOVITS_DIR is read the same way by the server as by the launcher."""

from pathlib import Path

import pytest

from galgame2voice.config import get_settings
from galgame2voice.utils.path_guard import discover_gpt_sovits_roots, get_effective_sovits_dir


@pytest.fixture
def program_folder(tmp_path, monkeypatch):
    root = tmp_path / "program"
    (root / "data").mkdir(parents=True)
    monkeypatch.setenv("GALGAME2VOICE_PROJECT_ROOT", str(root))
    get_settings.cache_clear()
    yield root
    get_settings.cache_clear()


def test_quoted_engine_dir_is_found(program_folder, tmp_path, monkeypatch):
    engine = tmp_path / "Engines" / "My Engine"
    engine.mkdir(parents=True)
    monkeypatch.setenv("GPT_SOVITS_DIR", f' "{engine}" ')

    assert get_effective_sovits_dir(program_folder) == engine.resolve()
    assert engine.resolve() in discover_gpt_sovits_roots(program_folder)


def test_relative_engine_dir_is_under_the_program_folder(program_folder, tmp_path, monkeypatch):
    engine = program_folder / "engine-v2"
    engine.mkdir()
    elsewhere = tmp_path / "somewhere-else"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setenv("GPT_SOVITS_DIR", "engine-v2")

    assert get_effective_sovits_dir(program_folder) == engine.resolve()
    roots = discover_gpt_sovits_roots(program_folder)
    assert engine.resolve() in roots
    assert Path(elsewhere / "engine-v2") not in roots


def test_missing_or_blank_engine_dir_is_ignored(program_folder, monkeypatch):
    monkeypatch.setenv("GPT_SOVITS_DIR", "   ")
    assert get_effective_sovits_dir(program_folder) is None
    monkeypatch.setenv("GPT_SOVITS_DIR", str(program_folder / "not-there"))
    assert get_effective_sovits_dir(program_folder) is None
