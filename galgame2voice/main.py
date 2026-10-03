"""
FastAPI Main Application Entry Point for galgame2voice.
Manages application lifespan, CORS, static routing, and router registration.
"""

import asyncio
import logging
import mimetypes
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import Depends, FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import MutableHeaders
from starlette.types import Receive, Scope, Send

from galgame2voice.config import get_settings
from galgame2voice.database import crud
from galgame2voice.database.session import get_db, init_db
from galgame2voice.routers import chat, config, health, voice, memory, affection, characters, metrics, system
from galgame2voice.security.auth import require_auth
from galgame2voice.security.rate_limit import RateLimitMiddleware
from galgame2voice.services.gpt_sovits_client import get_gpt_sovits_client, close_gpt_sovits_client
from galgame2voice.services.audio_cleaner import (
    CACHE_RETENTION_DAYS as _CACHE_RETENTION_DAYS,
    _cache_scan_and_clean,
    _scan_and_clean,
    _audio_cleanup_loop,
    AudioCleanerService,
)
from galgame2voice.utils.logger import setup_logger
from galgame2voice.utils.path_guard import resolve_existing_audio_path

__all__ = [
    "app",
    "create_app",
    "lifespan",
    "run",
    "AudioStaticFiles",
    "AudioCleanerService",
    "_audio_cleanup_loop",
    "_scan_and_clean",
    "_cache_scan_and_clean",
    "_CACHE_RETENTION_DAYS",
]

# Windows 注册表常把 .js 映射为 text/plain，ES module 会被浏览器 Strict MIME 拒载
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/javascript", ".mjs")

# Starlette 新旧版本状态码名称兼容：旧版只有 HTTP_422_UNPROCESSABLE_ENTITY，
# 代码中多处使用新名 CONTENT；缺失时补齐为同一数值，否则校验路径会 500。
if not hasattr(status, "HTTP_422_UNPROCESSABLE_CONTENT"):
    status.HTTP_422_UNPROCESSABLE_CONTENT = 422


logger = logging.getLogger("galgame2voice.main")


class HostValidationMiddleware:
    """
    Rejects /api/ requests whose Host header is not a loopback host.

    A malicious web page can DNS-rebind its own domain to 127.0.0.1 and call this
    API with the victim's browser; the browser still sends the attacker's domain
    as the Host header, so an exact allowlist breaks the attack. Only enabled for
    the local zero-config mode (see create_app) because TLS-terminating reverse
    proxies rewrite or forward their own Host value.
    """

    ALLOWED_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

    def __init__(self, app: Any) -> None:
        self.app = app

    @staticmethod
    def extract_hostname(raw_host: str) -> str:
        """Strips the optional port from a Host header value, keeping IPv6 brackets semantics."""
        host = (raw_host or "").strip()
        if not host:
            return ""
        if host.startswith("["):
            # IPv6 literal, e.g. "[::1]:8080" -> "::1"
            return host[1:].split("]", 1)[0].lower()
        if host.count(":") == 1:
            # "host:port" -> "host"; a bare IPv6 without brackets has 2+ colons, so it is not
            # port-split here and falls through to the verbatim comparison below (a bracket-less
            # "::1" therefore still matches the "::1" entry in ALLOWED_LOOPBACK_HOSTS).
            return host.split(":", 1)[0].lower()
        return host.lower()

    def _allowed_hostnames(self) -> set:
        allowed = set(self.ALLOWED_LOOPBACK_HOSTS)
        configured = str(get_settings().host).strip().lower()
        if configured:
            allowed.add(configured)
        return allowed

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and str(scope.get("path", "")).startswith("/api/"):
            headers = {k.lower(): v for k, v in scope.get("headers") or []}
            raw_host = headers.get(b"host", b"").decode("latin-1")
            if self.extract_hostname(raw_host) not in self._allowed_hostnames():
                logger.warning("Rejected /api request with unexpected Host header: %r", raw_host)
                await send({
                    "type": "http.response.start",
                    "status": 403,
                    "headers": [(b"content-type", b"application/json")],
                })
                await send({
                    "type": "http.response.body",
                    "body": b'{"detail": "Forbidden: unexpected Host header"}',
                })
                return
        await self.app(scope, receive, send)


def _init_logging_and_safety(settings) -> None:
    """Initializes logging with secret masking and runs fail-fast network exposure checks."""
    setup_logger(
        log_level=settings.log_level,
        logs_dir=settings.logs_dir if settings.log_to_file else None,
        log_to_file=settings.log_to_file,
    )
    logger.info("Initializing %s v%s...", settings.app_name, settings.app_version)

    is_loopback = str(settings.host).strip().lower() in ("127.0.0.1", "localhost", "::1")
    from galgame2voice.security.auth import is_auth_disabled

    if not is_loopback and is_auth_disabled():
        err_msg = (
            f"FATAL: Application is configured to listen on '{settings.host}' with auth_disabled=True. "
            "Network exposure strictly requires console token authentication enabled. "
            "Set GALGAME2VOICE_AUTH_DISABLED=0 in your environment or docker-compose.yml to proceed."
        )
        logger.critical(err_msg)
        raise RuntimeError(err_msg)


def _init_directories(settings) -> None:
    """Ensures required application runtime directories exist."""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.audio_dir.mkdir(parents=True, exist_ok=True)
    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    settings.characters_dir.mkdir(parents=True, exist_ok=True)
    logger.info(
        "Verified directories: data=%s, audio=%s, logs=%s, characters=%s",
        settings.data_dir,
        settings.audio_dir,
        settings.logs_dir,
        settings.characters_dir,
    )


async def _init_database_and_characters(settings) -> None:
    """Initializes SQLite schema, auto-heals voice profiles, and syncs character packages."""
    await init_db(settings.db_path)
    logger.info("Database initialized successfully at %s", settings.db_path)

    try:
        async with get_db(settings.db_path) as conn:
            healed = await crud.auto_heal_voice_profiles(conn)
            if healed > 0:
                logger.info("Auto-healed %d voice profile(s) with missing/invalid reference audio paths", healed)
    except Exception as exc:
        logger.debug("Startup auto-heal check skipped: %s", exc)

    try:
        from galgame2voice.services.character_manager import get_character_manager
        char_mgr = get_character_manager()
        discovered = char_mgr.discover_characters()
        logger.info("Discovered %d character package(s)", len(discovered))
        async with get_db(settings.db_path) as conn:
            synced = await char_mgr.sync_with_db(conn)
            if synced > 0:
                logger.info("Synced %d character package(s) with voice profiles", synced)
    except Exception as exc:
        logger.debug("Startup character packages sync skipped: %s", exc)


async def _init_gpt_sovits_client(settings) -> None:
    """Initializes shared GPT-SoVITS client, pre-seeds active profile, and triggers background warm-up."""
    try:
        from galgame2voice.services.voice_manager import get_voice_manager

        sovits_url = None
        try:
            async with get_db(settings.db_path) as conn:
                db_settings = await crud.get_settings_raw(conn)
                if getattr(db_settings, "gpt_sovits_url", None):
                    sovits_url = db_settings.gpt_sovits_url
        except Exception as exc:
            logger.warning("Could not read gpt_sovits_url from DB: %s", exc)

        client = get_gpt_sovits_client()
        if sovits_url and sovits_url.rstrip("/") != client.base_url:
            await client.set_base_url(sovits_url)

        # Pre-seed active voice profile from DB so frontend's initial switch is instantaneous
        try:
            vm = get_voice_manager()
            async with get_db(settings.db_path) as conn:
                default_profile = await crud.get_active_voice_profile(conn)
                if default_profile:
                    vm.active_profile = default_profile
                    if not client.current_gpt_weights:
                        client.current_gpt_weights = default_profile.gpt_weights_path
                    if not client.current_sovits_weights:
                        client.current_sovits_weights = default_profile.sovits_weights_path
                    if not client.current_refer_audio:
                        client.current_refer_audio = str(
                            resolve_existing_audio_path(default_profile.ref_audio_path)
                            or default_profile.ref_audio_path
                        )
                    if not client.current_refer_text:
                        client.current_refer_text = default_profile.prompt_text
                    if not client.current_refer_language:
                        client.current_refer_language = default_profile.prompt_lang
        except Exception as exc:
            logger.debug("Could not pre-seed active voice profile on startup: %s", exc)

        # Trigger non-blocking background warm-up of default voice profile
        try:
            vm = get_voice_manager()
            vm._spawn_background(vm.warmup_current_profile())
        except Exception as warmup_err:
            logger.debug("Could not trigger startup voice profile warm-up: %s", warmup_err)

        logger.info("GPT-SoVITS client initialized (endpoint: %s)", client.base_url)
    except Exception as exc:
        logger.error(
            "Failed to initialize GPT-SoVITS client: %s (如引擎在别处运行，请配置 "
            "GPT_SOVITS_BASE_URL 环境变量，例如 http://127.0.0.1:9880)",
            exc,
            exc_info=True,
        )


def _start_telegram_bg(settings) -> asyncio.Task | None:
    """Starts Telegram Bot polling in a background task if enabled in DB."""
    try:
        from galgame2voice.telegram_bot.bot import get_telegram_bot_manager
        tg_manager = get_telegram_bot_manager(db_path=settings.db_path)

        async def _run():
            try:
                async with get_db(settings.db_path) as conn:
                    db_settings = await crud.get_settings_raw(conn)
                if not getattr(db_settings, "telegram_enabled", False):
                    return
                tg_started = await tg_manager.start()
                if tg_started:
                    logger.info("Telegram Bot background polling started successfully.")
            except Exception as exc:
                logger.warning("Telegram Bot auto-start on boot skipped or failed: %s", exc)

        return asyncio.create_task(_run())
    except Exception:
        return None


async def _drain_active_services() -> None:
    """Drains background tasks from ChatService, SessionManager, and caches before WAL checkpoint."""
    try:
        from galgame2voice.routers import chat as chat_router_mod
        active_svcs = {
            getattr(chat_router_mod, "_chat_service", None),
            getattr(chat_router_mod, "_explicit_chat_service", None),
        }
        for chat_svc in active_svcs:
            if chat_svc is not None:
                if hasattr(chat_svc, "aclose"):
                    await chat_svc.aclose()
                if (
                    hasattr(chat_svc, "tts_service")
                    and hasattr(chat_svc.tts_service, "cache_manager")
                    and hasattr(chat_svc.tts_service.cache_manager, "aclose")
                ):
                    await chat_svc.tts_service.cache_manager.aclose()
                if (
                    hasattr(chat_svc, "session_manager")
                    and hasattr(chat_svc.session_manager, "aclose")
                ):
                    await chat_svc.session_manager.aclose()
        import galgame2voice.services.tts_cache_manager as tts_cache_mod
        cache_mgr = getattr(tts_cache_mod, "_tts_cache_manager_instance", None)
        if cache_mgr is not None and hasattr(cache_mgr, "aclose"):
            await cache_mgr.aclose()
        import galgame2voice.services.voice_manager as vm_mod
        vm_inst = getattr(vm_mod, "_global_voice_manager", None)
        if vm_inst is not None and hasattr(vm_inst, "aclose"):
            await vm_inst.aclose()

        # Gracefully shut down default thread pool executor before WAL checkpoint
        loop = asyncio.get_running_loop()
        if hasattr(loop, "shutdown_default_executor"):
            await loop.shutdown_default_executor()
            if hasattr(loop, "_executor_shutdown_called"):
                loop._executor_shutdown_called = False
            if hasattr(loop, "_default_executor"):
                loop._default_executor = None
    except Exception as exc:
        logger.debug("Error draining background tasks on shutdown: %s", exc)


async def _shutdown_services(settings, cleanup_task: asyncio.Task, tg_startup_task: asyncio.Task | None) -> None:
    """Gracefully drains background tasks, closes connections, and checkpoints SQLite WAL."""
    cleanup_task.cancel()
    try:
        await cleanup_task
    except asyncio.CancelledError:
        pass

    # Stop Telegram Bot
    if tg_startup_task and not tg_startup_task.done():
        tg_startup_task.cancel()
        try:
            await tg_startup_task
        except asyncio.CancelledError:
            pass
    try:
        from galgame2voice.telegram_bot.bot import get_telegram_bot_manager
        await get_telegram_bot_manager().stop()
    except Exception as exc:
        logger.debug("Error stopping Telegram Bot: %s", exc)

    # Release the shared GPT-SoVITS connection pool.
    try:
        await close_gpt_sovits_client()
    except Exception as exc:
        logger.debug("Error closing GPT-SoVITS client: %s", exc)

    # Drain background tasks from ChatService, SessionManager, and caches before WAL checkpoint
    await _drain_active_services()

    # Safe SQLite WAL truncation checkpoint
    try:
        async with get_db(settings.db_path) as conn:
            await conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
    except Exception as exc:
        logger.debug("WAL checkpoint on shutdown: %s", exc)

    logger.info("Shutting down %s...", settings.app_name)
    logger.info("Graceful shutdown complete.")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application lifespan context manager.
    Handles startup directory creation, DB initialization, and graceful shutdown.
    """
    settings = get_settings()

    # --- STARTUP PHASE ---
    app.state.start_time = time.time()
    app.state.start_time_iso = datetime.now(timezone.utc).isoformat()

    _init_logging_and_safety(settings)
    _init_directories(settings)
    await _init_database_and_characters(settings)
    await _init_gpt_sovits_client(settings)

    cleanup_task = asyncio.create_task(
        _audio_cleanup_loop(
            audio_dir=settings.audio_dir,
            interval_seconds=settings.audio_cleanup_interval_seconds,
        )
    )
    tg_startup_task = _start_telegram_bg(settings)

    logger.info(
        "Service startup complete. Listening on http://%s:%d",
        settings.host,
        settings.port,
    )

    yield  # Application serving requests

    # --- SHUTDOWN PHASE ---
    await _shutdown_services(settings, cleanup_task, tg_startup_task)


class AudioStaticFiles(StaticFiles):
    """
    Enhanced StaticFiles handler for audio files.
    Applies aggressive Cache-Control headers to immutable content-addressed cache files
    (e.g., /audio/cache/*.wav) and standard cache lifetimes to ephemeral chunks,
    while ensuring Accept-Ranges: bytes support.
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        if response.status_code in (200, 206):
            norm_path = path.replace("\\", "/").strip("/")
            if norm_path.startswith("cache/") or "/cache/" in norm_path:
                response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
            else:
                response.headers["Cache-Control"] = "public, max-age=3600"
            if "accept-ranges" not in response.headers:
                response.headers["Accept-Ranges"] = "bytes"
        return response


class StaticCacheControlMiddleware:
    """
    Cache headers middleware: static assets are safe to cache (immutable fingerprinted
    assets cached for 1 year, other static for 1 hour), while user-generated audio is private.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "") if scope["type"] == "http" else ""
        if path in ("/", "/index.html"):
            # 入口页面必须每次回源校验，避免发版后浏览器用旧 index 加载旧 JS
            cache_value = "no-cache"
        elif path.startswith("/static/assets/") or path == "/static/assets":
            # 指纹化静态资源永久强缓存（1年），极大提升二次加载速度
            cache_value = "public, max-age=31536000, immutable"
        elif path.startswith("/static/"):
            cache_value = "public, max-age=3600"
        elif path.startswith("/audio/"):
            cache_value = "private, max-age=0"
        else:
            cache_value = None

        if cache_value and scope.get("method") in ("GET", "HEAD"):
            async def send_with_cache(message: Any) -> None:
                if message["type"] == "http.response.start":
                    status_code = message.get("status", 200)
                    if status_code < 400:
                        MutableHeaders(scope=message)["Cache-Control"] = cache_value
                    else:
                        # Never cache 4xx/5xx error responses immutably
                        MutableHeaders(scope=message)["Cache-Control"] = "no-cache, no-store, must-revalidate"
                await send(message)
            await self.app(scope, receive, send_with_cache)
            return
        await self.app(scope, receive, send)


_StaticCacheControlMiddleware = StaticCacheControlMiddleware


def create_app() -> FastAPI:
    """Factory function creating configured FastAPI application instance."""
    settings = get_settings()

    app = FastAPI(
        title="galgame2voice",
        description="Lightweight Python/FastAPI companion extension patch for GPT-SoVITS",
        version=settings.app_version,
        lifespan=lifespan,
        docs_url="/docs" if settings.enable_docs else None,
        redoc_url="/redoc" if settings.enable_docs else None,
    )

    # 1. Configure CORS Middleware
    # allow_origin_regex covers loopback on ANY port so the console keeps working
    # when the launcher auto-switches away from a busy 8080.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$",
        allow_credentials=settings.cors_allow_credentials,
        allow_methods=settings.cors_allow_methods,
        allow_headers=settings.cors_allow_headers,
    )

    # Request rate limiting. Middleware added later wraps this one, so the
    # cache-control/gzip/host-validation layers run first; CORS is inside it, so
    # 429s bypassing CORS is acceptable: the console is same-origin.
    app.add_middleware(RateLimitMiddleware)

    # DNS Rebinding mitigation (audit finding: Host header validation).
    from galgame2voice.security.auth import is_auth_disabled

    _is_loopback_host = str(settings.host).strip().lower() in ("127.0.0.1", "localhost", "::1")
    if (
        settings.auth_disabled
        and _is_loopback_host
        and is_auth_disabled()
        and not settings.host_header_validation_disabled
    ):
        app.add_middleware(HostValidationMiddleware)

    # Compress large static/JS/CSS payloads.
    app.add_middleware(GZipMiddleware, minimum_size=1024)

    # Cache headers: static assets vs user-generated audio
    app.add_middleware(StaticCacheControlMiddleware)

    # 2. Register API Routers (every router except health gets console token auth as a
    # router-level dependency; health declares it per-route for /api/system/*, leaving
    # /api/health and /status unauthenticated)
    auth_deps = [Depends(require_auth)]
    app.include_router(health.router)
    app.include_router(config.router, dependencies=auth_deps)
    app.include_router(voice.router, dependencies=auth_deps)
    app.include_router(characters.router, dependencies=auth_deps)
    app.include_router(chat.router, dependencies=auth_deps)
    app.include_router(memory.router, dependencies=auth_deps)
    app.include_router(affection.router, dependencies=auth_deps)
    app.include_router(metrics.router, dependencies=auth_deps)
    app.include_router(system.router, dependencies=auth_deps)


    # 3. Mount Static Audio Storage Directory
    settings.audio_dir.mkdir(parents=True, exist_ok=True)
    app.mount(
        "/audio",
        AudioStaticFiles(directory=str(settings.audio_dir)),
        name="audio",
    )

    # 4. Mount Frontend Static Assets
    if settings.static_dir.exists():
        app.mount(
            "/static",
            StaticFiles(directory=str(settings.static_dir)),
            name="static",
        )

        @app.get("/", include_in_schema=False)
        async def serve_index():
            index_path = settings.static_dir / "index.html"
            if index_path.exists():
                return FileResponse(str(index_path))
            return JSONResponse({"message": "galgame2voice backend active. UI index.html not found."})
    else:
        @app.get("/", include_in_schema=False)
        async def root_fallback():
            return JSONResponse({
                "app": settings.app_name,
                "version": settings.app_version,
                "docs": "/docs",
                "status": "/api/health",
            })

    @app.get("/settings.html", include_in_schema=False)
    @app.get("/console", include_in_schema=False)
    @app.get("/settings", include_in_schema=False)
    async def console_redirect(request: Request):
        query = request.url.query
        target_url = f"/?settings=1&{query}" if query else "/?settings=1"
        return RedirectResponse(url=target_url, status_code=status.HTTP_307_TEMPORARY_REDIRECT)

    # Global Exception Handler Sanitizing Internal Errors
    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception):
        logger.error("Unhandled Exception on %s %s: %s", request.method, request.url.path, exc, exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal Server Error", "error_type": type(exc).__name__},
        )

    return app


# Application singleton instance for Uvicorn
app = create_app()


def run():
    """CLI execution entrypoint."""
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "galgame2voice.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
        log_config=None,  # Delegate log formatting to custom MaskingFilter logger
    )


if __name__ == "__main__":
    run()
