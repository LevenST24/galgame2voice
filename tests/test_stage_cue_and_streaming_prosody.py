"""
Tests for Chinese & Japanese Stage Direction Stripping, Emotion Inference,
Streaming Adaptive Prosody, WAV Byte-Rate Extraction, and TTS Speed Telemetry.
"""

import struct
import pytest
from httpx import AsyncClient, ASGITransport

from galgame2voice.utils.japanese_phonetics import (
    clean_japanese_parentheses,
    extract_stage_directions_and_emotion,
    is_spoken_dialogue_inside_brackets,
    CHINESE_STAGE_CUE_EXACT_SET,
    STAGE_CUE_EMOTION_MAP,
)
from galgame2voice.services.emotion_classifier import (
    extract_bracketed_emotion,
    classify_emotion,
)
from galgame2voice.services.streaming_parser import StreamingBilingualParser
from galgame2voice.services.gpt_sovits_client import extract_wav_duration
from galgame2voice.services.dynamic_batcher import get_speed_tracker, reset_speed_tracker
from galgame2voice.main import app


# ============================================================================
# 1. Stage Direction Stripping & Emotion Extraction Tests
# ============================================================================

def test_chinese_stage_direction_stripping_and_emotion():
    """Validates that Chinese action cues are stripped from speech and mapped to emotions."""
    cases = [
        ("（脸红）那、那个……我才没有想你呢！", "那、那个……我才没有想你呢！", "shy"),
        ("（叹了口气）真是拿你没办法……", "真是拿你没办法……", "sad"),
        ("（微微一笑）今天过得怎么样？", "今天过得怎么样？", "happy"),
        ("（双手叉腰）大笨蛋！", "大笨蛋！", "tsundere"),
        ("（害羞地低下头）其实……我喜欢你。", "其实……我喜欢你。", "shy"),
        ("（鼓起腮帮子）哼，理你才怪！", "哼，理你才怪！", "tsundere"),
        ("（别过脸去）谁要你管啊！", "谁要你管啊！", "shy"),
        ("（轻笑）真是个可爱的人呢。", "真是个可爱的人呢。", "happy"),
        ("（气鼓鼓地跺脚）讨厌讨厌！", "讨厌讨厌！", "tsundere"),
        ("（深呼吸）呼……终于放松下来了。", "呼……终于放松下来了。", "gentle"),
    ]

    for raw, expected_clean, expected_emo in cases:
        clean, emo = extract_stage_directions_and_emotion(raw)
        assert clean == expected_clean, f"Failed clean for '{raw}': got '{clean}' != '{expected_clean}'"
        assert emo == expected_emo, f"Failed emo for '{raw}': got '{emo}' != '{expected_emo}'"


def test_markdown_and_english_stage_cues():
    """Validates Markdown asterisks and English cues are stripped with emotions extracted."""
    cases = [
        ("*blushes* べ、別に！", "べ、別に！", "shy"),
        ("*sighs* 困ったものですね", "困ったものですね", "sad"),
        ("*giggles* あなたって本当に面白い人ね", "あなたって本当に面白い人ね", "happy"),
        ("(tsundere) バカ！", "バカ！", "tsundere"),
        ("【害羞】あ、あのね……", "あ、あのね……", "shy"),
    ]

    for raw, expected_clean, expected_emo in cases:
        clean, emo = extract_stage_directions_and_emotion(raw)
        assert clean == expected_clean
        assert emo == expected_emo


def test_spoken_dialogue_inside_brackets_is_preserved():
    """Validates that genuine spoken thoughts or dialogue inside brackets are NOT stripped."""
    # Internal thoughts with punctuation
    thought1 = "（本当にこれでいいの？）"
    assert clean_japanese_parentheses(thought1) == "本当にこれでいいの？"

    thought2 = "（……どうしよう、緊張してきた）"
    assert clean_japanese_parentheses(thought2) == "……どうしよう、緊張してきた"

    # Spoken dialogue inside brackets
    dialogue = "あなたが、私の（探している人ですか？）"
    assert clean_japanese_parentheses(dialogue) == "あなたが、私の探している人ですか？"


def test_emotion_classifier_integration():
    """Validates that classify_emotion integrates stage cues and asterisk cues."""
    assert classify_emotion("（脸红）那、那个……我才没有想你呢！") == "shy"
    assert classify_emotion("（双手叉腰）大笨蛋！") == "tsundere"
    assert classify_emotion("*sighs* 真是拿你没办法") == "sad"
    assert classify_emotion("（微微一笑）早安！") == "happy"


# ============================================================================
# 2. Streaming Dialogue Dynamic Prosody Tests
# ============================================================================

def test_streaming_parser_plain_dialogue_adaptive_prosody():
    """
    Validates that plain text dialogue streams without explicit JSON tags
    automatically receive fine-grained dynamic prosody calculation.
    """
    parser = StreamingBilingualParser()
    parser.feed_chunk("「（脸红）那、那个……我才没有想你呢！」")

    opts = parser.get_dynamic_tts_options(
        base_options={},
        adaptive_enabled=True,
        sentence_text="那、那个……我才没有想你呢！",
    )

    assert opts["emotion"] == "shy"
    assert "speed" in opts
    assert "temperature" in opts
    assert "top_k" in opts
    assert "top_p" in opts
    assert "fragment_interval" in opts
    # Shy archetype speed baseline is 0.96; trailing ellipsis adds deceleration (-0.04) -> ~0.92
    assert opts["speed"] <= 0.95
    # Deceleration also reduces top_p and increases fragment_interval
    assert opts["fragment_interval"] >= 0.35


def test_streaming_parser_exclamation_acceleration():
    """Validates that exclamations accelerate speed and raise temperature."""
    parser = StreamingBilingualParser()
    parser.feed_chunk("「（高兴）太棒了！终于做到了！」")

    opts = parser.get_dynamic_tts_options(
        base_options={},
        adaptive_enabled=True,
        sentence_text="太棒了！终于做到了！",
    )

    assert opts["emotion"] == "happy"
    # Happy baseline is 1.05; exclamation raises it by +0.05 -> ~1.10
    assert opts["speed"] >= 1.05
    assert opts["temperature"] >= 0.85


def test_streaming_parser_user_pinned_options_respected():
    """Validates that manual overrides in base_options are strictly preserved."""
    parser = StreamingBilingualParser()
    parser.feed_chunk("「（脸红）那、那个……」")

    custom_options = {
        "speed": 1.45,
        "temperature": 0.55,
        "top_k": 30,
    }

    opts = parser.get_dynamic_tts_options(
        base_options=custom_options,
        adaptive_enabled=True,
        sentence_text="那、那个……",
    )

    assert opts["speed"] == 1.45
    assert opts["temperature"] == 0.55
    assert opts["top_k"] == 30
    assert opts["emotion"] == "shy"


def test_streaming_parser_adaptive_disabled():
    """Validates that disabling adaptive_enabled returns base options unmodified."""
    parser = StreamingBilingualParser()
    parser.feed_chunk("「（脸红）那、那个……」")

    base = {"speed": 1.0}
    opts = parser.get_dynamic_tts_options(
        base_options=base,
        adaptive_enabled=False,
        sentence_text="那、那个……",
    )

    assert opts.get("speed") == 1.0
    assert "top_k" not in opts
    assert opts.get("ai_adaptive_voice") is False


# ============================================================================
# 3. WAV Header Byte-Rate & Duration Extraction Tests
# ============================================================================

def make_mock_wav(sample_rate: int = 32000, channels: int = 1, bits_per_sample: int = 16, num_samples: int = 32000) -> bytes:
    """Creates a minimal valid RIFF/WAVE byte buffer with given sample rate and length."""
    byte_rate = sample_rate * channels * bits_per_sample // 8
    block_align = channels * bits_per_sample // 8
    data_size = num_samples * block_align
    file_size = 36 + data_size

    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        file_size,
        b"WAVE",
        b"fmt ",
        16,  # PCM subchunk size
        1,   # AudioFormat: PCM = 1
        channels,
        sample_rate,
        byte_rate,
        block_align,
        bits_per_sample,
        b"data",
        data_size,
    )
    # Generate non-zero samples (e.g. 500)
    samples = struct.pack(f"<{num_samples}h", *([500] * num_samples))
    return header + samples


def test_extract_wav_duration_across_sample_rates():
    """Validates exact duration calculation from RIFF header byte-rate for 32k, 44.1k, and 48k."""
    # 32000 Hz, 1 sec = 32000 samples
    wav_32k = make_mock_wav(sample_rate=32000, num_samples=32000)
    dur_32k = extract_wav_duration(wav_32k)
    assert dur_32k is not None
    assert pytest.approx(dur_32k, 0.01) == 1.0

    # 44100 Hz, 2.5 sec = 110250 samples
    wav_44k = make_mock_wav(sample_rate=44100, num_samples=110250)
    dur_44k = extract_wav_duration(wav_44k)
    assert dur_44k is not None
    assert pytest.approx(dur_44k, 0.01) == 2.5

    # 48000 Hz, 0.5 sec = 24000 samples
    wav_48k = make_mock_wav(sample_rate=48000, num_samples=24000)
    dur_48k = extract_wav_duration(wav_48k)
    assert dur_48k is not None
    assert pytest.approx(dur_48k, 0.01) == 0.5


def test_extract_wav_duration_invalid_payload_fallback():
    """Validates fallback and safety when payload is corrupt or too short."""
    assert extract_wav_duration(b"") is None
    assert extract_wav_duration(b"too_short") is None

    # Corrupt header falls back to 64000 byte-rate
    fake_payload = b"X" * 64044
    dur = extract_wav_duration(fake_payload)
    assert dur is not None
    assert pytest.approx(dur, 0.05) == 1.0


# ============================================================================
# 4. Telemetry Endpoints Integration Tests
# ============================================================================

@pytest.mark.asyncio
async def test_tts_speed_metrics_endpoint():
    """Validates /api/metrics/tts-speed returns real-time RTF and batch telemetry."""
    reset_speed_tracker()
    tracker = get_speed_tracker()
    tracker.record(char_count=20, elapsed_s=0.5, audio_dur_s=2.0)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/api/metrics/tts-speed")
        assert resp.status_code == 200
        data = resp.json()

        assert "status" in data
        assert "avg_rtf" in data
        assert "chars_per_sec" in data
        assert "dynamic_batch_size" in data
        assert data["sample_count"] >= 1
        assert data["avg_rtf"] == 0.25  # 0.5s / 2.0s = 0.25 RTF


@pytest.mark.asyncio
async def test_metrics_overview_includes_tts_speed():
    """Validates /api/metrics/overview exposes tts_speed telemetry."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/api/metrics/overview")
        assert resp.status_code == 200
        data = resp.json()

        assert "tts_speed" in data
        assert data["tts_speed"] is not None
        assert "dynamic_batch_size" in data["tts_speed"]
