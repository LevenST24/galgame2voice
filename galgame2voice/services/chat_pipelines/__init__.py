"""
Chat Pipelines Package for Galgame2Voice.
Modularized pipelines for LLM streaming, bilingual text parsing & segmentation,
and priority-scheduled TTS audio generation.
"""

from __future__ import annotations

from . import stream_snapshot
from .llm_pipeline import LlmStreamPipeline
from .stream_coordinator import SseKeepAlive, StreamCoordinator
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
