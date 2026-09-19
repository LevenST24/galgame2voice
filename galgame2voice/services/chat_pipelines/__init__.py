"""
Chat Pipelines Package for Galgame2Voice.
Modularized pipelines for LLM streaming, bilingual text parsing & segmentation,
and priority-scheduled TTS audio generation.
"""

from __future__ import annotations

from .llm_pipeline import LlmStreamPipeline
from .text_pipeline import TextSegmentationPipeline
from .tts_pipeline import TtsStreamPipeline
from .stream_coordinator import StreamCoordinator, SseKeepAlive

__all__ = [
    "LlmStreamPipeline",
    "TextSegmentationPipeline",
    "TtsStreamPipeline",
    "StreamCoordinator",
    "SseKeepAlive",
]

