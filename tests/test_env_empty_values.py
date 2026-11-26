"""A setting left empty in the environment or .env falls back to its default."""

import pytest

from galgame2voice.config import Settings


@pytest.mark.parametrize(
    ("name", "field", "default"),
    [
        ("GALGAME2VOICE_PORT", "port", 8080),
        ("GALGAME2VOICE_AUDIO_RETENTION_MINUTES", "audio_retention_minutes", 30),
        ("GALGAME2VOICE_AUDIO_CLEANUP_INTERVAL_SECONDS", "audio_cleanup_interval_seconds", 600),
        ("GALGAME2VOICE_LOG_TO_FILE", "log_to_file", True),
    ],
)
def test_empty_environment_value_uses_default(monkeypatch, name, field, default):
    for alias in ("PORT", "GALGAME_PORT", "AUDIO_RETENTION_MINUTES", "AUDIO_CLEANUP_INTERVAL_SECONDS", "LOG_TO_FILE"):
        monkeypatch.delenv(alias, raising=False)
    monkeypatch.setenv(name, "")
    assert getattr(Settings(_env_file=None), field) == default


def test_empty_line_in_env_file_uses_default(monkeypatch, tmp_path):
    monkeypatch.delenv("GALGAME2VOICE_PORT", raising=False)
    monkeypatch.delenv("PORT", raising=False)
    monkeypatch.delenv("GALGAME_PORT", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("GALGAME2VOICE_PORT=\nGALGAME2VOICE_HOST=127.0.0.1\n", encoding="utf-8")
    assert Settings(_env_file=env_file).port == 8080


def test_filled_value_still_wins(monkeypatch):
    monkeypatch.setenv("GALGAME2VOICE_PORT", "9090")
    assert Settings(_env_file=None).port == 9090
