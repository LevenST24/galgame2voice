"""
Token Usage & Latency Telemetry Collector for galgame2voice.
Tracks real-time prompt/completion tokens, model pricing costs (USD/CNY),
TTFT (Time To First Token), TTS first chunk latency, and total E2E duration.
Employs a dual-tier architecture: an in-memory ring buffer for instant UI queries
and SQLite persistent storage for long-term historical analytics.
"""

from collections import deque
from datetime import datetime, timezone
import logging
from typing import Any, Dict, List, Optional, Tuple, Union
from pathlib import Path

from galgame2voice.config import get_settings
from galgame2voice.database import crud
from galgame2voice.database.session import get_db
from galgame2voice.services.tts_cache_manager import get_tts_cache_manager

logger = logging.getLogger("galgame2voice.services.metrics_collector")

# USD Pricing per 1,000,000 Tokens (Input / Output)
MODEL_PRICING_MAP: Dict[str, Dict[str, Tuple[float, float]]] = {
    "deepseek": {
        "default": (0.14, 0.28),
        "deepseek-chat": (0.14, 0.28),
        "deepseek-reasoner": (0.55, 2.19),
    },
    "openai": {
        "default": (0.15, 0.60),
        "gpt-4o-mini": (0.15, 0.60),
        "gpt-4o": (2.50, 10.00),
        "o3-mini": (1.10, 4.40),
    },
    "gemini": {
        "default": (0.075, 0.30),
        "gemini-2.5-flash": (0.15, 0.60),
        "gemini-2.5-pro": (1.25, 10.00),
        "gemini-2.0-flash": (0.10, 0.40),
    },
    "anthropic": {
        "default": (3.00, 15.00),
        "claude-sonnet-4-20250514": (3.00, 15.00),
        "claude-haiku-4-20250414": (0.80, 4.00),
        "claude-3-5-sonnet-20241022": (3.00, 15.00),
    },
    "qwen": {
        "default": (0.05, 0.20),
        "qwen-max-latest": (0.20, 0.60),
        "qwen-plus-latest": (0.05, 0.20),
    },
    "glm": {
        "default": (0.05, 0.05),
        "glm-4-plus": (0.05, 0.05),
        "glm-4-flash": (0.01, 0.01),
    },
    "xai": {
        "default": (3.00, 15.00),
        "grok-3": (3.00, 15.00),
        "grok-3-mini": (0.30, 0.50),
    },
    "siliconflow": {
        "default": (0.14, 0.28),
    },
    "groq": {
        "default": (0.59, 0.79),
    },
    "moonshot": {
        "default": (0.20, 0.60),
    },
    "custom": {
        "default": (0.0, 0.0),  # Local models have no API cost
    },
}

DEFAULT_FALLBACK_PRICE = (0.15, 0.60)
USD_TO_CNY_RATE = 7.20


class MetricsCollector:
    """
    Coordinates real-time metric emission, token estimation, cost calculation,
    in-memory ring buffering, and asynchronous database persistence.
    """

    def __init__(self, db_path: Optional[Union[str, Path]] = None, ring_buffer_size: int = 100):
        settings = get_settings()
        self.db_path = str(db_path or settings.db_path)
        self.ring_buffer: deque = deque(maxlen=ring_buffer_size)

    def calculate_cost(
        self,
        provider_id: str,
        model_name: str,
        prompt_tokens: int,
        completion_tokens: int,
    ) -> Tuple[float, float]:
        """
        Calculates estimated cost in USD and CNY for prompt and completion tokens.
        Returns (cost_usd, cost_cny).
        """
        pid = (provider_id or "").lower().strip()
        m_name = (model_name or "").lower().strip()

        provider_models = MODEL_PRICING_MAP.get(pid, {})
        input_rate, output_rate = provider_models.get(m_name, provider_models.get("default", DEFAULT_FALLBACK_PRICE))

        cost_usd = ((prompt_tokens * input_rate) + (completion_tokens * output_rate)) / 1_000_000.0
        cost_cny = cost_usd * USD_TO_CNY_RATE

        return round(cost_usd, 6), round(cost_cny, 4)

    @staticmethod
    def estimate_tokens(text: str) -> int:
        """
        Estimates token count with high accuracy across mixed CJK and Latin scripts.
        """
        if not text:
            return 0
        
        cjk_count = 0
        non_cjk_chars = 0
        for ch in text:
            code = ord(ch)
            if (
                0x4E00 <= code <= 0x9FFF
                or 0x3040 <= code <= 0x309F
                or 0x30A0 <= code <= 0x30FF
                or 0x3400 <= code <= 0x4DBF
            ):
                cjk_count += 1
            elif not ch.isspace():
                non_cjk_chars += 1

        estimated = int(cjk_count * 1.1 + (non_cjk_chars / 3.5) + 1)
        return max(1, estimated)

    async def record_metric(
        self,
        session_id: str = "default",
        channel: str = "web",
        provider_id: str = "deepseek",
        model_name: str = "deepseek-chat",
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        ttft_ms: float = 0.0,
        tts_first_chunk_ms: float = 0.0,
        total_latency_ms: float = 0.0,
        tts_cached_chunks: int = 0,
        tts_generated_chunks: int = 0,
    ) -> Dict[str, Any]:
        """
        Records telemetry for an end-to-end request.
        Updates in-memory ring buffer and persists asynchronously to SQLite.
        """
        cost_usd, cost_cny = self.calculate_cost(
            provider_id=provider_id,
            model_name=model_name,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )
        total_tokens = prompt_tokens + completion_tokens
        iso_timestamp = datetime.now(timezone.utc).isoformat()

        metric_record = {
            "timestamp": iso_timestamp,
            "session_id": session_id,
            "channel": channel,
            "provider_id": provider_id,
            "model_name": model_name,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "estimated_cost_usd": cost_usd,
            "estimated_cost_cny": cost_cny,
            "ttft_ms": round(float(ttft_ms), 1),
            "tts_first_chunk_ms": round(float(tts_first_chunk_ms), 1),
            "total_latency_ms": round(float(total_latency_ms), 1),
            "tts_cached_chunks": tts_cached_chunks,
            "tts_generated_chunks": tts_generated_chunks,
        }

        # Add to in-memory ring buffer
        self.ring_buffer.append(metric_record)

        # Asynchronously persist to SQLite
        try:
            async with get_db(self.db_path) as conn:
                await crud.insert_token_metric(
                    conn=conn,
                    session_id=session_id,
                    channel=channel,
                    provider_id=provider_id,
                    model_name=model_name,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    estimated_cost=cost_usd,
                    ttft_ms=ttft_ms,
                    tts_first_chunk_ms=tts_first_chunk_ms,
                    total_latency_ms=total_latency_ms,
                    tts_cached_chunks=tts_cached_chunks,
                    tts_generated_chunks=tts_generated_chunks,
                )
        except Exception as exc:
            logger.warning("Failed to persist metric log to SQLite: %s", exc)

        return metric_record

    async def get_overview(self) -> Dict[str, Any]:
        """
        Retrieves global token telemetry aggregated overview combined with TTS cache stats.
        """
        try:
            async with get_db(self.db_path) as conn:
                db_overview = await crud.get_metrics_overview(conn)
        except Exception as exc:
            logger.warning("Could not read db metrics overview: %s", exc)
            db_overview = {
                "total_requests": 0, "total_prompt_tokens": 0, "total_completion_tokens": 0,
                "total_tokens": 0, "estimated_cost_usd": 0.0, "estimated_cost_cny": 0.0,
                "avg_ttft_ms": 0.0, "avg_tts_first_chunk_ms": 0.0, "avg_total_latency_ms": 0.0,
            }

        # Retrieve TTS cache stats
        cache_manager = get_tts_cache_manager(db_path=self.db_path)
        cache_stats = await cache_manager.get_stats()

        overview = dict(db_overview)
        overview["cache_stats"] = cache_stats
        return overview

    async def get_providers(self) -> List[Dict[str, Any]]:
        """Retrieves breakdown of token usage and costs by provider."""
        try:
            async with get_db(self.db_path) as conn:
                return await crud.get_provider_metrics_breakdown(conn)
        except Exception as exc:
            logger.warning("Could not read provider metrics breakdown: %s", exc)
            return []

    async def get_latency_trend(self, limit: int = 30) -> List[Dict[str, Any]]:
        """
        Retrieves recent latency measurements from in-memory ring buffer or database.
        """
        if len(self.ring_buffer) >= min(5, limit):
            # Return from ring buffer
            recent = list(self.ring_buffer)[-limit:]
            return [
                {
                    "timestamp": r["timestamp"],
                    "ttft_ms": r["ttft_ms"],
                    "tts_first_chunk_ms": r["tts_first_chunk_ms"],
                    "total_latency_ms": r["total_latency_ms"],
                    "model_name": r["model_name"],
                    "provider_id": r["provider_id"],
                }
                for r in recent
            ]

        try:
            async with get_db(self.db_path) as conn:
                return await crud.get_recent_latency_trends(conn, limit=limit)
        except Exception as exc:
            logger.warning("Could not read latency trends from DB: %s", exc)
            return []


# Singleton accessor
_metrics_collector_instance: Optional[MetricsCollector] = None


def get_metrics_collector(db_path: Optional[Union[str, Path]] = None) -> MetricsCollector:
    """Returns singleton instance of MetricsCollector."""
    global _metrics_collector_instance
    if _metrics_collector_instance is None:
        _metrics_collector_instance = MetricsCollector(db_path=db_path)
    return _metrics_collector_instance


def reset_metrics_collector() -> None:
    """Resets the singleton for test isolation."""
    global _metrics_collector_instance
    _metrics_collector_instance = None
