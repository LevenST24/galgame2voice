"""Listening beyond this computer with the access token off is refused with Chinese guidance."""

import pytest

from galgame2voice.config import get_settings
from galgame2voice.main import _init_logging_and_safety


def test_lan_bind_without_token_explains_the_fix_in_chinese(monkeypatch, tmp_path):
    monkeypatch.setenv("GALGAME2VOICE_HOST", "0.0.0.0")
    monkeypatch.setenv("GALGAME2VOICE_AUTH_DISABLED", "1")
    monkeypatch.setenv("GALGAME2VOICE_LOG_TO_FILE", "false")
    get_settings.cache_clear()
    try:
        with pytest.raises(RuntimeError) as exc:
            _init_logging_and_safety(get_settings())
    finally:
        get_settings.cache_clear()
    message = str(exc.value)
    assert message.startswith("无法监听 0.0.0.0")
    assert ".env" in message and "GALGAME2VOICE_AUTH_DISABLED=0" in message
    assert "data/.console_token" in message
