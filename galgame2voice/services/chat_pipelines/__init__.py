"""
Chat Pipelines Package for Galgame2Voice.
Modularized pipelines for LLM streaming, bilingual text parsing & segmentation,
and priority-scheduled TTS audio generation.
"""

from __future__ import annotations

from . import stream_persist_policy, stream_snapshot
from .llm_pipeline import LlmStreamPipeline
from .stream_coordinator import SseKeepAlive, StreamCoordinator
from .stream_persist_policy import (
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
from .stream_snapshot import (
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
from .text_pipeline import TextSegmentationPipeline
from .tts_pipeline import TtsStreamPipeline

__all__ = [
    "LlmStreamPipeline",
    "TextSegmentationPipeline",
    "TtsStreamPipeline",
    "StreamCoordinator",
    "SseKeepAlive",
    "stream_persist_policy",
    "resolve_concat_pause_seconds",
    "plan_master_audio_url",
    "classify_empty_completion_error",
    "build_assistant_message_create",
    "decide_turn_message_action",
    "decide_early_exit_policy",
    "decide_completion_policy",
    "build_completion_done_event",
    "decide_teardown_policy",
    "stream_snapshot",
    "build_early_done_event",
    "build_final_tts_params",
    "build_line_audio_event",
    "clean_audio_chunks",
    "compute_completion_snapshot",
    "compute_disconnect_snapshot",
    "compute_early_exit_snapshot",
    "has_meaningful_text",
    "new_line_audio_state",
    "resolve_line_audio_snapshot_status",
    "snapshot_lines_with_audio",
]
