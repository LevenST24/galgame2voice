"""
Health check and system diagnostic router for galgame2voice.
Provides /api/health, /status, and /api/system/status endpoints.

All filesystem scans run in worker threads and are cached with a TTL so the
frontend's on-demand status requests never block the event loop.
"""

import asyncio
import logging
import os
import sys
import time
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from galgame2voice import __version__
from galgame2voice.services.engine_recovery import engine_failure_message
from galgame2voice.services.sovits_installation import find_engine_python
from galgame2voice.config import get_settings
from galgame2voice.database.session import get_db
from galgame2voice.database import crud
from galgame2voice.security.auth import require_auth
from galgame2voice.services.sovits_installation import (
    inspect_sovits_directory,
    read_sovits_directory,
    save_sovits_directory,
)
from galgame2voice.services.sovits_endpoint import (
    DEFAULT_SOVITS_BASE_URL,
    SovitsEndpoint,
    parse_sovits_endpoint,
    resolve_effective_sovits_endpoint,
)
from galgame2voice.utils.logger import sanitize_error_detail
from galgame2voice.utils.hardware import (
    detect_gpu_capability,
    get_system_memory_status,
)
from galgame2voice.utils.precision import (
    read_precision_cache,
    read_db_precision,
    resolve_initial_device_and_half,
    write_precision_cache,
    write_sovits_yaml_config,
)

logger = logging.getLogger("galgame2voice.routers.health")

router = APIRouter(tags=["Health & Diagnostics"])


async def get_effective_sovits_url() -> str:
    """Returns the unified effective GPT-SoVITS URL.

    Thin wrapper over the unified resolver (explicit process env > SQLite >
    .env/Settings > built-in default) — the same source the runtime traffic
    uses. Never raises: falls back to the Settings/default layer on error.
    """
    try:
        return (await resolve_effective_sovits_endpoint()).base_url
    except Exception as exc:
        logger.debug("Failed resolving effective sovits url: %s", exc)
    try:
        return parse_sovits_endpoint(
            get_settings().gpt_sovits_base_url, source="dotenv"
        ).base_url
    except Exception:
        return DEFAULT_SOVITS_BASE_URL


async def _resolve_sovits_endpoint_best_effort() -> SovitsEndpoint | None:
    """Resolves the effective endpoint, returning None (never raising) on failure."""
    try:
        return await resolve_effective_sovits_endpoint()
    except Exception as exc:
        logger.debug("Failed resolving GPT-SoVITS endpoint: %s", exc)
        return None

# Directory metrics are cached: the settings console re-requests status on demand,
# and scanning thousands of cache files each time would freeze the event loop.
_DIR_METRICS_TTL_SECONDS = 15.0
_dir_metrics_cache: dict[str, tuple[float, tuple[int, float]]] = {}


class HealthResponse(BaseModel):
    """Lightweight health check response."""
    status: str = Field(default="ok", json_schema_extra={"example": "ok"})
    app: str = Field(default="galgame2voice", json_schema_extra={"example": "galgame2voice"})
    version: str = Field(default=__version__, json_schema_extra={"example": __version__})
    uptime_seconds: float = Field(..., json_schema_extra={"example": 120.5})


class LegacyStatusResponse(BaseModel):
    """Legacy endpoint compatibility response."""
    status: str = Field(default="ok", json_schema_extra={"example": "ok"})
    app: str = Field(default="galgame2voice", json_schema_extra={"example": "galgame2voice"})
    version: str = Field(default=__version__, json_schema_extra={"example": __version__})
    gpt_sovits: str = Field(default="reachable", json_schema_extra={"example": "reachable"})


class AppTelemetry(BaseModel):
    """Application level telemetry information."""
    name: str = "galgame2voice"
    version: str = __version__
    uptime_seconds: float
    start_time: str
    python_version: str
    pid: int
    memory_usage_mb: float | None = None


class DatabaseTelemetry(BaseModel):
    """Database connectivity and state telemetry."""
    status: str
    wal_mode: bool
    path: str


class GptSovitsTelemetry(BaseModel):
    """GPT-SoVITS backend reachability telemetry."""
    status: str  # "reachable" | "unreachable"
    base_url: str
    latency_ms: float | None = None
    error: str | None = None
    # Where the probed base_url came from: "env" | "db" | "dotenv" | "default".
    # Optional for backward compatibility with older serialized payloads.
    source: str | None = None


class StorageTelemetry(BaseModel):
    """Storage metrics for audio and data directories."""
    audio_files_count: int
    audio_dir_size_mb: float
    data_dir_size_mb: float


class TelegramTelemetry(BaseModel):
    """Telegram bot integration status."""
    enabled: bool
    status: str  # "disabled" | "standby" | "unconfigured" | "running"


class HardwareTelemetry(BaseModel):
    """Host GPU and system memory diagnostic telemetry."""
    gpu_available: bool
    gpu_name: str
    fp32_forced: bool
    inference_precision: str = "FP32"
    configured_precision: str = "auto"
    system_memory_gb: float | None = None
    system_memory_avail_gb: float | None = None


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


async def _probe_custom_gpt_sovits_target(target: str, base_url: str, t0: float) -> GptSovitsTelemetry:
    """
    One-shot probe against an explicitly different URL.
    Connect budget 1s: healthy local engines connect in <50ms;
    some VPN/TUN stacks delay loopback refusals to ~2s, so a tight
    budget converts 'engine down' into a fast unreachable verdict.
    """
    timeout = httpx.Timeout(connect=1.0, read=2.5, write=2.5, pool=2.5)
    async with httpx.AsyncClient(trust_env=False, timeout=timeout) as one_shot:
        try:
            resp = await one_shot.get(f"{target}/control")
        except Exception:
            resp = await one_shot.get(f"{target}/")
        latency = round((time.perf_counter() - t0) * 1000, 2)
        if resp.status_code in (status.HTTP_200_OK, status.HTTP_400_BAD_REQUEST):
            return GptSovitsTelemetry(
                status="reachable", base_url=base_url, latency_ms=latency, error=None)
        return GptSovitsTelemetry(
            status="unreachable", base_url=base_url, latency_ms=latency,
            error=f"Unexpected status code: {resp.status_code}")


async def _probe_gpt_sovits(base_url: str) -> GptSovitsTelemetry:
    """
    Checks GPT-SoVITS reachability through the shared singleton client pool
    (probing GET /control with fallback to /). Fast health probe budget:
    long enough for a busy GPU to answer, short enough for a 5s poll.
    HTTP 200/400 counts as reachable; other codes / network errors do not.
    """
    t0 = time.perf_counter()
    target = (base_url or "").strip().rstrip("/")
    if not target.startswith(("http://", "https://")):
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
            return await _probe_custom_gpt_sovits_target(target, base_url, t0)

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


def _scan_dir_sync(directory: Path) -> tuple[int, float]:
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


async def _get_dir_metrics_cached(directory: Path) -> tuple[int, float]:
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
def _get_dir_metrics(directory: Path) -> tuple[int, float]:
    """Synchronous directory metrics — blocking, prefer _get_dir_metrics_cached."""
    return _scan_dir_sync(directory)


def _get_process_memory_mb() -> float | None:
    """Retrieves RSS memory usage in MB using psutil if available."""
    try:
        import psutil
        process = psutil.Process(os.getpid())
        return round(process.memory_info().rss / (1024 * 1024), 2)
    except Exception:
        return None


@router.get("/api/health", response_model=HealthResponse, summary="Basic Health Check")
async def health_check(request: Request) -> HealthResponse:
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
async def legacy_status(request: Request) -> LegacyStatusResponse:
    """
    Legacy compatibility endpoint.
    Performs quick reachability probe to GPT-SoVITS.
    """
    settings = get_settings()
    sovits_endpoint = await _resolve_sovits_endpoint_best_effort()
    probe = await _probe_gpt_sovits(
        sovits_endpoint.base_url if sovits_endpoint else await get_effective_sovits_url()
    )
    if sovits_endpoint is not None:
        probe.source = sovits_endpoint.source
    return LegacyStatusResponse(
        status="ok",
        app=settings.app_name,
        version=settings.app_version,
        gpt_sovits=probe.status,
    )


_GPU_METRICS_TTL_SECONDS = 60.0
_gpu_telemetry_cache: tuple[float, tuple[bool, str]] | None = None


def _get_gpu_telemetry_cached() -> tuple[bool, str]:
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
    sovits_dir = read_sovits_directory(project_root) or project_root

    device, is_half, _ = resolve_initial_device_and_half(project_root, sovits_dir)
    if device == "cpu":
        active_prec = "CPU"
    else:
        active_prec = "FP16" if is_half else "FP32"

    cfg_prec = read_db_precision(project_root) or "auto"
    env_prec = os.environ.get("GPT_SOVITS_PRECISION", "").strip().lower()
    if env_prec in ("fp16", "half", "true", "1"):
        cfg_prec = "fp16"
    elif env_prec in ("fp32", "float32", "false", "0"):
        cfg_prec = "fp32"

    return HardwareTelemetry(
        gpu_available=gpu_avail,
        gpu_name=gpu_name,
        fp32_forced=_engine_fp32_forced(),
        inference_precision=active_prec,
        configured_precision=cfg_prec,
        system_memory_gb=total_ram,
        system_memory_avail_gb=avail_ram,
    )


def _resolve_telegram_status(is_running: bool, is_enabled: bool, has_token: bool) -> str:
    """Computes categorical telegram telemetry status string without nested conditionals."""
    if is_running:
        return "running"
    if not is_enabled:
        return "disabled"
    if has_token:
        return "standby"
    return "unconfigured"


def _resolve_rel_db_path(db_path: Path, project_root: Path, data_dir_name: str) -> str:
    """Returns normalized relative database path with safe fallback for out-of-tree test databases."""
    try:
        return db_path.relative_to(project_root).as_posix()
    except Exception:
        return f"{data_dir_name}/{db_path.name}"


async def _collect_telegram_telemetry(db_path: Path) -> TelegramTelemetry:
    """Inspects telegram bot runtime manager and database configuration."""
    tg_running = False
    try:
        from galgame2voice.telegram_bot.bot import get_telegram_bot_manager
        tg_mgr = get_telegram_bot_manager(db_path=db_path)
        tg_running = getattr(tg_mgr, "is_running", False)
    except Exception:
        tg_running = False

    async with get_db(db_path) as conn:
        db_s = await crud.get_settings_raw(conn)
        has_token = bool(db_s.telegram_bot_token and db_s.telegram_bot_token.strip())
        is_enabled = bool(getattr(db_s, "telegram_enabled", False))

    return TelegramTelemetry(
        enabled=is_enabled,
        status=_resolve_telegram_status(tg_running, is_enabled, has_token),
    )


@router.get(
    "/api/system/status",
    response_model=SystemStatusResponse,
    summary="Comprehensive System Diagnostics",
    dependencies=[Depends(require_auth)],
)
async def system_status(request: Request) -> SystemStatusResponse:
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
    sovits_endpoint = await _resolve_sovits_endpoint_best_effort()
    gpt_probe_task = asyncio.create_task(
        _probe_gpt_sovits(
            sovits_endpoint.base_url if sovits_endpoint else await get_effective_sovits_url()
        )
    )
    audio_metrics_task = asyncio.create_task(_get_dir_metrics_cached(settings.audio_dir))
    data_metrics_task = asyncio.create_task(_get_dir_metrics_cached(settings.data_dir))
    memory_task = asyncio.create_task(asyncio.to_thread(_get_process_memory_mb))
    hardware_task = asyncio.create_task(asyncio.to_thread(_collect_hardware_telemetry_sync))

    gpt_probe = await gpt_probe_task
    if sovits_endpoint is not None:
        gpt_probe.source = sovits_endpoint.source
    audio_count, audio_size_mb = await audio_metrics_task
    _, data_size_mb = await data_metrics_task
    hardware_telemetry = await hardware_task

    # 2. Database Status Check (Normalized Relative Path)
    db_telemetry = DatabaseTelemetry(
        status="connected" if settings.db_path.exists() else "initializing",
        wal_mode=True,
        path=_resolve_rel_db_path(settings.db_path, settings.project_root, settings.data_dir_name),
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
    tg_telemetry = await _collect_telegram_telemetry(settings.db_path)

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


class RestartSovitsPayload(BaseModel):
    """Optional payload for restarting GPT-SoVITS subprocess with explicit precision."""
    precision: str | None = Field(
        default=None,
        description="Optional precision override: 'fp16', 'fp32', 'cpu', or 'auto'. If omitted, uses current setting.",
    )


async def _update_inference_precision_setting(precision_val: str) -> None:
    try:
        from galgame2voice.database.models import SettingsUpdate
        async with get_db() as conn:
            await crud.update_settings(conn, SettingsUpdate(inference_precision=precision_val))
    except Exception as exc:
        logger.debug("Failed updating inference_precision setting: %s", exc)


async def _apply_sovits_precision_config(
    project_root: Path,
    sovits_dir: Path,
    req_prec: str | None,
) -> tuple[str, bool, str]:
    """Applies target precision configuration to cache, YAML, and database settings.
    Returns (device, is_half, source)."""
    precision_map = {
        "cpu": ("cpu", False, "cpu"),
        "fp32": ("cuda", False, "fp32"),
        "float32": ("cuda", False, "fp32"),
        "fp16": ("cuda", True, "fp16"),
        "half": ("cuda", True, "fp16"),
    }
    if req_prec in precision_map:
        device, is_half, target_setting = precision_map[req_prec]
        source = "request"
        await asyncio.to_thread(write_precision_cache, project_root, str(sovits_dir), is_half=is_half, device=device)
        await _update_inference_precision_setting(target_setting)
    elif req_prec == "auto":
        cache_file = project_root / "data" / "precision.json"
        await asyncio.to_thread(cache_file.unlink, missing_ok=True)
        await _update_inference_precision_setting("auto")
        device, is_half, source = await asyncio.to_thread(resolve_initial_device_and_half, project_root, sovits_dir)
    else:
        device, is_half, source = await asyncio.to_thread(resolve_initial_device_and_half, project_root, sovits_dir)

    await asyncio.to_thread(write_sovits_yaml_config, sovits_dir, is_half=is_half, device=device)
    return device, is_half, source


class SovitsDirectoryPayload(BaseModel):
    directory: str = Field(min_length=1, max_length=4096)


@router.get("/api/system/sovits-directory", dependencies=[Depends(require_auth)])
async def get_sovits_directory_endpoint() -> dict:
    return await asyncio.to_thread(inspect_sovits_directory, get_settings().project_root)


@router.put("/api/system/sovits-directory", dependencies=[Depends(require_auth)])
async def save_sovits_directory_endpoint(payload: SovitsDirectoryPayload) -> dict:
    try:
        result = await asyncio.to_thread(save_sovits_directory, get_settings().project_root, payload.directory)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        logger.warning("Failed saving engine directory: %s", exc)
        raise HTTPException(status_code=500, detail="无法保存引擎目录，请确认程序目录有写入权限。") from exc
    from galgame2voice.routers.voice import _scan_cache
    generation = _scan_cache.get("generation", 0) + 1
    _scan_cache.clear()
    _scan_cache["generation"] = generation
    return result


def _resolve_sovits_directory(project_root: Path) -> Path:
    """Resolves and validates GPT-SoVITS directory from data/sovits_dir.txt."""
    sovits_dir_file = project_root / "data" / "sovits_dir.txt"
    if not sovits_dir_file.exists():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="尚未选择语音引擎目录。请在「全局设置 → 语音与推理」点击「选择文件夹」，选择完整解压的引擎包并保存。",
        )
    try:
        sovits_dir = read_sovits_directory(project_root)
    except (OSError, ValueError, UnicodeError) as exc:
        raise HTTPException(status_code=400, detail="引擎目录配置无法读取。请在「语音与推理」重新选择文件夹并保存。") from exc
    if sovits_dir is None or not sovits_dir.is_dir():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="保存的引擎目录已移动或删除。请在「语音与推理」重新选择实际存在的引擎文件夹并保存。",
        )
    return sovits_dir


_engine_restart_lock = threading.Lock()


@router.get("/api/system/engine-status", dependencies=[Depends(require_auth)])
async def engine_status_endpoint() -> dict[str, Any]:
    try:
        endpoint = await resolve_effective_sovits_endpoint()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="语音引擎服务地址无效。请在「语音与推理」检查并保存服务地址。") from exc
    if _engine_restart_lock.locked():
        return {"state": "starting", "ready": False, "message": "引擎正在重启，请等待当前操作完成。"}
    from scripts import run_server
    proc = run_server._SPAWNED_SOVITS_PROC
    telemetry = await _probe_gpt_sovits(endpoint.base_url)
    if telemetry.status == "reachable":
        return {"state": "ready", "ready": True, "message": "语音引擎已就绪。可在会话设置中选择音色。"}
    if endpoint.is_local and proc is not None and proc.poll() is not None:
        message = await asyncio.to_thread(engine_failure_message, get_settings().project_root / "logs" / "gpt_sovits.log")
        return {"state": "failed", "ready": False, "message": message}
    if endpoint.is_local and proc is not None:
        return {"state": "starting", "ready": False, "message": "引擎正在加载模型，请稍候；文字聊天可继续使用。"}
    return {"state": "unavailable", "ready": False,
            "message": "语音引擎尚未就绪。请在「语音与推理」选择引擎目录后启动，或检查已配置的远端引擎地址。文字聊天可继续使用。"}


@router.post(
    "/api/system/restart_sovits",
    summary="Restart GPT-SoVITS Engine Subprocess",
    dependencies=[Depends(require_auth)],
)
async def restart_sovits_endpoint(payload: RestartSovitsPayload | None = None) -> dict[str, Any]:
    """
    Terminates the existing GPT-SoVITS process and restarts it with the
    latest precision configuration (FP16 / FP32).

    Only a local (loopback) effective endpoint may be restarted by this host;
    a remote endpoint returns 409 and is never touched.
    """
    try:
        endpoint = await resolve_effective_sovits_endpoint()
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"GPT-SoVITS 地址配置无效: {exc}",
        ) from exc
    if not endpoint.is_local:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "REMOTE_SOVITS_NOT_MANAGED",
                "base_url": endpoint.base_url,
                "message": (
                    "当前 GPT-SoVITS 地址为远端服务 ("
                    f"{endpoint.base_url}"
                    ")，本机不会管理（拉起/重启）远端进程；请在远端主机上自行重启引擎。"
                ),
            },
        )

    if not _engine_restart_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="语音引擎正在启动或重启，请等待当前操作完成，勿重复点击。")
    task = asyncio.create_task(_restart_local_engine(endpoint, payload))

    def release(completed):
        _engine_restart_lock.release()
        if not completed.cancelled():
            completed.exception()  # A disconnected browser must not leave an unobserved failure.

    task.add_done_callback(release)
    # Disconnecting a browser must not interrupt process ownership halfway through.
    try:
        return await asyncio.shield(task)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Engine restart preparation failed")
        raise HTTPException(status_code=500, detail="无法准备语音引擎。请确认程序和引擎目录可写，在「语音与推理」重新选择完整引擎包后再试。") from exc


async def _restart_local_engine(endpoint: SovitsEndpoint, payload: RestartSovitsPayload | None) -> dict[str, Any]:
    settings = get_settings()
    sovits_dir = await asyncio.to_thread(_resolve_sovits_directory, settings.project_root)
    if not await asyncio.to_thread((sovits_dir / "api_v2.py").is_file):
        raise HTTPException(status_code=400, detail="所选文件夹缺少 api_v2.py。请完整解压引擎包，在「语音与推理」重新选择引擎文件夹。")
    if getattr(sys, "frozen", False) and await asyncio.to_thread(find_engine_python, sovits_dir) is None:
        raise HTTPException(status_code=400, detail="所选引擎缺少自己的运行环境。请使用包含 runtime 的完整集成包，重新选择文件夹后启动。")

    from scripts import run_server
    owned = run_server._SPAWNED_SOVITS_PROC
    live_owned = owned is not None and owned.poll() is None
    if not live_owned and await asyncio.to_thread(run_server.is_port_in_use, endpoint.port, endpoint.host):
        raise HTTPException(status_code=409, detail="语音引擎已在其他窗口运行，可直接使用。若需重启，请先关闭该引擎窗口，再点击「重启引擎」。")
    # Determine precision and device target only after validation and ownership checks.
    req_prec = str(payload.precision).strip().lower() if (payload and payload.precision) else None
    device, is_half, source = await _apply_sovits_precision_config(settings.project_root, sovits_dir, req_prec)

    if live_owned:
        await asyncio.to_thread(run_server.terminate_process_tree, owned.pid)
        await asyncio.to_thread(owned.wait, timeout=3)

    # Bounded wait for the old process to release the port (replaces the old
    # fixed sleep): poll up to ~5s at 0.25s intervals, off the event loop.
    from scripts.run_server import is_port_in_use

    port_released = False
    for _ in range(20):
        if not await asyncio.to_thread(is_port_in_use, endpoint.port, endpoint.host):
            port_released = True
            break
        await asyncio.sleep(0.25)
    if not port_released:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "SOVITS_PORT_STILL_IN_USE",
                "base_url": endpoint.base_url,
                "message": (
                    f"旧 GPT-SoVITS 进程未在 5 秒内释放端口 {endpoint.port}，"
                    "请等待片刻再试；若引擎在其他窗口运行，请先关闭该窗口。"
                ),
            },
        )

    try:
        from scripts.run_server import _spawn_sovits_process
        proc = await asyncio.to_thread(_spawn_sovits_process, sovits_dir, endpoint.host, endpoint.port, is_half, device=device)
        await asyncio.sleep(0.1)
        if proc.poll() is not None:
            message = await asyncio.to_thread(engine_failure_message, settings.project_root / "logs" / "gpt_sovits.log")
            raise HTTPException(status_code=502, detail=message)
        if device == "cpu":
            prec_desc = "CPU 稳定模式"
            prec_val = "CPU"
        else:
            prec_desc = "FP16 半精度" if is_half else "FP32 单精度"
            prec_val = "FP16" if is_half else "FP32"
        return {
            "status": "starting",
            "ready": False,
            "message": f"语音引擎已按 {prec_desc} 开始加载，请稍候；就绪前仍可进行文字聊天。",
            "is_half": is_half,
            "device": device,
            "precision": prec_val,
            "pid": proc.pid,
            "source": source,
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Engine restart failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="语音引擎启动未完成。请确认引擎已完整解压且目录可写，在「语音与推理」重新选择文件夹后再试。详细原因已记录在程序日志。",
        ) from exc
