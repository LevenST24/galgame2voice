"""With the access token on, every startup says where to find it (never the token itself)."""

import asyncio
import logging
from types import SimpleNamespace

import pytest

import galgame2voice.main as main
import galgame2voice.security.auth as auth

TOKEN = "s3cret-token-value-1234"


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "is_auth_disabled", lambda: False)
    monkeypatch.delenv(auth.ENV_TOKEN_VAR, raising=False)
    monkeypatch.setattr(auth, "get_env_console_token", lambda: None)

    async def read_db_token(_settings):
        return TOKEN

    monkeypatch.setattr(main, "_read_db_console_token", read_db_token)
    return SimpleNamespace(data_dir=tmp_path / "data")


def _announce(settings, caplog) -> str:
    with caplog.at_level(logging.INFO, logger="galgame2voice"):
        asyncio.run(main._announce_console_token(settings))
    return "\n".join(r.getMessage() for r in caplog.records)


def test_later_startups_name_the_token_file(settings, caplog):
    token_file = settings.data_dir / ".console_token"
    token_file.parent.mkdir(parents=True)
    token_file.write_text(TOKEN, encoding="utf-8")
    text = _announce(settings, caplog)
    assert "访问令牌" in text
    assert str(token_file) in text
    assert TOKEN not in text and TOKEN[-4:] not in text


def test_a_deleted_token_file_is_written_again(settings, caplog):
    text = _announce(settings, caplog)
    token_file = settings.data_dir / ".console_token"
    assert token_file.read_text(encoding="utf-8") == TOKEN
    assert str(token_file) in text
    assert TOKEN not in text


def test_an_env_token_points_to_the_variable_not_the_file(settings, caplog, monkeypatch):
    monkeypatch.setattr(auth, "get_env_console_token", lambda: TOKEN)
    text = _announce(settings, caplog)
    assert "GALGAME2VOICE_CONSOLE_TOKEN" in text
    assert ".console_token" not in text
    assert not (settings.data_dir / ".console_token").exists()


def test_nothing_is_said_when_the_token_is_off(settings, caplog, monkeypatch):
    monkeypatch.setattr(auth, "is_auth_disabled", lambda: True)
    assert "访问令牌" not in _announce(settings, caplog)


@pytest.mark.parametrize("contents", ["", "  \n\t  "])
def test_an_empty_token_file_is_written_again(settings, caplog, contents):
    token_file = settings.data_dir / ".console_token"
    token_file.parent.mkdir(parents=True)
    token_file.write_text(contents, encoding="utf-8")

    text = _announce(settings, caplog)

    assert token_file.read_text(encoding="utf-8") == TOKEN
    assert str(token_file) in text
    assert TOKEN not in text
