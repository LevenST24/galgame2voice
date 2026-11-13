"""
Pure terminal persistence and affection settlement policy for Galgame2Voice streaming chat.
Decides database persistence action, message/audio payloads, master WAV concatenation plans,
empty-reply error classification, and affection settlement across all four terminal states.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from galgame2voice.database.models import MessageCreate
from galgame2voice.utils.error_diagnostics import (
    CONTENT_FILTER_FINISH_REASONS,
    EMPTY_REPLY_CONTENT_FILTERED,
    EMPTY_REPLY_DEFAULT,
    EMPTY_REPLY_STOPPED_ON_LENGTH,
)

from .stream_snapshot import (
    compute_completion_snapshot,
    compute_disconnect_snapshot,
    compute_early_exit_snapshot,
    has_meaningful_text,
)


def resolve_concat_pause_seconds(tts_options: Mapping[str, Any] | None) -> float:
    """Resolves inter-chunk WAV silence duration in [0.0, 5.0]s from TTS options."""
    pause_candidate = None
    if tts_options:
        for key in ("fragment_interval", "pause_duration", "sentence_pause"):
            pause_candidate = tts_options.get(key)
            if pause_candidate is not None:
                break
    if pause_candidate is None:
        return 0.3
    try:
        pause_sec = float(pause_candidate)
    except (ValueError, TypeError):
        pause_sec = 0.3
    return max(0.0, min(5.0, pause_sec))


def plan_master_audio_url(
    audio_chunks: Sequence[Mapping[str, Any]],
    tts_options: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Plans whether master WAV concatenation is needed or a single/empty URL suffices."""
    if not audio_chunks:
        return {
            "needs_concat": False, "audio_url": "", "fallback_audio_url": "",
            "chunk_paths": [], "pause_seconds": 0.0,
        }
    first_url = audio_chunks[0]["audio_url"]
    if len(audio_chunks) == 1:
        return {
            "needs_concat": False, "audio_url": first_url, "fallback_audio_url": first_url,
            "chunk_paths": [], "pause_seconds": 0.0,
        }
    return {
        "needs_concat": True,
        "audio_url": "",
        "fallback_audio_url": first_url,
        "chunk_paths": [c.get("local_path", "") for c in audio_chunks],
        "pause_seconds": resolve_concat_pause_seconds(tts_options),
    }


def classify_empty_completion_error(
    *, last_finish_reason: str | None, lines_truncated: bool,
) -> tuple[str, bool]:
    """Classifies an empty LLM completion into diagnostic Chinese text and stopped_on_length flag."""
    if last_finish_reason in CONTENT_FILTER_FINISH_REASONS:
        return EMPTY_REPLY_CONTENT_FILTERED, False
    stopped_on_length = bool(lines_truncated or last_finish_reason in ("length", "MAX_TOKENS", "max_tokens"))
    return (EMPTY_REPLY_STOPPED_ON_LENGTH if stopped_on_length else EMPTY_REPLY_DEFAULT, stopped_on_length)


def build_assistant_message_create(
    *,
    session_id: str,
    content_chinese: str | None,
    content_japanese: str | None,
    lines: list[dict[str, Any]] | None,
    audio_url: str,
    latency_ms: int,
    reply_to_message_id: int | None,
) -> MessageCreate:
    """Constructs the assistant MessageCreate DTO for partial or full turn persistence."""
    return MessageCreate(
        session_id=session_id, role="assistant",
        content_chinese=content_chinese or "", content_japanese=content_japanese or "",
        lines=lines or None, audio_url=audio_url, latency_ms=latency_ms,
        reply_to_message_id=reply_to_message_id,
    )


def decide_turn_message_action(*, full_chinese: str | None, full_japanese: str | None) -> tuple[str, bool]:
    """Decides whether normal turn persistence writes an assistant row or prunes the user row."""
    has_meaningful_content = has_meaningful_text(full_chinese, full_japanese)
    return ("persist_full" if has_meaningful_content else "prune_user", has_meaningful_content)


def decide_early_exit_policy(
    *,
    announced_lines: Sequence[Mapping[str, Any]],
    line_audio_states: Mapping[int, Mapping[str, Any]],
    partial_chinese: str | None,
    partial_japanese: str | None,
    cancel_flagged: bool,
    is_truncation: bool,
    persisted_assistant: bool,
    has_user_message: bool,
) -> dict[str, Any]:
    """Decides persistence and truncated-done emission policy on stream truncation, error, or cancel."""
    early = compute_early_exit_snapshot(
        announced_lines=announced_lines, line_audio_states=line_audio_states,
        partial_chinese=partial_chinese, partial_japanese=partial_japanese,
        cancel_flagged=cancel_flagged, is_truncation=is_truncation,
    )
    snapshot_lines = early["snapshot_lines"]
    emit_truncated_done = early["emit_truncated_done"]
    should_persist_now = bool(
        emit_truncated_done and not persisted_assistant and has_user_message and early["should_persist"]
    )
    return {
        "db_action": "persist_partial" if should_persist_now else "none",
        "content_chinese": early["partial_chinese"],
        "content_japanese": early["partial_japanese"],
        "audio_url": "",
        "lines": snapshot_lines,
        "persisted_lines": snapshot_lines or None,
        "emit_truncated_done": emit_truncated_done,
        "settle_affection": False,
    }


def decide_completion_policy(
    *,
    announced_lines: Sequence[Mapping[str, Any]],
    line_audio_states: Mapping[int, Mapping[str, Any]],
    final_chinese: str | None,
    final_japanese: str | None,
    lines_truncated: bool,
    cancel_flagged: bool,
    last_finish_reason: str | None = None,
) -> dict[str, Any]:
    """Decides empty-reply abort, DB action, WAV concat, metrics, and affection settlement on completion."""
    completion = compute_completion_snapshot(
        announced_lines=announced_lines, line_audio_states=line_audio_states,
        final_chinese=final_chinese, final_japanese=final_japanese, lines_truncated=lines_truncated,
    )
    final_lines = completion["final_lines"]
    if not completion["has_reply"] and not cancel_flagged:
        error_text, stopped_on_length = classify_empty_completion_error(
            last_finish_reason=last_finish_reason, lines_truncated=lines_truncated,
        )
        return {
            "abort_empty_reply": True, "db_action": "prune_user",
            "content_chinese": final_chinese or "", "content_japanese": final_japanese or "",
            "final_lines": final_lines, "persisted_lines": final_lines or None,
            "concat_audio": False, "record_metrics": False, "settle_affection": False,
            "empty_error_text": error_text, "stopped_on_length": stopped_on_length,
        }

    db_action, has_meaningful_content = decide_turn_message_action(
        full_chinese=final_chinese, full_japanese=final_japanese,
    )
    return {
        "abort_empty_reply": False, "db_action": db_action,
        "content_chinese": final_chinese or "", "content_japanese": final_japanese or "",
        "final_lines": final_lines, "persisted_lines": final_lines or None,
        "concat_audio": True, "record_metrics": True, "settle_affection": has_meaningful_content,
        "empty_error_text": None, "stopped_on_length": False,
    }


def build_completion_done_event(
    *,
    full_chinese: str,
    full_japanese: str,
    final_lines: list[dict[str, Any]] | None,
    reply_format: str = "lines",
    final_emotion: str,
    affection: Mapping[str, Any],
    metrics: Any,
    total_audio_url: str,
    chunks: list[dict[str, Any]],
    latency_ms: int,
    tts_params: dict[str, Any] | None,
    persisted_message_id: int | None,
    telemetry: dict[str, Any],
    cancel_flagged: bool,
    has_meaningful_content: bool,
    lines_truncated: bool,
) -> dict[str, Any]:
    """Constructs the final normal-completion done SSE event payload."""
    return {
        "event": "done",
        "data": {
            "truncated": bool(cancel_flagged or not has_meaningful_content or lines_truncated),
            "chinese": full_chinese,
            "japanese": full_japanese,
            "lines": final_lines or None,
            "reply_format": reply_format,
            "emotion": final_emotion,
            "affection": affection,
            "metrics": metrics,
            "audio_url": total_audio_url,
            "total_audio_url": total_audio_url,
            "chunks": chunks,
            "latency_ms": latency_ms,
            "tts_params": tts_params,
            **({"message_id": persisted_message_id} if persisted_message_id is not None else {}),
            "telemetry": telemetry,
        },
    }


def decide_teardown_policy(
    *,
    announced_lines: Sequence[Mapping[str, Any]],
    line_audio_states: Mapping[int, Mapping[str, Any]],
    partial_chinese: str | None,
    partial_japanese: str | None,
    persisted_assistant: bool,
    has_user_message: bool,
    persisted_lines: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Decides fallback DB persistence or user-message pruning during stream finally teardown."""
    if persisted_assistant or not has_user_message:
        return {
            "db_action": "none", "content_chinese": partial_chinese, "content_japanese": partial_japanese,
            "audio_url": "", "lines": None, "settle_affection": False,
        }

    disconnect_snap = compute_disconnect_snapshot(
        announced_lines=announced_lines, line_audio_states=line_audio_states,
        partial_chinese=partial_chinese, partial_japanese=partial_japanese,
        message_persisted=persisted_assistant, persisted_lines=persisted_lines,
    )
    if disconnect_snap["should_persist"]:
        return {
            "db_action": "persist_partial", "content_chinese": partial_chinese,
            "content_japanese": partial_japanese, "audio_url": "",
            "lines": disconnect_snap["snapshot_lines"], "settle_affection": False,
        }
    return {
        "db_action": "prune_user", "content_chinese": partial_chinese, "content_japanese": partial_japanese,
        "audio_url": "", "lines": None, "settle_affection": False,
    }


__all__ = [
    "resolve_concat_pause_seconds",
    "plan_master_audio_url",
    "classify_empty_completion_error",
    "build_assistant_message_create",
    "decide_turn_message_action",
    "decide_early_exit_policy",
    "decide_completion_policy",
    "build_completion_done_event",
    "decide_teardown_policy",
]
