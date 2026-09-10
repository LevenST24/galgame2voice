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
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from galgame2voice.config import get_settings
from galgame2voice.database.session import get_db
from galgame2voice.database import crud
from galgame2voice.security.auth import require_auth
from galgame2voice.utils.logger import sanitize_error_detail
from galgame2voice.utils.hardware import (
    detect_gpu_capability,
    get_system_memory_status,
)
from galgame2voice.utils.precision import read_precision_cache

router = APIRouter(tags=["Health & Diagnostics"])


async def get_effective_sovits_url() -> str:
    """Returns the hot-reloadable GPT-SoVITS URL from DB settings, falling
    back to the env default — the same source the runtime traffic uses."""
    try:
        async with get_db() as conn:
            db_settings = await crud.get_settings_raw(conn)
        if db_settings and getattr(db_settings, "gpt_sovits_url", ""):
            return db_settings.gpt_sovits_url
    except Exception:
        pass
    return get_settings().gpt_sovits_base_url

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


class HardwareTelemetry(BaseModel):
    """Host GPU and system memory diagnostic telemetry."""
    gpu_available: bool
    gpu_name: str
    fp32_forced: bool
    inference_precision: str = "FP32"
    configured_precision: str = "auto"
    system_memory_gb: Optional[float] = None
    system_memory_avail_gb: Optional[float] = None


class SystemStatusResponse(BaseModel):
    """Full comprehensive system diagnostic status response."""
    status: str  # "healthy" | "degraded"
    timestamp: str
    app: AppTelemetry
    database: DatabaseTelemetry
    gpt_sovits: GptSovitsTelemetry
    storage: StorageTelemetry
    telegram: TelegramTelemetry
    hardware: HardwareTelemetry


async def _probe_gpt_sovits(base_url: str) -> GptSovitsTelemetry:
    """
    Checks GPT-SoVITS reachability through the shared singleton client pool
    (GET / — api_v2 answers on the root path). 3s connect/read budget:
    long enough for a busy GPU to answer, short enough for a 5s poll.
    HTTP 200/400 counts as reachable; other codes / network errors do not.
    """
    t0 = time.perf_counter()
    target = (base_url or "").strip().rstrip("/")
    if not (target.startswith("http://") or target.startswith("https://")):
        return GptSovitsTelemetry(
            status="unreachable",
            base_url=base_url or "",
            latency_ms=0.0,
            error="Invalid base_url: scheme must be http or https",
        )

    try:
        from galgame2voice.services.gpt_sovits_client import get_gpt_sovits_client
        client = get_gpt_sovits_client()

        if target and target != client.base_url.rstrip("/"):
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
        raw_err = result.get("error")
        safe_err = sanitize_error_detail(raw_err) if raw_err else None
        return GptSovitsTelemetry(
            status="reachable" if reachable else "unreachable",
            base_url=client.base_url,
            latency_ms=latency,
            error=safe_err,
        )
    except Exception as exc:
        latency = round((time.perf_counter() - t0) * 1000, 2)
        safe_err = sanitize_error_detail(exc)
        return GptSovitsTelemetry(
            status="unreachable",
            base_url=base_url,
            latency_ms=latency,
            error=f"{type(exc).__name__}: {safe_err}",
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
    probe = await _probe_gpt_sovits(await get_effective_sovits_url())
    return LegacyStatusResponse(
        status="ok",
        app=settings.app_name,
        version=settings.app_version,
        gpt_sovits=probe.status,
    )


_GPU_METRICS_TTL_SECONDS = 60.0
_gpu_telemetry_cache: Optional[Tuple[float, Tuple[bool, str, bool]]] = None


def _get_gpu_telemetry_cached() -> Tuple[bool, str, bool]:
    """TTL-cached static GPU capability to prevent blocking subprocess spawning on frequent status polls."""
    global _gpu_telemetry_cache
    now = time.monotonic()
    if _gpu_telemetry_cache is not None:
        stamp, val = _gpu_telemetry_cache
        if now - stamp < _GPU_METRICS_TTL_SECONDS:
            return val
    gpu_avail, gpu_name, _ = detect_gpu_capability()
    val = (gpu_avail, gpu_name or "N/A")
    _gpu_telemetry_cache = (now, val)
    return val


def _engine_fp32_forced() -> bool:
    """True when the calibration store has verified this device needs FP32."""
    cached = read_precision_cache(get_settings().project_root)
    return bool(cached and cached.get("is_half") is False)


def _collect_hardware_telemetry_sync() -> HardwareTelemetry:
    """Collects GPU capability and host RAM telemetry synchronously."""
    total_ram, avail_ram = get_system_memory_status()
    gpu_avail, gpu_name = _get_gpu_telemetry_cached()
    project_root = get_settings().project_root
    cached = read_precision_cache(project_root)

    env_prec = os.environ.get("GPT_SOVITS_PRECISION", "").strip().lower()
    if env_prec in ("fp16", "half", "true", "1"):
        active_prec = "FP16"
        cfg_prec = "fp16"
    elif env_prec in ("fp32", "float32", "false", "0"):
        active_prec = "FP32"
        cfg_prec = "fp32"
    elif cached and "is_half" in cached:
        active_prec = "FP16" if cached["is_half"] else "FP32"
        cfg_prec = "fp16" if cached["is_half"] else "fp32"
    else:
        active_prec = "FP32" if _engine_fp32_forced() else "FP16"
        cfg_prec = "auto"

    return HardwareTelemetry(
        gpu_available=gpu_avail,
        gpu_name=gpu_name,
        fp32_forced=_engine_fp32_forced(),
        inference_precision=active_prec,
        configured_precision=cfg_prec,
        system_memory_gb=total_ram,
        system_memory_avail_gb=avail_ram,
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
    Inspects DB state, GPT-SoVITS latency, storage sizes, memory usage, and hardware telemetry.
    """
    settings = get_settings()
    start_time = getattr(request.app.state, "start_time", time.time())
    uptime = round(time.time() - start_time, 2)
    start_time_iso = getattr(
        request.app.state,
        "start_time_iso",
        datetime.fromtimestamp(start_time, tz=timezone.utc).isoformat(),
    )

    # 1-5 gathered in PARALLEL: the GPT-SoVITS probe (network-bound) overlaps
    # with storage scans, memory retrieval, and hardware telemetry (thread-bound) so total latency = max, not sum.
    gpt_probe_task = asyncio.create_task(_probe_gpt_sovits(await get_effective_sovits_url()))
    audio_metrics_task = asyncio.create_task(_get_dir_metrics_cached(settings.audio_dir))
    data_metrics_task = asyncio.create_task(_get_dir_metrics_cached(settings.data_dir))
    memory_task = asyncio.create_task(asyncio.to_thread(_get_process_memory_mb))
    hardware_task = asyncio.create_task(asyncio.to_thread(_collect_hardware_telemetry_sync))

    gpt_probe = await gpt_probe_task
    audio_count, audio_size_mb = await audio_metrics_task
    _, data_size_mb = await data_metrics_task
    hardware_telemetry = await hardware_task

    # 2. Database Status Check (Normalized Relative Path)
    db_exists = settings.db_path.exists()
    try:
        rel_db_path = settings.db_path.relative_to(settings.project_root).as_posix()
    except Exception:
        # Safe relative path fallback when testing with temp paths outside project_root
        rel_db_path = f"{settings.data_dir_name}/{settings.db_path.name}"

    db_telemetry = DatabaseTelemetry(
        status="connected" if db_exists else "initializing",
        wal_mode=True,
        path=rel_db_path,
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
        is_enabled = bool(db_s and getattr(db_s, "telegram_enabled", False))

    tg_telemetry = TelegramTelemetry(
        enabled=is_enabled,
        status="running" if tg_running else ("disabled" if not is_enabled else ("standby" if has_token else "unconfigured")),
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
        hardware=hardware_telemetry,
    )


@router.post(
    "/api/system/restart_sovits",
    summary="Restart GPT-SoVITS Engine Subprocess",
    dependencies=[Depends(require_auth)],
)
async def restart_sovits_endpoint():
    """
    Terminates the existing GPT-SoVITS process and restarts it with the
    latest precision configuration (FP16 / FP32).
    """
    settings = get_settings()
    sovits_dir_file = settings.project_root / "data" / "sovits_dir.txt"
    if not sovits_dir_file.exists():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="GPT-SoVITS 目录路径未记录 (data/sovits_dir.txt 不存在)，无法自动重启",
        )
    sovits_dir = Path(sovits_dir_file.read_text(encoding="utf-8").strip())
    if not sovits_dir.exists():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"GPT-SoVITS 目录不存在: {sovits_dir}",
        )

    from galgame2voice.utils.precision import resolve_initial_is_half
    is_half, source = resolve_initial_is_half(settings.project_root, sovits_dir)

    pid_file = settings.project_root / "gptsovits.pid"
    old_pid = None
    if pid_file.exists():
        try:
            old_pid = int(pid_file.read_text(encoding="utf-8").strip())
        except ValueError:
            pass

    # Terminate old process
    if old_pid:
        try:
            import psutil
            if psutil.pid_exists(old_pid):
                p = psutil.Process(old_pid)
                for child in p.children(recursive=True):
                    try:
                        child.kill()
                    except Exception:
                        pass
                p.kill()
                p.wait(timeout=3.0)
        except Exception:
            pass

    await asyncio.sleep(1.0)

    try:
        from scripts.run_server import _spawn_sovits_process
        proc = _spawn_sovits_process(sovits_dir, "127.0.0.1", 9880, is_half)
        pid_file.write_text(str(proc.pid), encoding="utf-8")
        prec_desc = "FP16 半精度" if is_half else "FP32 单精度"
        return {
            "status": "ok",
            "message": f"GPT-SoVITS 语音引擎已按 {prec_desc} 成功重启 (PID: {proc.pid})",
            "is_half": is_half,
            "pid": proc.pid,
            "source": source,
        }
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"重启 GPT-SoVITS 失败: {exc}",
        )
