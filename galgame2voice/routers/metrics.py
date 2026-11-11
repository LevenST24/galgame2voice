"""
TTS Persistent Audio Cache API Router for galgame2voice.
Provides endpoints for inspecting cache statistics and clearing cached audio files.
"""

import logging
from typing import Any
from fastapi import APIRouter

from galgame2voice.database.models import CacheStatsResponse
from galgame2voice.services.tts_cache_manager import get_tts_cache_manager

logger = logging.getLogger("galgame2voice.routers.metrics")

router = APIRouter(tags=["Metrics & Cache"])


@router.get(
    "/api/cache/stats",
    response_model=CacheStatsResponse,
    summary="Get TTS Persistent Cache Statistics",
    description="Returns current disk usage, entry count, hits, misses, and hit rate percentage.",
)
async def get_cache_stats() -> CacheStatsResponse:
    cache_mgr = get_tts_cache_manager()
    stats = await cache_mgr.get_stats()
    # Keep the dashboard-facing alias in sync; the frontend reads total_entries.
    stats["total_entries"] = stats.get("total_files", 0)
    return CacheStatsResponse(**stats)


@router.post(
    "/api/cache/clear",
    summary="Clear TTS Persistent Audio Cache",
    description="Deletes all cached audio files in audio/cache/ and purges SQLite metadata.",
)
async def clear_cache() -> dict[str, Any]:
    cache_mgr = get_tts_cache_manager()
    deleted_files, freed_mb = await cache_mgr.clear()
    return {
        "status": "cleared",
        "deleted_files": deleted_files,
        "freed_mb": freed_mb,
    }
