"""A bad value in the environment or .env stops startup with a plain Chinese message."""

import os
import subprocess
import sys

import pytest

from galgame2voice.config import get_settings


@pytest.fixture
def fresh_settings():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.parametrize(
    ("name", "value", "reason"),
    [
        ("GALGAME2VOICE_PORT", "abc", "整数"),
        ("GALGAME2VOICE_DEBUG", "maybe", "true 或 false"),
    ],
)
def test_bad_setting_names_the_variable(monkeypatch, fresh_settings, name, value, reason):
    monkeypatch.setenv(name, value)
    with pytest.raises(SystemExit) as caught:
        get_settings()
    message = str(caught.value.code)
    assert name in message
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
