"""Pure unit tests for stream_persist_policy across all four terminal states."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import pytest

from galgame2voice.database.models import MessageCreate
from galgame2voice.services import chat_pipelines
from galgame2voice.services.chat_pipelines.stream_coordinator import StreamCoordinator
from galgame2voice.services.chat_pipelines.stream_persist_policy import (
    build_assistant_message_create,
    build_completion_done_event,
    classify_empty_completion_error,
    decide_completion_policy,
    decide_early_exit_policy,
    decide_teardown_policy,
    decide_turn_message_action,
    plan_master_audio_url,
    resolve_concat_pause_seconds,
)
from galgame2voice.services.chat_pipelines.stream_snapshot import new_line_audio_state
from galgame2voice.utils.error_diagnostics import (
    EMPTY_REPLY_CONTENT_FILTERED,
    EMPTY_REPLY_DEFAULT,
    EMPTY_REPLY_STOPPED_ON_LENGTH,
)


def _sample_lines_and_states() -> tuple[list[dict], dict[int, dict], dict]:
    announced_lines = [
        {"index": 0, "zh": "第一句。", "ja": "一番です。", "emotion": "gentle", "act": {"pose": "normal"}},
        {"index": 1, "zh": "第二句。", "ja": "二番です。", "emotion": "happy", "act": {}},
    ]
    chunk0 = {"index": 0, "line": 0, "sentence": "一番です。", "audio_url": "/audio/chunk_0.wav"}
    line_audio_states = {
        0: {**new_line_audio_state(0), "sealed": True, "pending": 0, "emitted": True, "status": "ready", "chunks": [chunk0]},
        1: {**new_line_audio_state(1), "sealed": True, "pending": 1, "chunks": []},
    }
    return announced_lines, line_audio_states, chunk0


def test_normal_completion_persists_once_and_settles_affection_once():
    """Terminal state 1a: Normal completion saves full message once, settles affection once, teardown is no-op."""
    announced_lines = [
        {"index": 0, "zh": "早安。", "ja": "おはよう。", "emotion": "gentle", "act": {}},
    ]
    chunk0 = {"index": 0, "line": 0, "sentence": "おはよう。", "audio_url": "/audio/0.wav"}
    states = {
        0: {**new_line_audio_state(0), "sealed": True, "pending": 0, "emitted": True, "status": "ready", "chunks": [chunk0]},
    }

    main_policy = decide_completion_policy(
        announced_lines=announced_lines,
        line_audio_states=states,
        final_chinese="早安。",
        final_japanese="おはよう。",
        lines_truncated=False,
        cancel_flagged=False,
        last_finish_reason="stop",
    )
    assert main_policy["abort_empty_reply"] is False
    assert main_policy["db_action"] == "persist_full"
    assert main_policy["concat_audio"] is True
    assert main_policy["record_metrics"] is True
    assert main_policy["settle_affection"] is True
    assert main_policy["empty_error_text"] is None
    assert main_policy["stopped_on_length"] is False
    assert main_policy["final_lines"][0]["audio"] == {"status": "ready", "chunks": [chunk0]}
    assert main_policy["persisted_lines"] == main_policy["final_lines"]

    # Simulate DB commit updating persisted_assistant=True, then entering finally teardown
    teardown_policy = decide_teardown_policy(
        announced_lines=announced_lines,
        line_audio_states=states,
        partial_chinese="早安。",
        partial_japanese="おはよう。",
        persisted_assistant=True,
        has_user_message=True,
        persisted_lines=main_policy["persisted_lines"],
    )
    assert teardown_policy["db_action"] == "none"
    assert teardown_policy["settle_affection"] is False

    saves = sum(1 for p in (main_policy, teardown_policy) if p["db_action"] in ("persist_full", "persist_partial"))
    settlements = sum(1 for p in (main_policy, teardown_policy) if p["settle_affection"])
    assert saves == 1
    assert settlements == 1


@pytest.mark.parametrize(
    ("finish_reason", "lines_truncated", "expected_text", "expected_stopped"),
    [
        ("SAFETY", False, EMPTY_REPLY_CONTENT_FILTERED, False),
        ("content_filter", True, EMPTY_REPLY_CONTENT_FILTERED, False),
        ("length", False, EMPTY_REPLY_STOPPED_ON_LENGTH, True),
        ("MAX_TOKENS", False, EMPTY_REPLY_STOPPED_ON_LENGTH, True),
        ("stop", True, EMPTY_REPLY_STOPPED_ON_LENGTH, True),
        ("stop", False, EMPTY_REPLY_DEFAULT, False),
        (None, False, EMPTY_REPLY_DEFAULT, False),
    ],
)
def test_empty_completion_aborts_prunes_user_and_never_settles_affection(
    finish_reason: str | None,
    lines_truncated: bool,
    expected_text: str,
    expected_stopped: bool,
):
    """Terminal state 1b: Empty uncancelled completion classifies error, prunes user, skips concat & affection."""
    err_text, stopped = classify_empty_completion_error(
        last_finish_reason=finish_reason,
        lines_truncated=lines_truncated,
    )
    assert (err_text, stopped) == (expected_text, expected_stopped)

    main_policy = decide_completion_policy(
        announced_lines=[],
        line_audio_states={},
        final_chinese="   ",
        final_japanese="\n",
        lines_truncated=lines_truncated,
        cancel_flagged=False,
        last_finish_reason=finish_reason,
    )
    assert main_policy["abort_empty_reply"] is True
    assert main_policy["db_action"] == "prune_user"
    assert main_policy["concat_audio"] is False
    assert main_policy["record_metrics"] is False
    assert main_policy["settle_affection"] is False
    assert main_policy["empty_error_text"] == expected_text
    assert main_policy["stopped_on_length"] is expected_stopped

    # Cancelled turn with only whitespace reaching completion path does not emit empty_reply error,
    # still prunes orphaned user message and does not settle affection.
    cancelled_blank = decide_completion_policy(
        announced_lines=[{"index": 0, "zh": "   ", "ja": "", "emotion": "gentle", "act": {}}],
        line_audio_states={0: new_line_audio_state(0)},
        final_chinese="   ",
        final_japanese="",
        lines_truncated=False,
        cancel_flagged=True,
        last_finish_reason="stop",
    )
    assert cancelled_blank["abort_empty_reply"] is False
    assert cancelled_blank["db_action"] == "prune_user"
    assert cancelled_blank["settle_affection"] is False


def test_truncation_and_stream_error_persist_at_most_once_without_affection():
    """Terminal state 2: Truncation persists in early exit; fatal error defers single persist to teardown."""
    announced_lines, states, _ = _sample_lines_and_states()

    # Case A: Stream truncation with partial content
    trunc_main = decide_early_exit_policy(
        announced_lines=announced_lines,
        line_audio_states=states,
        partial_chinese="第一句。第二句。",
        partial_japanese="一番です。二番です。",
        cancel_flagged=False,
        is_truncation=True,
        persisted_assistant=False,
        has_user_message=True,
    )
    assert trunc_main["db_action"] == "persist_partial"
    assert trunc_main["emit_truncated_done"] is True
    assert trunc_main["audio_url"] == ""
    assert trunc_main["settle_affection"] is False

    trunc_teardown = decide_teardown_policy(
        announced_lines=announced_lines,
        line_audio_states=states,
        partial_chinese="第一句。第二句。",
        partial_japanese="一番です。二番です。",
        persisted_assistant=True,
        has_user_message=True,
        persisted_lines=trunc_main["persisted_lines"],
    )
    assert trunc_teardown["db_action"] == "none"
    assert trunc_teardown["settle_affection"] is False

    # Case B: Non-truncation stream error with partial text -> early exit does not persist; teardown persists once
    err_main = decide_early_exit_policy(
        announced_lines=announced_lines,
        line_audio_states=states,
        partial_chinese="第一句。",
        partial_japanese="一番です。",
        cancel_flagged=False,
        is_truncation=False,
        persisted_assistant=False,
        has_user_message=True,
    )
    assert err_main["db_action"] == "none"
    assert err_main["emit_truncated_done"] is False
    assert err_main["settle_affection"] is False
    assert err_main["persisted_lines"] is not None

    err_teardown = decide_teardown_policy(
        announced_lines=announced_lines,
        line_audio_states=states,
        partial_chinese="第一句。",
        partial_japanese="一番です。",
        persisted_assistant=False,
        has_user_message=True,
        persisted_lines=err_main["persisted_lines"],
    )
    assert err_teardown["db_action"] == "persist_partial"
    assert err_teardown["lines"] is err_main["persisted_lines"]
    assert err_teardown["audio_url"] == ""
    assert err_teardown["settle_affection"] is False


def test_user_cancellation_persists_partial_or_prunes_empty_without_affection():
    """Terminal state 3: User cancellation persists partial once, or prunes user in teardown if empty."""
    announced_lines, states, _ = _sample_lines_and_states()

    # Cancelled with partial content
    cancel_main = decide_early_exit_policy(
        announced_lines=announced_lines,
        line_audio_states=states,
        partial_chinese="第一句。",
        partial_japanese="一番です。",
        cancel_flagged=True,
        is_truncation=False,
        persisted_assistant=False,
        has_user_message=True,
    )
    assert cancel_main["db_action"] == "persist_partial"
    assert cancel_main["emit_truncated_done"] is True
    assert cancel_main["settle_affection"] is False

    cancel_teardown = decide_teardown_policy(
        announced_lines=announced_lines,
        line_audio_states=states,
        partial_chinese="第一句。",
        partial_japanese="一番です。",
        persisted_assistant=True,
        has_user_message=True,
        persisted_lines=cancel_main["persisted_lines"],
    )
    assert cancel_teardown["db_action"] == "none"
    assert cancel_teardown["settle_affection"] is False

    # Cancelled before any text or lines were produced
    empty_cancel_main = decide_early_exit_policy(
        announced_lines=[],
        line_audio_states={},
        partial_chinese="",
        partial_japanese="   ",
        cancel_flagged=True,
        is_truncation=False,
        persisted_assistant=False,
        has_user_message=True,
    )
    assert empty_cancel_main["db_action"] == "none"
    assert empty_cancel_main["emit_truncated_done"] is True
    assert empty_cancel_main["settle_affection"] is False

    empty_cancel_teardown = decide_teardown_policy(
        announced_lines=[],
        line_audio_states={},
        partial_chinese="",
        partial_japanese="   ",
        persisted_assistant=False,
        has_user_message=True,
        persisted_lines=empty_cancel_main["persisted_lines"],
    )
    assert empty_cancel_teardown["db_action"] == "prune_user"
    assert empty_cancel_teardown["settle_affection"] is False


def test_client_disconnect_teardown_policy_rules():
    """Terminal state 4: Abrupt disconnect goes directly to decide_teardown_policy."""
    announced_lines, states, _ = _sample_lines_and_states()

    # Meaningful partial text -> persist_partial
    disc_partial = decide_teardown_policy(
        announced_lines=announced_lines,
        line_audio_states=states,
        partial_chinese="断线前的话。",
        partial_japanese="切断前の言葉。",
        persisted_assistant=False,
        has_user_message=True,
    )
    assert disc_partial["db_action"] == "persist_partial"
    assert disc_partial["content_chinese"] == "断线前的话。"
    assert disc_partial["content_japanese"] == "切断前の言葉。"
    assert disc_partial["audio_url"] == ""
    assert disc_partial["settle_affection"] is False
    assert disc_partial["lines"] is not None

    # No meaningful text -> prune_user
    disc_empty = decide_teardown_policy(
        announced_lines=[],
        line_audio_states={},
        partial_chinese="  ",
        partial_japanese="",
        persisted_assistant=False,
        has_user_message=True,
    )
    assert disc_empty["db_action"] == "prune_user"
    assert disc_empty["lines"] is None
    assert disc_empty["settle_affection"] is False

    # No user message -> none
    disc_no_user = decide_teardown_policy(
        announced_lines=announced_lines,
        line_audio_states=states,
        partial_chinese="断线前的话。",
        partial_japanese="切断前の言葉。",
        persisted_assistant=False,
        has_user_message=False,
    )
    assert disc_no_user["db_action"] == "none"
    assert disc_no_user["lines"] is None
    assert disc_no_user["settle_affection"] is False


def test_interrupted_policies_preserve_ready_line_audio_and_keep_master_audio_empty():
    """Interrupted turns retain meaningful text and completed line 0 audio while marking pending line 1 failed."""
    announced_lines, states, chunk0 = _sample_lines_and_states()

    trunc_policy = decide_early_exit_policy(
        announced_lines=announced_lines,
        line_audio_states=states,
        partial_chinese="第一句。第二句。",
        partial_japanese="一番です。二番です。",
        cancel_flagged=False,
        is_truncation=True,
        persisted_assistant=False,
        has_user_message=True,
    )
    cancel_policy = decide_early_exit_policy(
        announced_lines=announced_lines,
        line_audio_states=states,
        partial_chinese="第一句。第二句。",
        partial_japanese="一番です。二番です。",
        cancel_flagged=True,
        is_truncation=False,
        persisted_assistant=False,
        has_user_message=True,
    )
    disconnect_policy = decide_teardown_policy(
        announced_lines=announced_lines,
        line_audio_states=states,
        partial_chinese="第一句。第二句。",
        partial_japanese="一番です。二番です。",
        persisted_assistant=False,
        has_user_message=True,
    )

    for policy in (trunc_policy, cancel_policy, disconnect_policy):
        assert policy["db_action"] == "persist_partial"
        assert policy["content_chinese"] == "第一句。第二句。"
        assert policy["content_japanese"] == "一番です。二番です。"
        assert policy["audio_url"] == ""
        assert policy["settle_affection"] is False
        lines = policy["lines"]
        assert len(lines) == 2
        assert lines[0]["audio"] == {"status": "ready", "chunks": [chunk0]}
        assert lines[1]["audio"] == {"status": "failed", "chunks": []}


def test_master_audio_concat_plan_and_pause_resolution():
    """Verifies 0, 1, and multi-chunk master audio planning and pause option precedence/clamping."""
    assert plan_master_audio_url([]) == {
        "needs_concat": False,
        "audio_url": "",
        "fallback_audio_url": "",
        "chunk_paths": [],
        "pause_seconds": 0.0,
    }

    single = [{"index": 0, "audio_url": "/audio/only.wav", "local_path": "/tmp/only.wav"}]
    assert plan_master_audio_url(single, {"fragment_interval": 1.2}) == {
        "needs_concat": False,
        "audio_url": "/audio/only.wav",
        "fallback_audio_url": "/audio/only.wav",
        "chunk_paths": [],
        "pause_seconds": 0.0,
    }

    multi = [
        {"index": 0, "audio_url": "/audio/c0.wav", "local_path": "/tmp/c0.wav"},
        {"index": 1, "audio_url": "/audio/c1.wav", "local_path": "/tmp/c1.wav"},
    ]
    assert plan_master_audio_url(
        multi,
        {"fragment_interval": 0.6, "pause_duration": 1.5, "sentence_pause": 2.5},
    ) == {
        "needs_concat": True,
        "audio_url": "",
        "fallback_audio_url": "/audio/c0.wav",
        "chunk_paths": ["/tmp/c0.wav", "/tmp/c1.wav"],
        "pause_seconds": 0.6,
    }

    # Precedence, zero preservation, invalid fallback, and [0.0, 5.0] clamping
    assert resolve_concat_pause_seconds(None) == 0.3
    assert resolve_concat_pause_seconds({}) == 0.3
    assert resolve_concat_pause_seconds({"fragment_interval": 0.0, "pause_duration": 1.2}) == 0.0
    assert resolve_concat_pause_seconds({"fragment_interval": None, "pause_duration": "0.8", "sentence_pause": 2.0}) == 0.8
    assert resolve_concat_pause_seconds({"sentence_pause": 1.25}) == 1.25
    assert resolve_concat_pause_seconds({"fragment_interval": "invalid"}) == 0.3
    assert resolve_concat_pause_seconds({"fragment_interval": object()}) == 0.3
    assert resolve_concat_pause_seconds({"fragment_interval": -2.0}) == 0.0
    assert resolve_concat_pause_seconds({"fragment_interval": 9.5}) == 5.0


def test_message_create_turn_action_and_completion_done_event_builders():
    """Verifies MessageCreate DTO, turn action helper, done event payload, and package exports."""
    assert decide_turn_message_action(full_chinese="你好", full_japanese="") == ("persist_full", True)
    assert decide_turn_message_action(full_chinese="  ", full_japanese="こんにちは") == ("persist_full", True)
    assert decide_turn_message_action(full_chinese="  ", full_japanese=None) == ("prune_user", False)

    lines = [
        {
            "index": 0,
            "zh": "你好。",
            "ja": "こんにちは。",
            "emotion": "gentle",
            "act": {},
            "audio": {
                "status": "ready",
                "chunks": [{"index": 0, "line": 0, "audio_url": "/audio/0.wav", "sentence": "こんにちは。"}],
            },
        }
    ]
    msg = build_assistant_message_create(
        session_id="sess_1",
        content_chinese="你好。",
        content_japanese="こんにちは。",
        lines=lines,
        audio_url="/audio/full.wav",
        latency_ms=320,
        reply_to_message_id=7,
    )
    assert isinstance(msg, MessageCreate)
    assert msg.session_id == "sess_1"
    assert msg.role == "assistant"
    assert msg.content_chinese == "你好。"
    assert msg.content_japanese == "こんにちは。"
    assert msg.audio_url == "/audio/full.wav"
    assert msg.latency_ms == 320
    assert msg.reply_to_message_id == 7
    assert msg.lines is not None and msg.lines[0]["zh"] == "你好。"

    done_no_id = build_completion_done_event(
        full_chinese="你好。",
        full_japanese="こんにちは。",
        final_lines=lines,
        reply_format="lines",
        final_emotion="gentle",
        affection={"score": 55, "emotion": "gentle"},
        metrics={"ttft_ms": 120.0},
        total_audio_url="/audio/full.wav",
        chunks=[{"index": 0, "line": 0, "audio_url": "/audio/0.wav", "sentence": "こんにちは。"}],
        latency_ms=320,
        tts_params={"speed": 1.0, "temperature": 0.8, "emotion": "gentle", "adaptive_enabled": True},
        persisted_message_id=None,
        telemetry={"turn_id": "sess_1"},
        cancel_flagged=False,
        has_meaningful_content=True,
        lines_truncated=False,
    )
    assert done_no_id["event"] == "done"
    assert "message_id" not in done_no_id["data"]
    assert done_no_id["data"]["truncated"] is False
    assert done_no_id["data"]["audio_url"] == "/audio/full.wav"
    assert done_no_id["data"]["total_audio_url"] == "/audio/full.wav"

    done_cancelled = build_completion_done_event(
        full_chinese="你好。",
        full_japanese="こんにちは。",
        final_lines=lines,
        reply_format="old",
        final_emotion="gentle",
        affection={"score": 55},
        metrics={},
        total_audio_url="/audio/0.wav",
        chunks=[],
        latency_ms=100,
        tts_params=None,
        persisted_message_id=88,
        telemetry={},
        cancel_flagged=True,
        has_meaningful_content=True,
        lines_truncated=False,
    )
    assert done_cancelled["data"]["message_id"] == 88
    assert done_cancelled["data"]["truncated"] is True

    done_lines_trunc = build_completion_done_event(
        full_chinese="你好。",
        full_japanese="こんにちは。",
        final_lines=lines,
        reply_format="lines",
        final_emotion="gentle",
        affection={"score": 55},
        metrics={},
        total_audio_url="",
        chunks=[],
        latency_ms=100,
        tts_params=None,
        persisted_message_id=89,
        telemetry={},
        cancel_flagged=False,
        has_meaningful_content=True,
        lines_truncated=True,
    )
    assert done_lines_trunc["data"]["truncated"] is True

    assert hasattr(chat_pipelines, "stream_persist_policy")
    for exported_name in (
        "resolve_concat_pause_seconds",
        "plan_master_audio_url",
        "classify_empty_completion_error",
        "build_assistant_message_create",
        "decide_turn_message_action",
        "decide_early_exit_policy",
        "decide_completion_policy",
        "build_completion_done_event",
        "decide_teardown_policy",
    ):
        assert hasattr(chat_pipelines, exported_name)


@pytest.mark.asyncio
async def test_coordinator_concat_audio_chunks_delegates_to_plan(tmp_path: Path):
    """StreamCoordinator._concat_audio_chunks follows plan_master_audio_url and falls back safely."""
    calls: list[tuple[list[str], Path, float]] = []

    def make_coord(concat_fn, tts_options=None):
        coord = StreamCoordinator.__new__(StreamCoordinator)
        coord.tts_options = tts_options
        coord.tts_service = SimpleNamespace(audio_dir=tmp_path)
        coord.concat_wav_fn = concat_fn
        return coord

    coord_ok = make_coord(
        lambda paths, out, pause: (calls.append((paths, out, pause)) or True),
        tts_options={"pause_duration": 0.75},
    )
    assert await coord_ok._concat_audio_chunks([]) == ""
    assert await coord_ok._concat_audio_chunks([{"audio_url": "/audio/single.wav"}]) == "/audio/single.wav"
    assert calls == []

    multi = [
        {"audio_url": "/audio/0.wav", "local_path": str(tmp_path / "0.wav")},
        {"audio_url": "/audio/1.wav", "local_path": str(tmp_path / "1.wav")},
    ]
    master_url = await coord_ok._concat_audio_chunks(multi)
    assert master_url.startswith("/audio/full_") and master_url.endswith(".wav")
    assert len(calls) == 1
    assert calls[0][0] == [str(tmp_path / "0.wav"), str(tmp_path / "1.wav")]
    assert calls[0][2] == 0.75

    coord_false = make_coord(lambda *_: False)
    assert await coord_false._concat_audio_chunks(multi) == "/audio/0.wav"

    def raise_err(*_):
        raise OSError("synthetic disk full")

    coord_exc = make_coord(raise_err)
    assert await coord_exc._concat_audio_chunks(multi) == "/audio/0.wav"

