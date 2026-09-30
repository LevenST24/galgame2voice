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

from galgame2voice.database import crud
from galgame2voice.database.session import get_db, get_database_path
from galgame2voice.services.tts_cache_manager import get_tts_cache_manager

logger = logging.getLogger("galgame2voice.services.metrics_collector")

# USD Pricing per 1,000,000 Tokens (Input / Output)
DEFAULT_MODEL_KEY = "default"

MODEL_PRICING_MAP: Dict[str, Dict[str, Tuple[float, float]]] = {
    "deepseek": {
        DEFAULT_MODEL_KEY: (0.14, 0.28),
        "deepseek-chat": (0.14, 0.28),
        "deepseek-reasoner": (0.55, 2.19),
    },
    "openai": {
        DEFAULT_MODEL_KEY: (0.15, 0.60),
        "gpt-4o-mini": (0.15, 0.60),
        "gpt-4o": (2.50, 10.00),
        "o3-mini": (1.10, 4.40),
    },
    "gemini": {
        DEFAULT_MODEL_KEY: (0.075, 0.30),
        "gemini-2.5-flash": (0.15, 0.60),
        "gemini-2.5-pro": (1.25, 10.00),
        "gemini-2.0-flash": (0.10, 0.40),
    },
    "anthropic": {
        DEFAULT_MODEL_KEY: (3.00, 15.00),
        "claude-sonnet-4-20250514": (3.00, 15.00),
        "claude-haiku-4-20250414": (0.80, 4.00),
        "claude-3-5-sonnet-20241022": (3.00, 15.00),
    },
    "qwen": {
        DEFAULT_MODEL_KEY: (0.05, 0.20),
        "qwen-max-latest": (0.20, 0.60),
        "qwen-plus-latest": (0.05, 0.20),
    },
    "glm": {
        DEFAULT_MODEL_KEY: (0.05, 0.05),
        "glm-4-plus": (0.05, 0.05),
        "glm-4-flash": (0.01, 0.01),
    },
    "xai": {
        DEFAULT_MODEL_KEY: (3.00, 15.00),
        "grok-3": (3.00, 15.00),
        "grok-3-mini": (0.30, 0.50),
    },
    "siliconflow": {
        DEFAULT_MODEL_KEY: (0.14, 0.28),
    },
    "moonshot": {
        DEFAULT_MODEL_KEY: (0.20, 0.60),
    },
    "custom": {
        DEFAULT_MODEL_KEY: (0.0, 0.0),  # Local models have no API cost
    },
}

DEFAULT_FALLBACK_PRICE = (0.15, 0.60)
USD_TO_CNY_RATE = 7.20


def _safe_nonneg_int(val: Any) -> int:
    """Safely coerces val to a non-negative integer, returning 0 on None or conversion error."""
    try:
        return max(0, int(val)) if val is not None else 0
    except (TypeError, ValueError):
        return 0


def _safe_nonneg_float(val: Any) -> float:
    """Safely coerces val to a non-negative float, returning 0.0 on None or conversion error."""
    try:
        return max(0.0, float(val)) if val is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


class MetricsCollector:
    """
    Coordinates real-time metric emission, token estimation, cost calculation,
    in-memory ring buffering, and asynchronous database persistence.
    """

    def __init__(self, db_path: Optional[Union[str, Path]] = None, ring_buffer_size: int = 100) -> None:
        self.db_path = str(db_path) if db_path is not None else get_database_path()
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
        input_rate, output_rate = provider_models.get(m_name, provider_models.get(DEFAULT_MODEL_KEY, DEFAULT_FALLBACK_PRICE))

        p_tok = _safe_nonneg_int(prompt_tokens)
        c_tok = _safe_nonneg_int(completion_tokens)

        cost_usd = ((p_tok * input_rate) + (c_tok * output_rate)) / 1_000_000.0
        cost_cny = cost_usd * USD_TO_CNY_RATE

        return round(cost_usd, 6), round(cost_cny, 4)

    @staticmethod
    def estimate_tokens(text: str) -> int:
        """
        Estimates token count with high accuracy across mixed CJK and Latin scripts.
        """
        if not text or not isinstance(text, str):
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
        safe_prompt_tok = _safe_nonneg_int(prompt_tokens)
        safe_comp_tok = _safe_nonneg_int(completion_tokens)

        cost_usd, cost_cny = self.calculate_cost(
            provider_id=provider_id,
            model_name=model_name,
            prompt_tokens=safe_prompt_tok,
            completion_tokens=safe_comp_tok,
        )
        total_tokens = safe_prompt_tok + safe_comp_tok
        iso_timestamp = datetime.now(timezone.utc).isoformat()

        safe_ttft = _safe_nonneg_float(ttft_ms)
        safe_tts_first = _safe_nonneg_float(tts_first_chunk_ms)
        safe_total_lat = _safe_nonneg_float(total_latency_ms)

        metric_record = {
            "timestamp": iso_timestamp,
            "session_id": str(session_id or "default"),
            "channel": str(channel or "web"),
            "provider_id": str(provider_id or "deepseek"),
            "model_name": str(model_name or "deepseek-chat"),
            "prompt_tokens": safe_prompt_tok,
            "completion_tokens": safe_comp_tok,
            "total_tokens": total_tokens,
            "estimated_cost_usd": cost_usd,
            "estimated_cost_cny": cost_cny,
            "ttft_ms": round(safe_ttft, 1),
            "tts_first_chunk_ms": round(safe_tts_first, 1),
            "total_latency_ms": round(safe_total_lat, 1),
            "tts_cached_chunks": _safe_nonneg_int(tts_cached_chunks),
            "tts_generated_chunks": _safe_nonneg_int(tts_generated_chunks),
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

    async def record_chat_turn(
        self,
        session_id: str,
        provider_id: str,
        model_name: str,
        messages: List[Any],
        chinese: str,
        japanese: str,
        ttft_ms: float,
        tts_first_chunk_ms: float,
        total_latency_ms: float,
        tts_cached_chunks: int = 0,
        tts_generated_chunks: int = 0,
        channel: str = "web",
    ) -> Dict[str, Any]:
        """
        Calculates prompt and completion tokens from conversation messages and bilingual response,
        then records telemetry record.
        """
        prompt_text = "".join([getattr(m, "content", "") for m in messages])
        prompt_tokens = self.estimate_tokens(prompt_text)
        completion_tokens = self.estimate_tokens(chinese + japanese)
        return await self.record_metric(
            session_id=session_id,
            channel=channel,
            provider_id=provider_id,
            model_name=model_name,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            ttft_ms=ttft_ms,
            tts_first_chunk_ms=tts_first_chunk_ms,
            total_latency_ms=total_latency_ms,
            tts_cached_chunks=tts_cached_chunks,
            tts_generated_chunks=tts_generated_chunks,
        )

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

        # Retrieve real-time TTS speed and dynamic batch scheduler telemetry
        try:
            from galgame2voice.services.dynamic_batcher import get_speed_tracker
            overview["tts_speed"] = get_speed_tracker().get_telemetry()
        except Exception as exc:
            logger.debug("Could not read TTS speed telemetry: %s", exc)
            overview["tts_speed"] = None

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
