"""
Comprehensive Test Suite for Fine-Grained Adaptive Emotion Prosody, Micro-Prosody,
and Synthesis-Speed-Driven Dynamic Batching.

Covers:
1. Prosody clamping functions (speed, temp, top_k, top_p, fragment_interval, batch_size).
2. Archetype prosody matrix & calculate_adaptive_prosody micro-prosody rules:
   - Baseline lookup for all 7 standard emotion archetypes
   - Hesitation/ellipsis modulation (speed decrease, fragment_interval increase)
   - Exclamation modulation (speed increase, temperature & top_k increase)
   - Question inflection modulation (top_k & temperature increase)
   - Utterance length modulation (< 6 chars slow-down, > 45 chars acceleration)
   - Stutter prosody modulation
3. StreamingBilingualParser extraction of top_k, top_p, fragment_interval from LLM JSON.
4. Dynamic Batching & Speed Tracker:
   - SynthesisSpeedTracker metrics (RTF, chars/sec, constrained vs high-throughput)
   - DynamicBatchScheduler slice estimation (cut0, cut2, cut5, etc.)
   - Batch size resolution under streaming, single-slice, constrained, and GPU-fast regimes
   - Explicit user override respect
5. Character package schema support for per-emotion custom voice_params.
6. TTS Service and API synthesis endpoint integration.
"""

import asyncio
import math
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional
import pytest
from httpx import AsyncClient, ASGITransport

from galgame2voice.utils.prosody import (
    DYNAMIC_SPEED_MIN,
    DYNAMIC_SPEED_MAX,
    DYNAMIC_TEMP_MIN,
    DYNAMIC_TEMP_MAX,
    DYNAMIC_TOP_K_MIN,
    DYNAMIC_TOP_K_MAX,
    DYNAMIC_TOP_P_MIN,
    DYNAMIC_TOP_P_MAX,
    DYNAMIC_FRAGMENT_INTERVAL_MIN,
    DYNAMIC_FRAGMENT_INTERVAL_MAX,
    DYNAMIC_BATCH_SIZE_MIN,
    DYNAMIC_BATCH_SIZE_MAX,
    EMOTION_PROSODY_MATRIX,
    clamp_dynamic_speed,
    clamp_dynamic_temperature,
    clamp_dynamic_top_k,
    clamp_dynamic_top_p,
    clamp_dynamic_fragment_interval,
    clamp_dynamic_batch_size,
    calculate_adaptive_prosody,
)
from galgame2voice.services.dynamic_batcher import (
    SynthesisSpeedRecord,
    SynthesisSpeedTracker,
    DynamicBatchScheduler,
    get_speed_tracker,
    get_batch_scheduler,
)
from galgame2voice.services.streaming_parser import StreamingBilingualParser
from galgame2voice.services.character_manager import (
    CharacterManager,
    CharacterManifest,
    EmotionConfig,
    VoiceParamsConfig,
)
from galgame2voice.services.gpt_sovits_client import GptSovitsClient, resolve_tts_options
from galgame2voice.services.tts_service import TtsService
from galgame2voice.routers.voice import SynthesizeRequest
from galgame2voice.main import create_app
from tests.conftest import MockGptSovitsServer


# ============================================================================
# 1. Parameter Clamping Unit Tests
# ============================================================================

class TestProsodyClampingHelpers:
    """Verifies that all dynamic parameters are safely clamped within valid boundaries."""

    def test_clamp_top_k_boundaries(self):
        assert clamp_dynamic_top_k(15) == 15
        assert clamp_dynamic_top_k(1) == DYNAMIC_TOP_K_MIN
        assert clamp_dynamic_top_k(50) == DYNAMIC_TOP_K_MAX
        assert clamp_dynamic_top_k(0) == DYNAMIC_TOP_K_MIN
        assert clamp_dynamic_top_k(-10) == DYNAMIC_TOP_K_MIN
        assert clamp_dynamic_top_k(999) == DYNAMIC_TOP_K_MAX
        assert clamp_dynamic_top_k("20") == 20
        assert clamp_dynamic_top_k(None, fallback=12) == 12
        assert clamp_dynamic_top_k("invalid", fallback=15) == 15

    def test_clamp_top_p_boundaries(self):
        assert clamp_dynamic_top_p(0.85) == 0.85
        assert clamp_dynamic_top_p(0.50) == DYNAMIC_TOP_P_MIN
        assert clamp_dynamic_top_p(1.00) == DYNAMIC_TOP_P_MAX
        assert clamp_dynamic_top_p(0.1) == DYNAMIC_TOP_P_MIN
        assert clamp_dynamic_top_p(1.5) == DYNAMIC_TOP_P_MAX
        assert clamp_dynamic_top_p("0.92") == 0.92
        assert clamp_dynamic_top_p(float("nan"), fallback=0.8) == 0.8
        assert clamp_dynamic_top_p(None, fallback=0.85) == 0.85

    def test_clamp_fragment_interval_boundaries(self):
        assert clamp_dynamic_fragment_interval(0.3) == 0.3
        assert clamp_dynamic_fragment_interval(0.10) == DYNAMIC_FRAGMENT_INTERVAL_MIN
        assert clamp_dynamic_fragment_interval(1.00) == DYNAMIC_FRAGMENT_INTERVAL_MAX
        assert clamp_dynamic_fragment_interval(0.01) == DYNAMIC_FRAGMENT_INTERVAL_MIN
        assert clamp_dynamic_fragment_interval(5.0) == DYNAMIC_FRAGMENT_INTERVAL_MAX
        assert clamp_dynamic_fragment_interval("0.45") == 0.45
        assert clamp_dynamic_fragment_interval(None, fallback=0.3) == 0.3

    def test_clamp_batch_size_boundaries(self):
        assert clamp_dynamic_batch_size(4) == 4
        assert clamp_dynamic_batch_size(1) == DYNAMIC_BATCH_SIZE_MIN
        assert clamp_dynamic_batch_size(16) == DYNAMIC_BATCH_SIZE_MAX
        assert clamp_dynamic_batch_size(0) == DYNAMIC_BATCH_SIZE_MIN
        assert clamp_dynamic_batch_size(64) == DYNAMIC_BATCH_SIZE_MAX
        assert clamp_dynamic_batch_size("8") == 8
        assert clamp_dynamic_batch_size(None, fallback=1) == 1


# ============================================================================
# 2. Emotion Prosody Matrix & Micro-Prosody Dynamics
# ============================================================================

class TestEmotionProsodyMatrixAndMicroDynamics:
    """Verifies baseline lookup and fine-grained punctuation/utterance length modulation."""

    def test_all_seven_emotion_archetypes_exist(self):
        expected_archetypes = ["gentle", "happy", "tsundere", "shy", "sad", "angry", "cool"]
        for emo in expected_archetypes:
            assert emo in EMOTION_PROSODY_MATRIX
            matrix_entry = EMOTION_PROSODY_MATRIX[emo]
            assert "speed" in matrix_entry
            assert "temperature" in matrix_entry
            assert "top_k" in matrix_entry
            assert "top_p" in matrix_entry
            assert "fragment_interval" in matrix_entry

    def test_baseline_calculation_without_punctuation(self):
        """Plain neutral text without punctuation should match the archetype baseline."""
        text = "普通の発言です"
        result = calculate_adaptive_prosody(text=text, emotion="happy")
        base = EMOTION_PROSODY_MATRIX["happy"]
        assert result["speed"] == base["speed"]
        assert result["temperature"] == base["temperature"]
        assert result["top_k"] == base["top_k"]
        assert result["top_p"] == base["top_p"]
        assert result["fragment_interval"] == base["fragment_interval"]

    def test_hesitation_and_ellipsis_prosody_damping(self):
        """Ellipsis and wavy dashes should decelerate speed, slightly damp temperature, and lengthen fragment pauses."""
        neutral_res = calculate_adaptive_prosody("あの本はどこですか", emotion="shy")
        hesitant_res = calculate_adaptive_prosody("あの……本は……どこですか〜……", emotion="shy")

        assert hesitant_res["speed"] < neutral_res["speed"]
        assert hesitant_res["fragment_interval"] > neutral_res["fragment_interval"]

    def test_exclamation_prosody_acceleration(self):
        """Exclamations should accelerate speed, elevate temperature, raise top_k, and shorten pauses."""
        neutral_res = calculate_adaptive_prosody("行きます", emotion="angry")
        exclaimed_res = calculate_adaptive_prosody("行きます！！！絶対に許さない！", emotion="angry")

        assert exclaimed_res["speed"] > neutral_res["speed"]
        assert exclaimed_res["temperature"] >= neutral_res["temperature"]
        assert exclaimed_res["top_k"] >= neutral_res["top_k"]
        assert exclaimed_res["fragment_interval"] <= neutral_res["fragment_interval"]

    def test_question_inflection_prosody(self):
        """Questions should slightly elevate top_k and temperature for natural interrogative cadence."""
        neutral_res = calculate_adaptive_prosody("君がやったの", emotion="gentle")
        question_res = calculate_adaptive_prosody("本当に君がやったの？？", emotion="gentle")

        assert question_res["top_k"] >= neutral_res["top_k"]
        assert question_res["temperature"] >= neutral_res["temperature"]

    def test_short_utterance_prevents_rushing(self):
        """Very short utterances (< 6 chars) decelerate slightly to avoid being swallowed."""
        medium_text = "今日はどこかへ行こうか"
        short_text = "はい。"
        medium_res = calculate_adaptive_prosody(medium_text, emotion="gentle")
        short_res = calculate_adaptive_prosody(short_text, emotion="gentle")

        assert short_res["speed"] < medium_res["speed"]

    def test_long_utterance_prevents_sluggishness(self):
        """Long monologues (> 45 chars) accelerate slightly to avoid dragging."""
        base_text = "普通の長さのセリフです。"
        long_text = "これはとても長くて詳細な説明文であり、リスナーが退屈しないようにテンポよく発話されるべき独白のテキストです。"
        base_res = calculate_adaptive_prosody(base_text, emotion="cool")
        long_res = calculate_adaptive_prosody(long_text, emotion="cool")

        assert long_res["speed"] > base_res["speed"]

    def test_stutter_prosody_flustered(self):
        """Stutter markers (べ、別に / あ、あの) trigger hesitant, varied prosody."""
        neutral_res = calculate_adaptive_prosody("あんたのことが好きじゃない", emotion="tsundere")
        stutter_res = calculate_adaptive_prosody("べ、別に！あんたのことが好きじゃないんだから！", emotion="tsundere")

        # Stutter increases pause interval and slightly elevates top_k
        assert stutter_res["top_k"] >= neutral_res["top_k"]


# ============================================================================
# 3. Streaming Parser Dynamic Top-k, Top-p, Fragment Interval Extraction
# ============================================================================

class TestStreamingParserProsodyExtraction:
    """Verifies that StreamingBilingualParser extracts the full dynamic prosody dictionary from LLM chunks."""

    def test_parser_extracts_all_fine_grained_parameters(self):
        parser = StreamingBilingualParser()
        raw = (
            '{"tts": {"speed": 1.12, "temp": 0.88, "top_k": 22, "top_p": 0.92, '
            '"fragment_interval": 0.45, "emotion": "happy"}, '
            '"chinese": "今天天气真好！", "japanese": "今日は本当にいい天気ですね！"}'
        )
        parser.feed_chunk(raw)
        zh, ja, rem = parser.finalize()

        assert zh == "今天天气真好！"
        assert ja == "今日は本当にいい天気ですね！"
        assert parser.tts_speed == 1.12
        assert parser.tts_temperature == 0.88
        assert parser.tts_top_k == 22
        assert parser.tts_top_p == 0.92
        assert parser.tts_fragment_interval == 0.45
        assert parser.tts_emotion == "happy"

        dynamic_opts = parser.get_dynamic_tts_options()
        assert dynamic_opts["speed"] == 1.12
        assert dynamic_opts["temperature"] == 0.88
        assert dynamic_opts["top_k"] == 22
        assert dynamic_opts["top_p"] == 0.92
        assert dynamic_opts["fragment_interval"] == 0.45
        assert dynamic_opts["emotion"] == "happy"

    def test_parser_clamps_out_of_bounds_parameters(self):
        parser = StreamingBilingualParser()
        raw = (
            '{"tts": {"speed": 5.0, "temp": -1.0, "top_k": 999, "top_p": 0.05, '
            '"fragment_interval": 10.0, "emotion": "angry"}, '
            '"chinese": "气死我了！", "japanese": "頭にきたわ！"}'
        )
        parser.feed_chunk(raw)
        parser.finalize()

        assert parser.tts_speed == DYNAMIC_SPEED_MAX
        assert parser.tts_temperature == DYNAMIC_TEMP_MIN
        assert parser.tts_top_k == DYNAMIC_TOP_K_MAX
        assert parser.tts_top_p == DYNAMIC_TOP_P_MIN
        assert parser.tts_fragment_interval == DYNAMIC_FRAGMENT_INTERVAL_MAX


# ============================================================================
# 4. Dynamic Batch Size Scheduler & Speed Tracker Tests
# ============================================================================

class TestDynamicBatchSchedulerAndSpeedTracker:
    """Verifies that synthesis speed is tracked and optimal batch_size is dynamically computed."""

    def test_speed_tracker_metrics_calculation(self):
        tracker = SynthesisSpeedTracker(max_history=10)
        # Record 3 samples: 100 chars synthesized in 2.0s resulting in 5.0s audio
        tracker.record(char_count=100, elapsed_s=2.0, audio_dur_s=5.0)
        tracker.record(char_count=50, elapsed_s=1.0, audio_dur_s=2.5)

        metrics = tracker.get_metrics()
        assert metrics["sample_count"] == 2
        # RTF = 2.0 / 5.0 = 0.4
        assert metrics["avg_rtf"] == 0.4
        # chars/s = (100 + 50) / (2.0 + 1.0) = 50.0
        assert metrics["avg_chars_per_sec"] == 50.0
        assert metrics["is_high_throughput"] is True
        assert metrics["is_resource_constrained"] is False

    def test_speed_tracker_resource_constrained_detection(self):
        tracker = SynthesisSpeedTracker(max_history=10)
        # Slow synthesis: 10 chars took 3.0s, resulting in 2.0s audio (RTF = 1.5, cps = 3.3)
        tracker.record(char_count=10, elapsed_s=3.0, audio_dur_s=2.0)
        metrics = tracker.get_metrics()
        assert metrics["is_resource_constrained"] is True
        assert metrics["is_high_throughput"] is False

    def test_scheduler_slice_count_estimation(self):
        text = "第一句。第二句！第三句？第四句、第五句"
        # cut0: always 1
        assert DynamicBatchScheduler.estimate_slice_count(text, "cut0") == 1
        # cut2 (split on period/comma): 5 slices
        assert DynamicBatchScheduler.estimate_slice_count(text, "cut2") >= 4
        # cut3 (split on period): 3 slices
        assert DynamicBatchScheduler.estimate_slice_count(text, "cut3") >= 3

    def test_scheduler_streaming_always_returns_batch_size_one(self):
        scheduler = DynamicBatchScheduler()
        # Even with multi-fragment text, streaming requires batch_size 1 for minimum TTFA
        long_multi_sentence = "文1。文2。文3。文4。文5。文6。"
        batch_size = scheduler.compute_batch_size(
            text=long_multi_sentence,
            is_streaming=True,
            split_method="cut2",
        )
        assert batch_size == 1

    def test_scheduler_single_slice_returns_batch_size_one(self):
        scheduler = DynamicBatchScheduler()
        batch_size = scheduler.compute_batch_size(
            text="短いテキストです。",
            is_streaming=False,
            split_method="cut0",
        )
        assert batch_size == 1

    def test_scheduler_user_override_respected(self):
        scheduler = DynamicBatchScheduler()
        batch_size = scheduler.compute_batch_size(
            text="長い文。たくさんの文。分割される文。",
            is_streaming=False,
            split_method="cut2",
            user_batch_size=6,
        )
        assert batch_size == 6

    def test_scheduler_resource_constrained_clamps_batch_size(self):
        tracker = SynthesisSpeedTracker()
        tracker.record(char_count=10, elapsed_s=3.0, audio_dur_s=2.0)
        tracker.record(char_count=10, elapsed_s=3.0, audio_dur_s=2.0)
        scheduler = DynamicBatchScheduler(tracker=tracker)

        multi_frag_text = "句1。句2。句3。句4。句5。句6。句7。句8。"
        batch_size = scheduler.compute_batch_size(
            text=multi_frag_text,
            is_streaming=False,
            split_method="cut2",
        )
        # On constrained hardware, batch_size is clamped to at most 2
        assert batch_size <= 2

    def test_scheduler_high_throughput_scales_batch_size(self):
        tracker = SynthesisSpeedTracker()
        tracker.record(char_count=200, elapsed_s=1.0, audio_dur_s=5.0)
        tracker.record(char_count=200, elapsed_s=1.0, audio_dur_s=5.0)
        scheduler = DynamicBatchScheduler(tracker=tracker)

        multi_frag_text = "句1。句2。句3。句4。句5。句6。句7。句8。句9。句10。"
        batch_size = scheduler.compute_batch_size(
            text=multi_frag_text,
            is_streaming=False,
            split_method="cut2",
        )
        # On high throughput GPU, multi-fragment text scales up to 4 or 8
        assert batch_size >= 4


# ============================================================================
# 5. Character Package Schema Custom Voice Params Tests
# ============================================================================

class TestCharacterPackageCustomVoiceParams:
    """Verifies that CharacterPackage supports custom per-emotion voice_params."""

    def test_emotion_config_with_custom_voice_params(self, tmp_path):
        manifest_data = {
            "id": "custom_heroine",
            "name": "CustomTestHeroine",
            "version": "1.0.0",
            "weights": {
                "gpt": "weights/heroine.ckpt",
                "sovits": "weights/heroine.pth",
            },
            "emotions": {
                "happy": {
                    "audio": "audio/happy.wav",
                    "text": "嬉しいです！",
                    "lang": "ja",
                    "voice_params": {
                        "speed": 1.18,
                        "temperature": 0.82,
                        "top_k": 25,
                        "top_p": 0.90,
                        "fragment_interval": 0.25,
                    },
                }
            },
        }
        manifest = CharacterManifest(**manifest_data)
        assert manifest.emotions["happy"].voice_params is not None
        assert manifest.emotions["happy"].voice_params.speed == 1.18
        assert manifest.emotions["happy"].voice_params.top_k == 25

    def test_resolve_emotion_audio_path_returns_voice_params(self, tmp_path):
        pkg_dir = tmp_path / "test_char"
        pkg_dir.mkdir()
        audio_dir = pkg_dir / "audio"
        audio_dir.mkdir()
        sample_wav = audio_dir / "happy.wav"
        # 16000 Hz * 2 bytes/sample * 4 seconds = 128000 bytes
        pcm_bytes = b"\x00\x7f" * 64000
        total_size = 36 + len(pcm_bytes)
        import struct
        header = (
            b"RIFF"
            + struct.pack("<I", total_size)
            + b"WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00\x80>\x00\x00\x00}\x00\x00\x02\x00\x10\x00data"
            + struct.pack("<I", len(pcm_bytes))
        )
        sample_wav.write_bytes(header + pcm_bytes)

        manifest_content = """{
            "id": "test_char",
            "name": "TestChar",
            "version": "1.0.0",
            "weights": {"gpt": "gpt.ckpt", "sovits": "sovits.pth"},
            "emotions": {
                "happy": {
                    "audio": "audio/happy.wav",
                    "text": "こんにちは！",
                    "lang": "ja",
                    "voice_params": {
                        "speed": 1.20,
                        "temperature": 0.95,
                        "top_k": 18
                    }
                }
            }
        }"""
        (pkg_dir / "manifest.json").write_text(manifest_content, encoding="utf-8")

        mgr = CharacterManager(characters_dir=tmp_path)
        res = mgr.resolve_emotion_audio_path("TestChar", "happy")
        assert res is not None
        assert Path(res["ref_audio_path"]).is_file()
        assert res["prompt_text"] == "こんにちは！"
        assert res.get("voice_params") is not None
        assert res["voice_params"]["speed"] == 1.20
        assert res["voice_params"]["temperature"] == 0.95
        assert res["voice_params"]["top_k"] == 18


# ============================================================================
# 6. End-to-End Voice Synthesize API Prosody Forwarding
# ============================================================================

class TestVoiceSynthesizeAPIProsodyForwarding:
    """Verifies that the FastAPI synthesize request DTO and endpoint accept fragment_interval, batch_size, emotion."""

    def test_synthesize_request_schema(self):
        req = SynthesizeRequest(
            text="テストです。",
            speed=1.05,
            top_k=20,
            temperature=0.85,
            top_p=0.92,
            fragment_interval=0.35,
            batch_size=2,
            emotion="happy",
            ai_adaptive_voice=True,
        )
        assert req.fragment_interval == 0.35
        assert req.batch_size == 2
        assert req.emotion == "happy"
        assert req.ai_adaptive_voice is True

    @pytest.mark.asyncio
    async def test_resolve_tts_options_full_prosody_enrichment(self):
        opts = {
            "ai_adaptive_voice": True,
            "emotion": "tsundere",
        }
        resolved = resolve_tts_options(opts)
        # Should have archetype defaults applied and clamped
        assert resolved["top_k"] in range(DYNAMIC_TOP_K_MIN, DYNAMIC_TOP_K_MAX + 1)
        assert DYNAMIC_TOP_P_MIN <= resolved["top_p"] <= DYNAMIC_TOP_P_MAX
        assert DYNAMIC_FRAGMENT_INTERVAL_MIN <= resolved["fragment_interval"] <= DYNAMIC_FRAGMENT_INTERVAL_MAX
