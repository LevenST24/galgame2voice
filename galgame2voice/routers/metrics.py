"""
Metrics & Telemetry API Router for galgame2voice.
Provides endpoints for global token telemetry, model distribution,
latency trends, and TTS persistent audio cache management.
"""

import logging
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Query, status

from galgame2voice.database.models import (
    MetricsOverviewResponse,
    ProvidersMetricsResponse,
    LatencyTrendResponse,
    CacheStatsResponse,
)
from galgame2voice.services.metrics_collector import get_metrics_collector
from galgame2voice.services.tts_cache_manager import get_tts_cache_manager

logger = logging.getLogger("galgame2voice.routers.metrics")

router = APIRouter(tags=["Metrics & Cache"])


@router.get(
    "/api/metrics/overview",
    response_model=MetricsOverviewResponse,
    summary="Get Token & Latency Telemetry Overview",
    description="Returns aggregate metrics including token counts, pricing in USD/CNY, average latency, and TTS cache stats.",
)
async def get_metrics_overview() -> MetricsOverviewResponse:
    collector = get_metrics_collector()
    overview_data = await collector.get_overview()
    return MetricsOverviewResponse(**overview_data)


@router.get(
    "/api/metrics/providers",
    response_model=ProvidersMetricsResponse,
    summary="Get Token & Cost Breakdown by Provider",
    description="Returns token and cost breakdown across DeepSeek, OpenAI, Gemini, Claude, Qwen, GLM, etc.",
)
async def get_metrics_providers() -> ProvidersMetricsResponse:
    collector = get_metrics_collector()
    providers_data = await collector.get_providers()
    return ProvidersMetricsResponse(providers=providers_data)


@router.get(
    "/api/metrics/latency-trend",
    response_model=LatencyTrendResponse,
    summary="Get Recent Request Latency Timeseries",
    description="Returns recent TTFT, TTS first chunk, and total latency timeseries for trend visualization.",
)
async def get_latency_trend(
    limit: int = Query(default=30, ge=1, le=200, description="Max number of recent requests to return")
) -> LatencyTrendResponse:
    collector = get_metrics_collector()
    trend_data = await collector.get_latency_trend(limit=limit)
    return LatencyTrendResponse(trend=trend_data)


@router.get(
    "/api/cache/stats",
    response_model=CacheStatsResponse,
    summary="Get TTS Persistent Cache Statistics",
    description="Returns current disk usage, entry count, hits, misses, and hit rate percentage.",
)
async def get_cache_stats() -> CacheStatsResponse:
    cache_mgr = get_tts_cache_manager()
    stats = await cache_mgr.get_stats()
    return CacheStatsResponse(**stats)


@router.post(
    "/api/cache/clear",
    summary="Clear TTS Persistent Audio Cache",
    description="Deletes all cached audio files in audio/cache/ and purges SQLite metadata.",
)
async def clear_cache() -> Dict[str, Any]:
    cache_mgr = get_tts_cache_manager()
    deleted_files, freed_mb = await cache_mgr.clear()
    return {
        "status": "cleared",
        "deleted_files": deleted_files,
        "freed_mb": freed_mb,
    }
