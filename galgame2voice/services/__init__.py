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

__all__ = [
    "GptSovitsClient",
    "VoiceManager",
    "TtsService",
    "ChatService",
    "StreamingBilingualParser",
    "clean_japanese_parentheses",
    "resolve_tts_options",
    "SLICING_METHODS",
    "TTS_PRESETS",
    "get_voice_manager",
    "set_voice_manager",
]

