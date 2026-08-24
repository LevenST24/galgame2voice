"""
Health check and system diagnostic router for galgame2voice.
Provides /api/health, /status, and /api/system/status endpoints.
"""

import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import httpx
from fastapi import APIRouter, Request, status
from pydantic import BaseModel, Field

from galgame2voice.config import get_settings

router = APIRouter(tags=["Health & Diagnostics"])


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
    Asynchronously checks GPT-SoVITS backend reachability with a 2.0s timeout.
    Does not raise exceptions on connection failure.
    """
    target_url = f"{base_url.rstrip('/')}/control"
    t0 = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=0.6, trust_env=False) as client:
            resp = await client.get(target_url)
            latency = round((time.perf_counter() - t0) * 1000, 2)
            # GPT-SoVITS /control returns 200 or 400 ("command is required") when active
            if resp.status_code in (200, 400):
                return GptSovitsTelemetry(
                    status="reachable",
                    base_url=base_url,
                    latency_ms=latency,
                    error=None,
                )
            else:
                return GptSovitsTelemetry(
                    status="unreachable",
                    base_url=base_url,
                    latency_ms=latency,
                    error=f"Unexpected status code: {resp.status_code}",
                )
    except Exception as exc:
        latency = round((time.perf_counter() - t0) * 1000, 2)
        return GptSovitsTelemetry(
            status="unreachable",
            base_url=base_url,
            latency_ms=latency,
            error=type(exc).__name__,
        )


def _get_dir_metrics(directory: Path) -> tuple[int, float]:
    """Calculates file count and total size in MB for a given directory."""
    if not directory.exists() or not directory.is_dir():
        return 0, 0.0
    count = 0
    total_bytes = 0
    try:
        for p in directory.rglob("*"):
            if p.is_file():
                count += 1
                total_bytes += p.stat().st_size
    except OSError:
        pass
    return count, round(total_bytes / (1024 * 1024), 2)


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
    Legacy Spring Boot compatibility endpoint.
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

    # 1. GPT-SoVITS Reachability Probe
    gpt_probe = await _probe_gpt_sovits(settings.gpt_sovits_base_url)

    # 2. Database Status Check
    db_exists = settings.db_path.exists()
    db_telemetry = DatabaseTelemetry(
        status="connected" if db_exists else "initializing",
        wal_mode=True,
        path=str(settings.db_path),
    )

    # 3. Storage Metrics
    audio_count, audio_size_mb = _get_dir_metrics(settings.audio_dir)
    _, data_size_mb = _get_dir_metrics(settings.data_dir)
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
        memory_usage_mb=_get_process_memory_mb(),
    )

    # 5. Telegram Status
    tg_telemetry = TelegramTelemetry(
        enabled=settings.telegram_enabled,
        status="running" if settings.telegram_enabled else "disabled",
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
