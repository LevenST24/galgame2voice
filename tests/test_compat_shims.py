"""
Compatibility-contract tests.

The audit flagged a set of backwards-compatibility aliases and re-export modules
as debt: nothing inside the repository needs them any more, but external callers
(`from galgame2voice.adapters import ProviderTestResult`) may. Deleting them on
the strength of "grep found no internal caller" would be guessing about code we
cannot see.

So they are pinned instead. Each one must still resolve to the *same object* as
its canonical implementation, which gives three things:

* a shim cannot silently rot into a stale copy during a refactor;
* the debt becomes an explicit, reviewable list rather than a grep result;
* removing one later is a deliberate, visible edit to this file.
"""

import pytest

from galgame2voice.adapters import base as adapter_base
from galgame2voice.adapters.llm import anthropic_adapter, openai_adapter
from galgame2voice.adapters.stt import openai_stt
from galgame2voice.utils.audio_spec import _AUDIO_SPEC_CACHE


def test_openai_compatible_shim_module_reexports_the_canonical_adapter():
    """`galgame2voice.adapters.openai_compatible` exists only for import compatibility."""
    import galgame2voice.adapters.llm.openai_adapter as canonical
    import galgame2voice.adapters.openai_compatible as shim

    assert shim.OpenAICompatibleLLMAdapter is canonical.OpenAICompatibleLLMAdapter
    assert shim.__all__ == ["OpenAICompatibleLLMAdapter"]


def test_tts_cache_shim_module_reexports_the_canonical_manager():
    """`galgame2voice.services.tts_cache` is a pure re-export surface."""
    import galgame2voice.services.tts_cache as shim
    import galgame2voice.services.tts_cache_manager as canonical
    from galgame2voice.database import models

    assert shim.TtsCacheManager is canonical.TtsCacheManager
    assert shim.get_tts_cache_manager is canonical.get_tts_cache_manager
    assert shim.reset_tts_cache_manager is canonical.reset_tts_cache_manager
    assert shim.TtsCacheEntry is models.TtsCacheEntry
    assert set(shim.__all__) == {
        "TtsCacheManager",
        "TtsCacheEntry",
        "get_tts_cache_manager",
        "reset_tts_cache_manager",
    }


def test_adapter_response_aliases_point_at_the_canonical_types():
    assert adapter_base.ChatResponse is adapter_base.LLMResponse
    assert adapter_base.ProviderTestResult is adapter_base.TestResult


def test_stt_legacy_class_alias_points_at_the_canonical_adapter():
    assert openai_stt.OpenAISTTAdapter is openai_stt.OpenAICompatibleSTTAdapter


@pytest.mark.parametrize("module", [openai_adapter, anthropic_adapter])
def test_private_retry_aliases_point_at_the_canonical_helpers(module):
    """These two are private, so they are the cheapest debt to retire first."""
    assert module._parse_retry_after is adapter_base.parse_retry_after
    assert module._calculate_backoff_delay is adapter_base.calculate_backoff_delay


def test_tts_duration_cache_aliases_expose_the_shared_cache():
    from galgame2voice.services import tts_service

    assert tts_service._AUDIO_STAT_DURATION_CACHE is _AUDIO_SPEC_CACHE._cache
    assert tts_service._AUDIO_DURATION_CACHE is _AUDIO_SPEC_CACHE._cache


def test_natsume_legacy_mapping_is_a_graceful_mapping_facade():
    """Its *contents* come from the installed character package.

    With no character assets installed the facade is legitimately empty, so
    assert the contract that matters to legacy callers — mapping semantics with a
    "gentle" fallback instead of a KeyError — rather than the data.
    """
    from galgame2voice.services.emotion_references import NATSUME_EMOTION_REFERENCES

    assert isinstance(NATSUME_EMOTION_REFERENCES, dict)
    assert NATSUME_EMOTION_REFERENCES["definitely_not_an_emotion"] == NATSUME_EMOTION_REFERENCES.get("gentle", {})
    assert isinstance(NATSUME_EMOTION_REFERENCES.get("gentle"), (dict, type(None)))


def test_utils_package_still_reexports_the_ffmpeg_predicate():
    """`is_ffmpeg_available` is a public re-export, not a decision point.

    It is kept for external callers; the conversion path resolves the binary
    through `_require_ffmpeg_bin` instead. Asserted here so nobody "fixes" a
    stale patch by deleting the public name.
    """
    from galgame2voice.utils import audio_converter, is_ffmpeg_available

    assert is_ffmpeg_available is audio_converter.is_ffmpeg_available
