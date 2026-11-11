"""A turn's telemetry must not report a milestone that never happened as a timing."""
from galgame2voice.utils.profiler import ChatTurnProfiler


def test_turn_without_audio_reports_no_first_audio_time():
    profiler = ChatTurnProfiler(turn_id="synthetic", enabled=False)
    profiler.record_llm_first_token()
    profiler.record_first_sentence()

    data = profiler.to_dict()

    assert data["t5_app_ttfa_ms"] is None
    assert data["passed_target"] is None
    for key in ("t3_tts_dispatch_ms", "t4_upstream_first_byte_ms", "t6_frontend_delivered_ms",
                "t7_playback_ms", "tts_infer_done_ms"):
        assert data[key] is None, key
    assert data["t1_ttft_ms"] is not None and data["t1_ttft_ms"] >= 0
    assert data["t2_first_sentence_ms"] >= data["t1_ttft_ms"]


def test_turn_without_any_token_reports_no_first_token_time():
    data = ChatTurnProfiler(turn_id="synthetic", enabled=False).to_dict()

    assert data["t1_ttft_ms"] is None
    assert data["t2_first_sentence_ms"] is None


def test_turn_with_audio_keeps_its_first_audio_time_and_target_verdict():
    profiler = ChatTurnProfiler(turn_id="synthetic", enabled=False)
    profiler.t_first_audio = profiler.t_start + 0.25

    data = profiler.to_dict()

    assert data["t5_app_ttfa_ms"] == 250.0
    assert data["passed_target"] is True


def test_waterfall_without_audio_does_not_claim_a_first_audio_time():
    profiler = ChatTurnProfiler(turn_id="synthetic", enabled=True)
    profiler.record_llm_first_token()

    report = profiler.render_ascii()

    assert "TTFA:" in report
    assert "no audio" in report
    assert "PASS" not in report and "WARN" not in report
