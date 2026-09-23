"""
Two-Tier TTS Cache compatibility alias module for galgame2voice.
Re-exports TtsCacheManager, TtsCacheEntry, and singleton accessor functions
from galgame2voice.services.tts_cache_manager and database models.
"""

from galgame2voice.database.models import TtsCacheEntry
from galgame2voice.services.tts_cache_manager import (
    TtsCacheManager,
    get_tts_cache_manager,
    reset_tts_cache_manager,
)

__all__ = [
    "TtsCacheManager",
    "TtsCacheEntry",
    "get_tts_cache_manager",
    "reset_tts_cache_manager",
]
