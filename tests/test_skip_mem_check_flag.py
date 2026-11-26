"""GALGAME2VOICE_SKIP_MEM_CHECK=0 keeps the memory check on, as people expect from 0."""

import pytest

from galgame2voice.services.voice_resources import InsufficientMemoryError, VoiceResourceGuard


def _low_memory_guard():
    return VoiceResourceGuard(memory_status=lambda: (16.0, 0.2), release=lambda: None)


@pytest.mark.parametrize("value", ["0", "false", "No", " off ", ""])
def test_off_values_keep_the_check(monkeypatch, value):
    monkeypatch.setenv("GALGAME2VOICE_SKIP_MEM_CHECK", value)
    with pytest.raises(InsufficientMemoryError):
        _low_memory_guard().check()


@pytest.mark.parametrize("value", ["1", "true", "yes"])
def test_on_values_skip_the_check(monkeypatch, value):
    monkeypatch.setenv("GALGAME2VOICE_SKIP_MEM_CHECK", value)
    _low_memory_guard().check()
