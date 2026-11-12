"""
Unit and Regression Tests for System-Wide Optimizations (R5).
Verifies:
1. SSE keep-alive formatting as W3C comment frames (: keep-alive\\n\\n).
2. CharacterManager _AUDIO_PROBE_CACHE hits and zero re-probing on unchanged files.
3. Frontend CSS decoupling and clean index.html.
"""

import asyncio
import wave
from pathlib import Path
from unittest.mock import patch

import pytest

from galgame2voice.routers.chat import sse_event_formatter
from galgame2voice.services import character_manager, character_package
from galgame2voice.services.character_manager import CharacterManager
from galgame2voice.services.chat_service import SseKeepAlive


class TestSseKeepAliveFormatting:
    """Verifies that sse_event_formatter properly adheres to W3C SSE comment specifications."""

    @pytest.mark.asyncio
    async def test_sse_event_formatter_keep_alive_dict(self):
        async def dummy_gen():
            yield {"event": "text", "data": {"delta_chinese": "你好"}}
            yield SseKeepAlive()
            yield {"comment": ": keep-alive\n\n"}
            yield ": keep-alive\n\n"
            yield {"event": "done", "data": {"truncated": False}}

        formatted = []
        async for chunk in sse_event_formatter(dummy_gen()):
            formatted.append(chunk)

        assert len(formatted) == 5
        assert formatted[0] == 'event: text\ndata: {"delta_chinese": "你好"}\n\n'
        assert formatted[1] == ': keep-alive\n\n'
        assert formatted[2] == ': keep-alive\n\n'
        assert formatted[3] == ': keep-alive\n\n'
        assert 'event: done' in formatted[4]


class TestCharacterAudioProbeCache:
    """Verifies that _AUDIO_PROBE_CACHE memoizes file hash and duration."""

    def test_audio_probe_caching_performance(self, tmp_path, monkeypatch):
        monkeypatch.setattr(character_package, "_AUDIO_PROBE_CACHE", {})
        monkeypatch.setattr(character_manager, "_AUDIO_PROBE_CACHE", character_package._AUDIO_PROBE_CACHE)
        audio = tmp_path / "ref.wav"
        with wave.open(str(audio), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(32000)
            w.writeframes(b"\x01\x00" * 32000 * 4)

        first = CharacterManager._probe_audio_file(audio, audio)
        assert first[0] is not None and first[2] is None
        assert len(character_package._AUDIO_PROBE_CACHE) == 1

        # An unchanged file is served from the cache without re-probing.
        with patch("galgame2voice.services.tts_service.TtsService.get_audio_duration") as mock_probe:
            assert CharacterManager._probe_audio_file(audio, audio) == first
            mock_probe.assert_not_called()

        # A changed file (new size) is probed again.
        with wave.open(str(audio), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(32000)
            w.writeframes(b"\x02\x00" * 32000 * 5)
        with patch("galgame2voice.services.tts_service.TtsService.get_audio_duration", return_value=5.0) as mock_probe:
            second = CharacterManager._probe_audio_file(audio, audio)
            mock_probe.assert_called_once()
        assert second[0] != first[0]
        assert second[1] == 5.0


class TestFrontendCssDecoupling:
    """Verifies that frontend/index.html is clean and frontend/src/styles.css contains provider card rules."""

    def test_index_html_has_no_inline_style_block(self):
        index_html = Path("frontend/index.html").read_text(encoding="utf-8")
        assert "<style>" not in index_html, "frontend/index.html should not have inline <style> tags"
        assert "</style>" not in index_html

    def test_styles_css_contains_provider_card(self):
        styles_css = Path("frontend/src/styles.css").read_text(encoding="utf-8")
        assert ".provider-config-card" in styles_css
        assert ".test-result-box" in styles_css
