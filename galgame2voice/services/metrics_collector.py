"""
Token Usage & Latency Telemetry Collector for galgame2voice.
Tracks real-time prompt/completion tokens, model pricing costs (USD/CNY),
TTFT (Time To First Token), TTS first chunk latency, and total E2E duration.
Employs a dual-tier architecture: an in-memory ring buffer for instant UI queries
and SQLite persistent storage for long-term historical analytics.
"""

from collections import Counter, deque
from datetime import datetime, timezone
import logging
from typing import Any
from pathlib import Path
from itertools import repeat

from galgame2voice.database import crud
from galgame2voice.database.session import get_db, get_database_path

logger = logging.getLogger("galgame2voice.services.metrics_collector")

# Limit temporary memory independently of the length of the whole context.
_TOKEN_COUNT_CHUNK_SIZE = 2048

# USD Pricing per 1,000,000 Tokens (Input / Output)
DEFAULT_MODEL_KEY = "default"

MODEL_PRICING_MAP: dict[str, dict[str, tuple[float, float]]] = {
    "deepseek": {
        DEFAULT_MODEL_KEY: (0.14, 0.28),
        "deepseek-flash": (0.30, 1.20),
        "deepseek-v4-pro": (1.32, 3.96),
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

    def __init__(self, db_path: str | Path | None = None, ring_buffer_size: int = 100) -> None:
        self.db_path = str(db_path) if db_path is not None else get_database_path()
        self.ring_buffer: deque = deque(maxlen=ring_buffer_size)

    def calculate_cost(
        self,
        provider_id: str,
        model_name: str,
        prompt_tokens: int,
        completion_tokens: int,
    ) -> tuple[float, float]:
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
        for offset in range(0, len(text), _TOKEN_COUNT_CHUNK_SIZE):
            chunk = text[offset:offset + _TOKEN_COUNT_CHUNK_SIZE]
            sample = chunk[:256]
            # A mostly unique chunk gains little from a frequency table. Keep
            # this branch local to the chunk so later repetitive text is fast.
            if len(sample) == 256 and len(set(sample)) >= 128:
                counts = zip(chunk, repeat(1))
            else:
                counts = Counter(chunk).items()
            for ch, count in counts:
                code = ord(ch)
                if (
                    0x4E00 <= code <= 0x9FFF
                    or 0x3040 <= code <= 0x309F
                    or 0x30A0 <= code <= 0x30FF
                    or 0x3400 <= code <= 0x4DBF
                ):
                    cjk_count += count
                elif not ch.isspace():
                    non_cjk_chars += count

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
    ) -> dict[str, Any]:
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
        messages: list[Any],
        chinese: str,
        japanese: str,
        ttft_ms: float,
        tts_first_chunk_ms: float,
        total_latency_ms: float,
        tts_cached_chunks: int = 0,
        tts_generated_chunks: int = 0,
        channel: str = "web",
    ) -> dict[str, Any]:
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


# Singleton accessor
_metrics_collector_instance: MetricsCollector | None = None


def get_metrics_collector(db_path: str | Path | None = None) -> MetricsCollector:
    """Returns singleton instance of MetricsCollector."""
    global _metrics_collector_instance
    if _metrics_collector_instance is None:
        _metrics_collector_instance = MetricsCollector(db_path=db_path)
    return _metrics_collector_instance


def reset_metrics_collector() -> None:
    """Resets the singleton for test isolation."""
    global _metrics_collector_instance
    _metrics_collector_instance = None
