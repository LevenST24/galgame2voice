"""
Unit tests for Emotion Reference Audio Mapping and TtsService dynamic emotion injection.
"""

import pytest
from pathlib import Path
from galgame2voice.services.emotion_references import (
    resolve_emotion_reference,
    normalize_emotion,
    NATSUME_EMOTION_REFERENCES,
)
from galgame2voice.services.tts_service import TtsService


def test_normalize_emotion_synonyms():
    assert normalize_emotion("happy") == "happy"
    assert normalize_emotion("cheerful") == "happy"
    assert normalize_emotion("喜悦") == "happy"
    assert normalize_emotion("开心") == "happy"
    assert normalize_emotion("sad") == "sad"
    assert normalize_emotion("难过") == "sad"
    assert normalize_emotion("tsundere") == "tsundere"
    assert normalize_emotion("傲娇") == "tsundere"
    assert normalize_emotion("shy") == "shy"
    assert normalize_emotion("害羞") == "shy"
    assert normalize_emotion("cool") == "cool"
    assert normalize_emotion("高冷") == "cool"
    assert normalize_emotion("gentle") == "gentle"
    assert normalize_emotion("unknown_xyz") == "gentle"
    assert normalize_emotion(None) == "gentle"


def test_resolve_emotion_reference_natsume():
    # Verify resolving for Shiki Natsume
    res_happy = resolve_emotion_reference("四季夏目", "happy")
    assert res_happy is not None
    assert "happy.ogg" in res_happy["ref_audio_path"]
    assert "ありがとう" in res_happy["prompt_text"]
    assert res_happy["prompt_lang"] == "ja"

    res_tsundere = resolve_emotion_reference("四季ナツメ (Shiki Natsume)", "傲娇")
    assert res_tsundere is not None
    assert "tsundere.ogg" in res_tsundere["ref_audio_path"]
    assert "バカ" in res_tsundere["prompt_text"]

    res_sad = resolve_emotion_reference("四季夏目", "sad")
    assert res_sad is not None
    assert "sad.ogg" in res_sad["ref_audio_path"]

    res_shy = resolve_emotion_reference("siki", "shy")
    assert res_shy is not None
    assert "shy.ogg" in res_shy["ref_audio_path"]

    res_cool = resolve_emotion_reference("四季夏目", "cool")
    assert res_cool is not None
    assert "cool.ogg" in res_cool["ref_audio_path"]
    assert "勝手に仲間" in res_cool["prompt_text"]

    # Other non-natsume character should not resolve natsume audios
    res_other = resolve_emotion_reference("Arona", "happy")
    assert res_other is None


@pytest.mark.asyncio
async def test_tts_service_populates_emotion_reference():
    tts = TtsService()
    # When ai_adaptive_voice is True and emotion is 'tsundere'
    opts = {"emotion": "tsundere", "ai_adaptive_voice": True}
    res = await tts._populate_voice_profile_opts(opts)
    assert "tsundere.ogg" in res["ref_audio_path"]
    assert "バカ" in res["prompt_text"]

    # When ai_adaptive_voice is False, it should not override with emotion audio
    opts_disabled = {"emotion": "tsundere", "ai_adaptive_voice": False}
    res_disabled = await tts._populate_voice_profile_opts(opts_disabled)
    assert "tsundere.ogg" not in res_disabled.get("ref_audio_path", "")
