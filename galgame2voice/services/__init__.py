"""
Services module for galgame2voice.
Exports GptSovitsClient, VoiceManager, TtsService, and TTS utilities.
"""

from galgame2voice.services.gpt_sovits_client import (
    GptSovitsClient,
    clean_japanese_parentheses,
    resolve_tts_options,
    SLICING_METHODS,
    TTS_PRESETS,
)
from galgame2voice.services.tts_service import (
    TtsService,
)
from galgame2voice.services.voice_manager import (
    VoiceManager,
    get_voice_manager,
    set_voice_manager,
)

from galgame2voice.services.chat_service import (
    ChatService,
    StreamingBilingualParser,
)

from galgame2voice.services.affection_service import AffectionService
from galgame2voice.services.memory_service import MemoryService
from galgame2voice.services.session_manager import SessionManager, SessionTurn
from galgame2voice.services.metrics_collector import MetricsCollector, get_metrics_collector
from galgame2voice.services.tts_cache_manager import TtsCacheManager, get_tts_cache_manager

__all__ = [
    "GptSovitsClient",
    "VoiceManager",
    "TtsService",
    "ChatService",
    "StreamingBilingualParser",
    "AffectionService",
    "MemoryService",
    "SessionManager",
    "SessionTurn",
    "MetricsCollector",
    "TtsCacheManager",
    "get_metrics_collector",
    "get_tts_cache_manager",
    "clean_japanese_parentheses",
    "resolve_tts_options",
    "SLICING_METHODS",
    "TTS_PRESETS",
    "get_voice_manager",
    "set_voice_manager",
]

