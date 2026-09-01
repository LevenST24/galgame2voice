"""
Health check and system diagnostic router for galgame2voice.
Provides /api/health, /status, and /api/system/status endpoints.

All filesystem scans run in worker threads and are cached with a TTL so the
5-second frontend status poll never blocks the event loop.
"""

import asyncio
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import httpx
from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, Field

from galgame2voice.config import get_settings
from galgame2voice.database.session import get_db
from galgame2voice.database import crud
from galgame2voice.security.auth import require_auth

router = APIRouter(tags=["Health & Diagnostics"])

# Directory metrics are cached: the settings console polls every few seconds,
# and scanning thousands of cache files each time would freeze the event loop.
_DIR_METRICS_TTL_SECONDS = 15.0
_dir_metrics_cache: Dict[str, Tuple[float, Tuple[int, float]]] = {}


class HealthResponse(BaseModel):
    """Lightweight health check response."""
    status: str = Field(default="ok", json_schema_extra={"example": "ok"})
    app: str = Field(default="galgame2voice", json_schema_extra={"example": "galgame2voice"})
    version: str = Field(default="2.0.0", json_schema_extra={"example": "2.0.0"})
    uptime_seconds: float = Field(..., json_schema_extra={"example": 120.5})


class LegacyStatusResponse(BaseModel):
    """Legacy endpoint compatibility response."""
    status: str = Field(default="ok", json_schema_extra={"example": "ok"})
    app: str = Field(default="galgame2voice", json_schema_extra={"example": "galgame2voice"})
    version: str = Field(default="2.0.0", json_schema_extra={"example": "2.0.0"})
    gpt_sovits: str = Field(default="reachable", json_schema_extra={"example": "reachable"})


class AppTelemetry(BaseModel):
    """Application level telemetry information."""
    name: str = "galgame2voice"
    version: str = "2.0.0"
    uptime_seconds: float
    start_time: str
    python_version: str
    pid: int
    memory_usage_mb: Optional[float] = None


class DatabaseTelemetry(BaseModel):
    """Database connectivity and state telemetry."""
    status: str
    wal_mode: bool
    path: str


class GptSovitsTelemetry(BaseModel):
    """GPT-SoVITS backend reachability telemetry."""
    status: str  # "reachable" | "unreachable"
    base_url: str
    latency_ms: Optional[float] = None
    error: Optional[str] = None


class StorageTelemetry(BaseModel):
    """Storage metrics for audio and data directories."""
    audio_files_count: int
    audio_dir_size_mb: float
    data_dir_size_mb: float


class TelegramTelemetry(BaseModel):
    """Telegram bot integration status."""
    enabled: bool
    status: str  # "disabled" | "running" | "error"


class SystemStatusResponse(BaseModel):
    """Full comprehensive system diagnostic status response."""
    status: str  # "healthy" | "degraded"
    timestamp: str
    app: AppTelemetry
    database: DatabaseTelemetry
    gpt_sovits: GptSovitsTelemetry
    storage: StorageTelemetry
    telegram: TelegramTelemetry


async def _probe_gpt_sovits(base_url: str) -> GptSovitsTelemetry:
    """
    Checks GPT-SoVITS reachability through the shared singleton client pool
    (GET / — api_v2 answers on the root path). 3s connect/read budget:
    long enough for a busy GPU to answer, short enough for a 5s poll.
    HTTP 200/400 counts as reachable; other codes / network errors do not.
    """
    t0 = time.perf_counter()
    try:
        from galgame2voice.services.gpt_sovits_client import get_gpt_sovits_client
        client = get_gpt_sovits_client()

        target = base_url.rstrip("/")
        if target and target != client.base_url:
            # One-shot probe against an explicitly different URL.
            # Connect budget 1s: healthy local engines connect in <50ms;
            # some VPN/TUN stacks delay loopback refusals to ~2s, so a tight
            # budget converts "engine down" into a fast unreachable verdict.
            timeout = httpx.Timeout(connect=1.0, read=2.5, write=2.5, pool=2.5)
            async with httpx.AsyncClient(trust_env=False, timeout=timeout) as one_shot:
                try:
                    resp = await one_shot.get(f"{target}/control")
                except Exception:
                    resp = await one_shot.get(f"{target}/")
                latency = round((time.perf_counter() - t0) * 1000, 2)
                if resp.status_code in (200, 400):
                    return GptSovitsTelemetry(
                        status="reachable", base_url=base_url, latency_ms=latency, error=None)
                return GptSovitsTelemetry(
                    status="unreachable", base_url=base_url, latency_ms=latency,
                    error=f"Unexpected status code: {resp.status_code}")

        result = await client.check_health()
        latency = round((time.perf_counter() - t0) * 1000, 2)
        reachable = bool(result.get("connected"))
        return GptSovitsTelemetry(
            status="reachable" if reachable else "unreachable",
            base_url=client.base_url,
            latency_ms=latency,
            error=result.get("error"),
        )
    except Exception as exc:
        latency = round((time.perf_counter() - t0) * 1000, 2)
        return GptSovitsTelemetry(
            status="unreachable",
            base_url=base_url,
            latency_ms=latency,
            error=f"{type(exc).__name__}: {exc}",
        )


def _scan_dir_sync(directory: Path) -> Tuple[int, float]:
    """Blocking recursive file count + size scan (runs in a worker thread)."""
    if not directory.exists() or not directory.is_dir():
        return 0, 0.0
    count = 0
    total_bytes = 0
    try:
        for p in directory.rglob("*"):
            try:
                if p.is_file():
                    count += 1
                    total_bytes += p.stat().st_size
            except OSError:
                continue
    except OSError:
        pass
    return count, round(total_bytes / (1024 * 1024), 2)


async def _get_dir_metrics_cached(directory: Path) -> Tuple[int, float]:
    """TTL-cached directory metrics computed off the event loop."""
    key = str(directory)
    now = time.monotonic()
    cached = _dir_metrics_cache.get(key)
    if cached is not None:
        stamp, value = cached
        if now - stamp < _DIR_METRICS_TTL_SECONDS:
            return value
    value = await asyncio.to_thread(_scan_dir_sync, directory)
    _dir_metrics_cache[key] = (now, value)
    return value


# Backward-compatible sync alias (kept for existing tooling/tests).
def _get_dir_metrics(directory: Path) -> Tuple[int, float]:
    """Synchronous directory metrics — blocking, prefer _get_dir_metrics_cached."""
    return _scan_dir_sync(directory)


def _get_process_memory_mb() -> Optional[float]:
    """Retrieves RSS memory usage in MB using psutil if available."""
    try:
        import psutil
        process = psutil.Process(os.getpid())
        return round(process.memory_info().rss / (1024 * 1024), 2)
    except Exception:
        return None


@router.get("/api/health", response_model=HealthResponse, summary="Basic Health Check")
async def health_check(request: Request):
    """
    Lightweight health check endpoint for automated liveness probing.
    Returns HTTP 200 immediately.
    """
    settings = get_settings()
    start_time = getattr(request.app.state, "start_time", time.time())
    uptime = round(time.time() - start_time, 2)
    return HealthResponse(
        status="ok",
        app=settings.app_name,
        version=settings.app_version,
        uptime_seconds=uptime,
    )


@router.get("/status", response_model=LegacyStatusResponse, summary="Legacy Status Endpoint")
async def legacy_status(request: Request):
    """
    Legacy compatibility endpoint.
    Performs quick reachability probe to GPT-SoVITS.
    """
    settings = get_settings()
    probe = await _probe_gpt_sovits(settings.gpt_sovits_base_url)
    return LegacyStatusResponse(
        status="ok",
        app=settings.app_name,
        version=settings.app_version,
        gpt_sovits=probe.status,
    )


@router.get(
    "/api/system/status",
    response_model=SystemStatusResponse,
    summary="Comprehensive System Diagnostics",
    dependencies=[Depends(require_auth)],
)
async def system_status(request: Request):
    """
    Deep diagnostic telemetry endpoint for Web Management Console.
    Inspects DB state, GPT-SoVITS latency, storage sizes, and memory usage.
    """
    settings = get_settings()
    start_time = getattr(request.app.state, "start_time", time.time())
    uptime = round(time.time() - start_time, 2)
    start_time_iso = getattr(
        request.app.state,
        "start_time_iso",
        datetime.fromtimestamp(start_time, tz=timezone.utc).isoformat(),
    )

    # 1-4 gathered in PARALLEL: the GPT-SoVITS probe (network-bound) overlaps
    # with the storage scans (thread-bound) so total latency = max, not sum.
    gpt_probe_task = asyncio.create_task(_probe_gpt_sovits(settings.gpt_sovits_base_url))
    audio_metrics_task = asyncio.create_task(_get_dir_metrics_cached(settings.audio_dir))
    data_metrics_task = asyncio.create_task(_get_dir_metrics_cached(settings.data_dir))
    memory_task = asyncio.create_task(asyncio.to_thread(_get_process_memory_mb))

    gpt_probe = await gpt_probe_task
    audio_count, audio_size_mb = await audio_metrics_task
    _, data_size_mb = await data_metrics_task

    # 2. Database Status Check
    db_exists = settings.db_path.exists()
    db_telemetry = DatabaseTelemetry(
        status="connected" if db_exists else "initializing",
        wal_mode=True,
        path=str(settings.db_path),
    )

    # 3. Storage Metrics
    storage_telemetry = StorageTelemetry(
        audio_files_count=audio_count,
        audio_dir_size_mb=audio_size_mb,
        data_dir_size_mb=data_size_mb,
    )

    # 4. Process Memory & Runtime Telemetry
    app_telemetry = AppTelemetry(
        name=settings.app_name,
        version=settings.app_version,
        uptime_seconds=uptime,
        start_time=start_time_iso,
        python_version=sys.version.split()[0],
        pid=os.getpid(),
        memory_usage_mb=await memory_task,
    )

    # 5. Telegram Status
    tg_running = False
    try:
        from galgame2voice.telegram_bot.bot import get_telegram_bot_manager
        tg_mgr = get_telegram_bot_manager(db_path=settings.db_path)
        tg_running = getattr(tg_mgr, "is_running", False)
    except Exception:
        tg_running = False

    async with get_db(settings.db_path) as conn:
        db_s = await crud.get_settings_raw(conn)
        has_token = bool(db_s and db_s.telegram_bot_token and db_s.telegram_bot_token.strip())

    tg_telemetry = TelegramTelemetry(
        enabled=tg_running or has_token or settings.telegram_enabled,
        status="running" if tg_running else ("disabled" if not has_token else "standby"),
    )

    overall_status = "healthy" if gpt_probe.status == "reachable" else "degraded"

    return SystemStatusResponse(
        status=overall_status,
        timestamp=datetime.now(timezone.utc).isoformat(),
        app=app_telemetry,
        database=db_telemetry,
        gpt_sovits=gpt_probe,
        storage=storage_telemetry,
        telegram=tg_telemetry,
    )
