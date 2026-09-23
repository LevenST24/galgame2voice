"""
Unit and Regression Tests for System-Wide Optimizations (R5).
Verifies:
1. SSE keep-alive formatting as W3C comment frames (: keep-alive\\n\\n).
2. CharacterManager _AUDIO_PROBE_CACHE hits and zero re-probing on unchanged files.
3. System update static asset cleanup via git clean -fd.
4. Frontend CSS decoupling and clean index.html.
"""

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from galgame2voice.routers.chat import sse_event_formatter
from galgame2voice.routers.system import _apply_update_sync
from galgame2voice.services.character_manager import _AUDIO_PROBE_CACHE, get_character_manager
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

    def test_audio_probe_caching_performance(self):
        mgr = get_character_manager()
        mgr.discover_characters()

        # The cache should now be populated with reference audio records
        initial_cache_len = len(_AUDIO_PROBE_CACHE)
        assert initial_cache_len > 0, "Expected _AUDIO_PROBE_CACHE to contain discovered audio entries"

        # Re-discovery should hit cache without re-probing
        with patch("galgame2voice.services.tts_service.TtsService.get_audio_duration") as mock_probe:
            mgr.discover_characters()
            mock_probe.assert_not_called()


class TestSystemUpdateGitClean:
    """Verifies that git clean -fd is executed for static build artifacts."""

    def test_apply_update_executes_git_clean_on_static(self):
        executed_cmds = []

        def fake_run_git(args, cwd, timeout=30.0, env_overrides=None):
            # Supply-chain guard: the update endpoint checks the origin remote
            # against an allowlist before fetching/pulling.
            if args[0] == "remote" and len(args) > 1 and args[1] == "get-url":
                return 0, "https://github.com/LevenST24/galgame2voice.git", ""
            executed_cmds.append(list(args))
            if args == ["rev-parse", "--is-inside-work-tree"]:
                return 0, "true", ""
            if args == ["rev-parse", "--abbrev-ref", "HEAD"]:
                return 0, "main", ""
            if args[:2] == ["status", "--porcelain"]:
                return 0, " M galgame2voice/static/assets/index-test.css", ""
            if args[:2] == ["checkout", "HEAD"]:
                return 0, "", ""
            if args[:2] == ["clean", "-fd"]:
                return 0, "", ""
            if args == ["rev-parse", "HEAD"]:
                return 0, "1234567890abcdef1234567890abcdef12345678", ""
            if args == ["rev-parse", "--short", "HEAD"]:
                return 0, "1234567", ""
            if args == ["rev-parse", "--abbrev-ref", "@{u}"]:
                return 0, "origin/main", ""
            if args[:2] == ["pull", "origin"]:
                return 0, "Already up to date.", ""
            if args[:2] == ["diff", "--name-only"]:
                return 0, "", ""
            return 0, "", ""

        with patch("galgame2voice.routers.system._run_git_cmd", side_effect=fake_run_git):
            res, _ = _apply_update_sync(Path("C:/dummy_project"), force_rebuild_frontend=False)
            assert res.success is True
            # Verify clean -fd was called for galgame2voice/static
            assert any(cmd[:3] == ["clean", "-fd", "--"] and "galgame2voice/static" in cmd for cmd in executed_cmds)


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
