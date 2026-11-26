"""A bad value in the environment or .env stops startup with a plain Chinese message."""

import os
import subprocess
import sys

import pytest

from galgame2voice.config import InvalidSettingsError, get_settings


@pytest.fixture
def fresh_settings():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.parametrize(
    ("name", "value", "expected_name", "reason"),
    [
        ("GALGAME2VOICE_PORT", "abc", "GALGAME2VOICE_PORT", "整数"),
        ("GALGAME2VOICE_DEBUG", "maybe", "GALGAME2VOICE_DEBUG", "true 或 false"),
        ("DEBUG", "maybe", "GALGAME2VOICE_DEBUG / DEBUG", "true 或 false"),
        ("GALGAME2VOICE_CORS_ORIGINS", "[http://localhost:3000", "CORS_ORIGINS", "无法解析列表"),
    ],
)
def test_bad_setting_names_the_variable(monkeypatch, fresh_settings, name, value, expected_name, reason):
    monkeypatch.delenv(f"GALGAME2VOICE_{name}", raising=False)
    monkeypatch.setenv(name, value)
    with pytest.raises(InvalidSettingsError) as caught:
        get_settings()
    message = str(caught.value)
    assert expected_name in message
    assert value in message
    assert reason in message
    assert ".env" in message


def test_startup_shows_message_without_traceback(tmp_path):
    env = {**os.environ, "GALGAME2VOICE_PORT": "abc", "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run(
        [sys.executable, "-c", "import galgame2voice.main"],
        cwd=tmp_path, env=env, capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    assert proc.returncode == 1
    assert "GALGAME2VOICE_PORT" in proc.stderr
    assert "Traceback" not in proc.stderr


def test_invalid_settings_keeps_exception_fallback(monkeypatch, fresh_settings):
    from galgame2voice.database.session import DEFAULT_DB_PATH, get_database_path

    monkeypatch.delenv("GALGAME2VOICE_DB_PATH", raising=False)
    monkeypatch.delenv("GALGAME_DB_PATH", raising=False)
    monkeypatch.setenv("GALGAME2VOICE_DEBUG", "maybe")
    assert issubclass(InvalidSettingsError, Exception)
    assert get_database_path() == DEFAULT_DB_PATH


def test_desktop_entry_prints_bad_setting_message(tmp_path, monkeypatch, fresh_settings, capsys):
    from scripts import desktop_entry, run_server

    for key in ("GALGAME2VOICE_PROJECT_ROOT", "GALGAME2VOICE_HOST", "HOST", "GALGAME_HOST", "GALGAME2VOICE_PORT", "PORT", "GALGAME_PORT"):
        if key not in os.environ:
            monkeypatch.setenv(key, "")
        monkeypatch.delenv(key)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GALGAME2VOICE_DEBUG", "maybe")
    monkeypatch.setattr(desktop_entry, "get_install_root", lambda: tmp_path)
    monkeypatch.setattr(desktop_entry.sys, "argv", ["Galgame2Voice.exe", "--no-engine", "--no-browser"])
    monkeypatch.setattr(run_server, "check_python_environment", lambda: True)
    assert desktop_entry.main() == 1
    err = capsys.readouterr().err
    assert "GALGAME2VOICE_DEBUG" in err and "Traceback" not in err
