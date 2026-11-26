"""Huge or infinite numbers in voice parameters are clamped instead of crashing."""

import pytest

from galgame2voice.services.gpt_sovits_client import GptSovitsClient
from galgame2voice.services.streaming_parser import StreamingBilingualParser
from galgame2voice.services.tts_options import _TTS_NUMERIC_RANGES, resolve_tts_options, validate_user_tts_options
from galgame2voice.utils import prosody

HUGE_INT = int("1" * 400)  # too big to turn into a float
INF = float("inf")


@pytest.mark.parametrize("value", [HUGE_INT, -HUGE_INT, INF, -INF])
@pytest.mark.parametrize(
    ("clamp", "low", "high"),
    [
        (prosody.clamp_dynamic_speed, prosody.DYNAMIC_SPEED_MIN, prosody.DYNAMIC_SPEED_MAX),
        (prosody.clamp_dynamic_temperature, prosody.DYNAMIC_TEMP_MIN, prosody.DYNAMIC_TEMP_MAX),
        (prosody.clamp_dynamic_top_k, prosody.DYNAMIC_TOP_K_MIN, prosody.DYNAMIC_TOP_K_MAX),
        (prosody.clamp_dynamic_top_p, prosody.DYNAMIC_TOP_P_MIN, prosody.DYNAMIC_TOP_P_MAX),
        (
            prosody.clamp_dynamic_fragment_interval,
            prosody.DYNAMIC_FRAGMENT_INTERVAL_MIN,
            prosody.DYNAMIC_FRAGMENT_INTERVAL_MAX,
        ),
        (prosody.clamp_dynamic_batch_size, prosody.DYNAMIC_BATCH_SIZE_MIN, prosody.DYNAMIC_BATCH_SIZE_MAX),
    ],
)
def test_clamp_stays_in_range(clamp, low, high, value):
    assert low <= clamp(value) <= high


@pytest.mark.parametrize("adaptive", [False, True])
@pytest.mark.parametrize("key", ["speed_factor", "temperature", "top_k", "top_p", "fragment_interval", "batch_size"])
@pytest.mark.parametrize("value", [HUGE_INT, -HUGE_INT, INF, -INF])
def test_resolve_tts_options_stays_in_range(key, value, adaptive):
    resolved = resolve_tts_options({key: value, "ai_adaptive_voice": adaptive})
    low, high, _ = _TTS_NUMERIC_RANGES[key]
    assert low <= resolved[key] <= high


def test_streamed_reply_with_huge_tts_number_keeps_parsing():
    reply = (
        '{"lines":[{"zh":"你好。","ja":"こんにちは。"}],'
        '"tts":{"temperature":' + "1" * 400 + "}}"
    )
    parser = StreamingBilingualParser()
    for i in range(0, len(reply), 9):
        parser.feed_chunk(reply[i : i + 9])
    zh, ja, _ = parser.finalize()
    assert (zh, ja) == ("你好。", "こんにちは。")
    assert prosody.DYNAMIC_TEMP_MIN <= parser.tts_temperature <= prosody.DYNAMIC_TEMP_MAX
    assert len(parser.normalized_lines()) == 1


@pytest.mark.parametrize("bad_temp", [HUGE_INT, INF, 99, "bad"])
def test_validate_user_tts_options_rejects_invalid_temp_alias(bad_temp):
    with pytest.raises(ValueError):
        validate_user_tts_options({"temp": bad_temp})


@pytest.mark.parametrize(
    ("key", "value"),
    [("speed", HUGE_INT), ("temp", HUGE_INT), ("top_k", INF), ("top_p", HUGE_INT), ("fragment_interval", HUGE_INT)],
)
def test_calculate_adaptive_prosody_falls_back_on_overflow(key, value):
    result = prosody.calculate_adaptive_prosody("普通の発言です", emotion="happy", base_params={key: value})
    expected = prosody.EMOTION_PROSODY_MATRIX["happy"]
    assert result["speed"] == expected["speed"]
    assert result["temperature"] == expected["temperature"]
    assert result["top_k"] == expected["top_k"]
    assert result["top_p"] == expected["top_p"]
    assert result["fragment_interval"] == expected["fragment_interval"]


@pytest.mark.parametrize("value", [INF, -INF])
def test_build_tts_payload_clamps_infinite_batch_size(value):
    payload = GptSovitsClient()._build_tts_payload("こんにちは。", {"batch_size": value, "ref_audio_path": "ref.wav"})
    assert prosody.DYNAMIC_BATCH_SIZE_MIN <= payload["batch_size"] <= prosody.DYNAMIC_BATCH_SIZE_MAX
