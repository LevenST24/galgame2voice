"""Pure unit tests for stream_snapshot calculations across all four terminal states."""

from __future__ import annotations

from types import SimpleNamespace

from galgame2voice.services import chat_pipelines
from galgame2voice.services.chat_pipelines.stream_coordinator import StreamCoordinator
from galgame2voice.services.chat_pipelines.stream_snapshot import (
    build_early_done_event,
    build_final_tts_params,
    build_line_audio_event,
    clean_audio_chunks,
    compute_completion_snapshot,
    compute_disconnect_snapshot,
    compute_early_exit_snapshot,
    has_meaningful_text,
    new_line_audio_state,
    resolve_line_audio_snapshot_status,
    snapshot_lines_with_audio,
)


def test_has_meaningful_text_and_new_line_audio_state():
    assert has_meaningful_text("你好", "") is True
    assert has_meaningful_text("  ", "こんにちは") is True
    assert has_meaningful_text("", "") is False
    assert has_meaningful_text("   ", "\n\t ") is False

    state = new_line_audio_state(3)
    assert state == {
        "line": 3,
        "sealed": False,
        "pending": 0,
        "chunks": [],
        "errors": [],
        "emitted": False,
        "status": None,
    }


def test_build_line_audio_event_statuses():
    s_ready = new_line_audio_state(0)
    s_ready["chunks"] = [{"index": 0, "line": 0, "sentence": "一番です。", "audio_url": "/audio/0.wav"}]
    status, ev = build_line_audio_event(0, s_ready)
    assert status == "ready"
    assert ev == {
        "event": "line_audio",
        "data": {
            "line": 0,
            "status": "ready",
            "chunks": [{"index": 0, "line": 0, "sentence": "一番です。", "audio_url": "/audio/0.wav"}],
        },
    }

    s_skipped = new_line_audio_state(1)
    status_skip, ev_skip = build_line_audio_event(1, s_skipped)
    assert status_skip == "skipped"
    assert ev_skip == {
        "event": "line_audio",
        "data": {"line": 1, "status": "skipped", "chunks": []},
    }

    s_failed = new_line_audio_state(2)
    s_failed["errors"] = ["TTS error", "TTS error", "Timeout"]
    status_fail, ev_fail = build_line_audio_event(2, s_failed)
    assert status_fail == "failed"
    assert ev_fail == {
        "event": "line_audio",
        "data": {
            "line": 2,
            "status": "failed",
            "chunks": [],
            "error": "TTS error; Timeout",
        },
    }


def test_completion_snapshot_mixed_statuses():
    """Terminal state 1: Normal completion (compute_completion_snapshot)."""
    announced_lines = [
        {"index": 0, "zh": "第一句。", "ja": "一番です。", "emotion": "happy", "act": {"pose": "a"}},
        {"index": 1, "zh": "（点头）", "ja": "", "emotion": "gentle", "act": {}},
        {"index": 2, "zh": "第三句。", "ja": "三番です。", "emotion": "gentle", "act": {}},
        {"index": 3, "zh": "第四句。", "ja": "……", "emotion": "calm", "act": {}},
        {"index": 4, "zh": "第五句。", "ja": "五番です。", "emotion": "sad", "act": {}},
    ]
    chunk0 = {"index": 0, "line": 0, "sentence": "一番です。", "audio_url": "/audio/0.wav"}
    chunk2 = {"index": 1, "line": 2, "sentence": "三番です。", "audio_url": "/audio/2.wav"}
    line_audio_states = {
        0: {**new_line_audio_state(0), "status": "ready", "emitted": True, "chunks": [chunk0]},
        1: {**new_line_audio_state(1), "status": "skipped", "emitted": True, "chunks": []},
        2: {**new_line_audio_state(2), "chunks": [chunk2]},
        3: {**new_line_audio_state(3), "chunks": [], "errors": []},
        4: {**new_line_audio_state(4), "chunks": [], "errors": ["synth failed"]},
    }

    snap = compute_completion_snapshot(
        announced_lines=announced_lines,
        line_audio_states=line_audio_states,
        final_chinese="第一句。（点头）第三句。第四句。第五句。",
        final_japanese="一番です。三番です。……五番です。",
        lines_truncated=False,
    )
    assert snap["has_reply"] is True
    assert snap["lines_truncated"] is False
    statuses = [line["audio"]["status"] for line in snap["final_lines"]]
    assert statuses == ["ready", "skipped", "ready", "skipped", "failed"]
    assert snap["final_lines"][0]["audio"]["chunks"] == [chunk0]
    assert snap["final_lines"][2]["audio"]["chunks"] == [chunk2]

    empty_snap = compute_completion_snapshot(
        announced_lines=[],
        line_audio_states={},
        final_chinese="  ",
        final_japanese="",
        lines_truncated=True,
    )
    assert empty_snap["has_reply"] is False
    assert empty_snap["lines_truncated"] is True
    assert empty_snap["final_lines"] == []


def test_early_exit_snapshot_on_truncation_and_early_done_event():
    """Terminal state 2: Stream error / truncation (cancel_flagged=False) + build_early_done_event."""
    announced_lines = [
        {"index": 0, "zh": "已就绪。", "ja": "準備完了。", "emotion": "gentle", "act": {}},
        {"index": 1, "zh": "（沉默）", "ja": "……", "emotion": "gentle", "act": {}},
        {"index": 2, "zh": "合成中。", "ja": "合成中。", "emotion": "gentle", "act": {}},
    ]
    chunk0 = {"index": 0, "line": 0, "sentence": "準備完了。", "audio_url": "/audio/0.wav"}
    line_audio_states = {
        0: {**new_line_audio_state(0), "status": "ready", "emitted": True, "chunks": [chunk0]},
        1: {**new_line_audio_state(1), "status": "skipped", "emitted": True, "chunks": []},
        2: {**new_line_audio_state(2), "sealed": True, "pending": 1, "chunks": []},
    }

    early = compute_early_exit_snapshot(
        announced_lines=announced_lines,
        line_audio_states=line_audio_states,
        partial_chinese="已就绪。（沉默）合成中。",
        partial_japanese="準備完了。……合成中。",
        cancel_flagged=False,
        is_truncation=True,
    )
    assert early["emit_truncated_done"] is True
    assert early["should_persist"] is True
    statuses = [line["audio"]["status"] for line in early["snapshot_lines"]]
    assert statuses == ["ready", "skipped", "failed"]

    fatal_err = compute_early_exit_snapshot(
        announced_lines=announced_lines,
        line_audio_states=line_audio_states,
        partial_chinese="已就绪。",
        partial_japanese="準備完了。",
        cancel_flagged=False,
        is_truncation=False,
    )
    assert fatal_err["emit_truncated_done"] is False
    assert fatal_err["should_persist"] is False
    assert [line["audio"]["status"] for line in fatal_err["snapshot_lines"]] == ["ready", "skipped", "failed"]

    done_ev = build_early_done_event(
        partial_chinese=early["partial_chinese"],
        partial_japanese=early["partial_japanese"],
        snapshot_lines=early["snapshot_lines"],
        reply_format="lines",
        message_id=42,
    )
    assert done_ev == {
        "event": "done",
        "data": {
            "truncated": True,
            "chinese": "已就绪。（沉默）合成中。",
            "japanese": "準備完了。……合成中。",
            "lines": early["snapshot_lines"],
            "reply_format": "lines",
            "message_id": 42,
        },
    }
    assert (
        build_early_done_event(
            partial_chinese="早安。",
            partial_japanese="おはよう。",
            snapshot_lines=[],
        )["data"]["lines"]
        is None
    )


def test_early_exit_snapshot_on_user_cancellation():
    """Terminal state 3: User cancellation (cancel_flagged=True)."""
    announced_lines = [
        {"index": 0, "zh": "", "ja": "日本語のみ。", "emotion": "gentle", "act": {}},
    ]
    line_audio_states = {
        0: new_line_audio_state(0),
    }

    cancelled = compute_early_exit_snapshot(
        announced_lines=announced_lines,
        line_audio_states=line_audio_states,
        partial_chinese="",
        partial_japanese="",
        cancel_flagged=True,
        is_truncation=False,
    )
    assert cancelled["emit_truncated_done"] is True
    assert cancelled["should_persist"] is True
    assert cancelled["snapshot_lines"][0]["audio"]["status"] == "failed"

    empty_cancelled = compute_early_exit_snapshot(
        announced_lines=[],
        line_audio_states={},
        partial_chinese="   ",
        partial_japanese="",
        cancel_flagged=True,
        is_truncation=False,
    )
    assert empty_cancelled["emit_truncated_done"] is True
    assert empty_cancelled["should_persist"] is False
    assert empty_cancelled["snapshot_lines"] == []


def test_disconnect_snapshot_persistence_rules():
    """Terminal state 4: Client disconnect (compute_disconnect_snapshot)."""
    announced_lines = [
        {"index": 0, "zh": "断线前的话。", "ja": "切断前の言葉。", "emotion": "gentle", "act": {}},
    ]
    line_audio_states = {
        0: new_line_audio_state(0),
    }

    disc = compute_disconnect_snapshot(
        announced_lines=announced_lines,
        line_audio_states=line_audio_states,
        partial_chinese="断线前的话。",
        partial_japanese="切断前の言葉。",
        message_persisted=False,
    )
    assert disc["should_persist"] is True
    assert disc["snapshot_lines"] is not None and len(disc["snapshot_lines"]) == 1
    assert disc["snapshot_lines"][0]["audio"]["status"] == "failed"

    precomputed = [{"index": 0, "zh": "已算好", "ja": "計算済み", "audio": {"status": "ready", "chunks": []}}]
    disc_reused = compute_disconnect_snapshot(
        announced_lines=announced_lines,
        line_audio_states=line_audio_states,
        partial_chinese="断线前的话。",
        partial_japanese="切断前の言葉。",
        message_persisted=False,
        persisted_lines=precomputed,
    )
    assert disc_reused["should_persist"] is True
    assert disc_reused["snapshot_lines"] is precomputed

    disc_already_persisted = compute_disconnect_snapshot(
        announced_lines=announced_lines,
        line_audio_states=line_audio_states,
        partial_chinese="断线前的话。",
        partial_japanese="切断前の言葉。",
        message_persisted=True,
    )
    assert disc_already_persisted["should_persist"] is False
    assert disc_already_persisted["snapshot_lines"] is None

    disc_empty = compute_disconnect_snapshot(
        announced_lines=announced_lines,
        line_audio_states=line_audio_states,
        partial_chinese="  ",
        partial_japanese="",
        message_persisted=False,
    )
    assert disc_empty["should_persist"] is False
    assert disc_empty["snapshot_lines"] is None


def test_stream_coordinator_delegation_consistency():
    """Verifies StreamCoordinator helper methods delegate identically to stream_snapshot pure functions."""
    coord = StreamCoordinator.__new__(StreamCoordinator)
    announced_lines = [
        {"index": 0, "zh": "第一句。", "ja": "一番です。", "emotion": "gentle", "act": {}},
        {"index": 1, "zh": "第二句。", "ja": "二番です。", "emotion": "happy", "act": {"pose": "b"}},
    ]
    chunk0 = {"index": 0, "line": 0, "sentence": "一番です。", "audio_url": "/audio/0.wav"}
    states = {
        0: {**new_line_audio_state(0), "status": "ready", "emitted": True, "chunks": [chunk0]},
        1: new_line_audio_state(1),
    }
    run = SimpleNamespace(announced_lines=announced_lines, line_audio_states=states)

    for mark_failed in (False, True):
        expected = snapshot_lines_with_audio(
            announced_lines,
            states,
            mark_unfinished_failed=mark_failed,
        )
        assert coord._snapshot_lines_with_audio(run, mark_unfinished_failed=mark_failed) == expected

    raw_chunks = [
        {"index": 0, "line": 0, "sentence": "一番です。", "audio_url": "/audio/0.wav", "local_path": "/tmp/0.wav"},
    ]
    for inc_line in (False, True):
        assert StreamCoordinator._clean_audio_chunks(raw_chunks, include_line=inc_line) == clean_audio_chunks(
            raw_chunks, include_line=inc_line
        )

    parser = SimpleNamespace(tts_speed=1.1, tts_temperature=0.8, tts_emotion="happy")
    assert StreamCoordinator._build_final_tts_params(parser, True) == build_final_tts_params(parser, True)
    empty_parser = SimpleNamespace(tts_speed=None, tts_temperature=None, tts_emotion=None)
    assert StreamCoordinator._build_final_tts_params(empty_parser, True) is None
    assert resolve_line_audio_snapshot_status(None, mark_unfinished_failed=True) == ("failed", [])
    assert hasattr(chat_pipelines, "stream_snapshot")
