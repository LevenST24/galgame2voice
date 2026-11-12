"""
Pure snapshot and terminal state calculations for Galgame2Voice streaming chat pipeline.
Decouples terminal line/audio status resolution, early-exit/completion/disconnect snapshot
assembly, and done event payload construction from asyncio queues and database state.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence


def has_meaningful_text(chinese: str | None = "", japanese: str | None = "") -> bool:
    """Returns True if either Chinese or Japanese string contains non-whitespace text."""
    return bool((chinese or "").strip() or (japanese or "").strip())


def new_line_audio_state(index: int) -> dict[str, Any]:
    """Creates the initial mutable audio state dictionary for a dialogue line."""
    return {
        "line": index,
        "sealed": False,
        "pending": 0,
        "chunks": [],
        "errors": [],
        "emitted": False,
        "status": None,
    }


def build_line_audio_event(index: int, state: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    """Resolves terminal line_audio status and constructs the line_audio SSE event dict."""
    errors = state.get("errors") or []
    chunks = list(state.get("chunks") or [])
    status = "failed" if errors else ("ready" if chunks else "skipped")
    data: dict[str, Any] = {
        "line": index,
        "status": status,
        "chunks": chunks,
    }
    if status == "failed":
        data["error"] = "; ".join(dict.fromkeys(errors))
    return status, {"event": "line_audio", "data": data}


def resolve_line_audio_snapshot_status(
    state: Mapping[str, Any] | None,
    *,
    mark_unfinished_failed: bool = False,
) -> tuple[str, list[dict[str, Any]]]:
    """Resolves the snapshot audio (status, chunks) tuple for a single line's audio state."""
    if state is None:
        return ("failed" if mark_unfinished_failed else "skipped", [])

    chunks = list(state.get("chunks") or [])
    if state.get("emitted"):
        status = state["status"]
    elif state.get("sealed") and state.get("pending", 0) == 0 and not state.get("errors"):
        status = "ready" if chunks else "skipped"
    elif mark_unfinished_failed:
        status = "failed"
    else:
        status = "failed" if state.get("errors") else ("ready" if chunks else "skipped")
    return status, chunks


def snapshot_lines_with_audio(
    announced_lines: Sequence[Mapping[str, Any]],
    line_audio_states: Mapping[int, Mapping[str, Any]],
    *,
    mark_unfinished_failed: bool = False,
) -> list[dict[str, Any]]:
    """Builds a terminal snapshot of announced lines paired with their resolved audio status and chunks."""
    result: list[dict[str, Any]] = []
    for line in announced_lines:
        line_copy = dict(line)
        idx = line["index"]
        state = line_audio_states.get(idx)
        status, chunks = resolve_line_audio_snapshot_status(
            state,
            mark_unfinished_failed=mark_unfinished_failed,
        )
        line_copy["audio"] = {
            "status": status,
            "chunks": chunks,
        }
        result.append(line_copy)
    return result


def clean_audio_chunks(
    audio_chunks: Sequence[Mapping[str, Any]],
    *,
    include_line: bool = False,
) -> list[dict[str, Any]]:
    """Strips internal fields (such as local_path and is_cached) from audio chunks before emitting."""
    return [
        {
            "index": c.get("index", i),
            "audio_url": c.get("audio_url", ""),
            "sentence": c.get("sentence", ""),
            **({"line": c["line"]} if include_line and "line" in c else {}),
        }
        for i, c in enumerate(audio_chunks)
    ]


def build_final_tts_params(parser: Any, ai_adaptive_voice: Any) -> dict[str, Any] | None:
    """Constructs final TTS parameter payload from parser attributes if available."""
    if parser.tts_speed is not None or parser.tts_temperature is not None or parser.tts_emotion is not None:
        return {
            "speed": parser.tts_speed,
            "temperature": parser.tts_temperature,
            "emotion": parser.tts_emotion,
            "adaptive_enabled": bool(ai_adaptive_voice),
        }
    return None


def compute_early_exit_snapshot(
    *,
    announced_lines: Sequence[Mapping[str, Any]],
    line_audio_states: Mapping[int, Mapping[str, Any]],
    partial_chinese: str | None,
    partial_japanese: str | None,
    cancel_flagged: bool,
    is_truncation: bool = True,
) -> dict[str, Any]:
    """Computes terminal snapshot and persistence flags for early exit (error, truncation, or cancellation)."""
    snapshot_lines = snapshot_lines_with_audio(
        announced_lines,
        line_audio_states,
        mark_unfinished_failed=True,
    )
    partial_ch = partial_chinese or ""
    partial_ja = partial_japanese or ""
    has_partial = has_meaningful_text(partial_ch, partial_ja) or bool(snapshot_lines)
    emit_truncated_done = bool(cancel_flagged) or (bool(is_truncation) and has_partial)
    should_persist = has_partial and emit_truncated_done
    return {
        "snapshot_lines": snapshot_lines,
        "partial_chinese": partial_ch,
        "partial_japanese": partial_ja,
        "has_partial": has_partial,
        "should_persist": should_persist,
        "emit_truncated_done": emit_truncated_done,
    }


def build_early_done_event(
    *,
    partial_chinese: str | None,
    partial_japanese: str | None,
    snapshot_lines: Sequence[Mapping[str, Any]],
    reply_format: str = "lines",
    message_id: int | None = None,
) -> dict[str, Any]:
    """Constructs the truncated done SSE event payload for early-terminated streams."""
    return {
        "event": "done",
        "data": {
            "truncated": True,
            "chinese": partial_chinese or "",
            "japanese": partial_japanese or "",
            "lines": list(snapshot_lines),
            "reply_format": reply_format,
            **({"message_id": message_id} if message_id is not None else {}),
        },
    }


def compute_completion_snapshot(
    *,
    announced_lines: Sequence[Mapping[str, Any]],
    line_audio_states: Mapping[int, Mapping[str, Any]],
    final_chinese: str | None,
    final_japanese: str | None,
    lines_truncated: bool = False,
) -> dict[str, Any]:
    """Computes terminal lines and reply status on normal stream completion."""
    final_ch = final_chinese or ""
    final_ja = final_japanese or ""
    final_lines = snapshot_lines_with_audio(
        announced_lines,
        line_audio_states,
        mark_unfinished_failed=False,
    )
    has_reply = has_meaningful_text(final_ch, final_ja) or bool(final_lines)
    return {
        "final_lines": final_lines,
        "final_chinese": final_ch,
        "final_japanese": final_ja,
        "has_reply": has_reply,
        "lines_truncated": bool(lines_truncated),
    }


def compute_disconnect_snapshot(
    *,
    announced_lines: Sequence[Mapping[str, Any]],
    line_audio_states: Mapping[int, Mapping[str, Any]],
    partial_chinese: str | None,
    partial_japanese: str | None,
    message_persisted: bool,
    persisted_lines: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Computes teardown persistence snapshot when the stream exits without having persisted."""
    partial_ch = partial_chinese or ""
    partial_ja = partial_japanese or ""
    if message_persisted or not has_meaningful_text(partial_ch, partial_ja):
        return {
            "should_persist": False,
            "partial_chinese": partial_ch,
            "partial_japanese": partial_ja,
            "snapshot_lines": None,
        }

    lines_to_save = persisted_lines
    if lines_to_save is None and announced_lines:
        lines_to_save = snapshot_lines_with_audio(
            announced_lines,
            line_audio_states,
            mark_unfinished_failed=True,
        )
    return {
        "should_persist": True,
        "partial_chinese": partial_ch,
        "partial_japanese": partial_ja,
        "snapshot_lines": lines_to_save,
    }


__all__ = [
    "has_meaningful_text",
    "new_line_audio_state",
    "build_line_audio_event",
    "resolve_line_audio_snapshot_status",
    "snapshot_lines_with_audio",
    "clean_audio_chunks",
    "build_final_tts_params",
    "compute_early_exit_snapshot",
    "build_early_done_event",
    "compute_completion_snapshot",
    "compute_disconnect_snapshot",
]
