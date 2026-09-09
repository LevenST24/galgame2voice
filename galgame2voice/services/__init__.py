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

from galgame2voice.services.emotion_classifier import (
    EMOTION_KEYWORDS,
    VALID_EMOTIONS,
    EMOTION_NAME_MAP,
    classify_emotion,
)
from galgame2voice.services.streaming_parser import StreamingBilingualParser
from galgame2voice.services.chat_service import (
    ChatService,
    DYNAMIC_SPEED_MIN,
    DYNAMIC_SPEED_MAX,
    DYNAMIC_TEMP_MIN,
    DYNAMIC_TEMP_MAX,
    clamp_dynamic_speed,
    clamp_dynamic_temperature,
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
    "classify_emotion",
    "EMOTION_KEYWORDS",
    "VALID_EMOTIONS",
    "EMOTION_NAME_MAP",
    "DYNAMIC_SPEED_MIN",
    "DYNAMIC_SPEED_MAX",
    "DYNAMIC_TEMP_MIN",
    "DYNAMIC_TEMP_MAX",
    "clamp_dynamic_speed",
    "clamp_dynamic_temperature",
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

