"""
FastAPI Main Application Entry Point for galgame2voice.
Manages application lifespan, CORS, static routing, and router registration.
"""

import asyncio
import logging
import mimetypes
import os
import time

# Windows 注册表常把 .js 映射为 text/plain，ES module 会被浏览器 Strict MIME 拒载
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/javascript", ".mjs")
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import Depends, FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import MutableHeaders

# Starlette 新旧版本状态码名称兼容：旧版只有 HTTP_422_UNPROCESSABLE_ENTITY，
# 代码中多处使用新名 CONTENT；缺失时补齐为同一数值，否则校验路径会 500。
if not hasattr(status, "HTTP_422_UNPROCESSABLE_CONTENT"):
    status.HTTP_422_UNPROCESSABLE_CONTENT = 422

from galgame2voice.config import get_settings
from galgame2voice.database import crud
from galgame2voice.database.session import get_db, init_db
from galgame2voice.routers import chat, config, health, voice, memory, affection, characters, metrics
from galgame2voice.security.auth import require_auth
from galgame2voice.security.rate_limit import RateLimitMiddleware
from galgame2voice.services.gpt_sovits_client import get_gpt_sovits_client, close_gpt_sovits_client
from galgame2voice.utils.logger import setup_logger


logger = logging.getLogger("galgame2voice.main")


# TTS 缓存保留天数：缓存用于复用省算力，但需设上限防止磁盘无限增长
_CACHE_RETENTION_DAYS = 7


def _cache_scan_and_clean(cache_dir: Path, cutoff: float) -> Tuple[int, List[str]]:
    """Removes cache audio files older than the retention cutoff (LRU by mtime) and returns unlinked keys."""
    cleaned = 0
    unlinked_keys: List[str] = []
    if not cache_dir.is_dir():
        return 0, []
    for f in cache_dir.iterdir():
        if not f.is_file():
            continue
        if f.suffix.lower() in (".wav", ".ogg", ".mp3", ".opus"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
                    cleaned += 1
                    unlinked_keys.append(f.stem)
            except Exception as e:
                logger.debug("Failed to remove cached audio %s: %s", f, e)
    return cleaned, unlinked_keys


async def _audio_cleanup_loop(audio_dir: Path, interval_seconds: int):
    """Periodically removes ephemeral audio files exceeding retention duration while protecting persistent cache.

    The retention duration is re-read from DB settings each cycle so console
    edits to audio_retention_minutes take effect without a restart."""
    logger.info("Started background audio cleanup worker (interval=%d sec)", interval_seconds)
    while True:
        try:
            await asyncio.sleep(interval_seconds)
            protected_audio_names = set()
            try:
                async with get_db() as conn:
                    db_settings = await crud.get_settings_raw(conn)
                    if conn is not None:
                        try:
                            cur = await conn.execute("SELECT ref_audio_path FROM voice_profiles;")
                            rows = await cur.fetchall()
                            for r in rows:
                                if r and r[0]:
                                    protected_audio_names.add(Path(r[0]).name.lower())
                        except Exception:
                            pass
                retention_minutes = int(getattr(db_settings, "audio_retention_minutes", 30) or 30)
            except Exception as exc:
                logger.debug("Falling back to default audio retention: %s", exc)
                retention_minutes = 30
            now = time.time()
            cutoff = now - (retention_minutes * 60)
            cache_cutoff = now - (_CACHE_RETENTION_DAYS * 86400)

            def _scan_and_clean() -> Tuple[int, List[str]]:
                cleaned = 0
                if audio_dir.exists():
                    for f in audio_dir.iterdir():
                        # Strictly protect non-file entries and subdirectories (cache, references)
                        if f.is_dir() or f.name.lower() in ("cache", "references"):
                            continue
                        # Strictly protect reference audio files (*.ogg) and registered voice profile references
                        if f.suffix.lower() == ".ogg" or f.name.lower() in protected_audio_names:
                            continue
                        # Target ephemeral synthesized audio files (e.g. chunk_*.wav, full_*.wav)
                        if f.is_file() and f.suffix.lower() in (".wav", ".mp3", ".opus"):
                            try:
                                if f.stat().st_mtime < cutoff:
                                    f.unlink()
                                    cleaned += 1
                            except Exception as e:
                                logger.debug("Failed to remove audio file %s: %s", f, e)
                # TTS 分句缓存按天级保留期清理，防止磁盘无限增长并同步清理数据库记录
                cache_cleaned, unlinked_keys = _cache_scan_and_clean(audio_dir / "cache", cache_cutoff)
                cleaned += cache_cleaned
                return cleaned, unlinked_keys

            cleaned_count, unlinked_keys = await asyncio.to_thread(_scan_and_clean)
            if unlinked_keys:
                try:
                    async with get_db() as conn:
                        for batch_idx in range(0, len(unlinked_keys), 100):
                            batch = unlinked_keys[batch_idx:batch_idx + 100]
                            placeholders = ",".join(["?"] * len(batch))
                            await conn.execute(
                                f"DELETE FROM tts_cache_entries WHERE cache_key IN ({placeholders});",
                                batch,
                            )
                except Exception as db_clean_err:
                    logger.debug("Failed to purge tts_cache_entries for unlinked keys: %s", db_clean_err)
            if cleaned_count > 0:
                logger.info("Audio cleanup removed %d expired audio files.", cleaned_count)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.warning("Error in audio cleanup loop: %s", exc)


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

    # 1. Initialize Logger with Secret Masking
    setup_logger(
        log_level=settings.log_level,
        logs_dir=settings.logs_dir if settings.log_to_file else None,
        log_to_file=settings.log_to_file,
    )
    logger.info("Initializing %s v%s...", settings.app_name, settings.app_version)

    # 2. Ensure Required Directories Exist
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.audio_dir.mkdir(parents=True, exist_ok=True)
    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    logger.info("Verified directories: data=%s, audio=%s, logs=%s",
                settings.data_dir, settings.audio_dir, settings.logs_dir)

    # 3. Initialize SQLite Database Schema (WAL Mode) & Self-Heal broken audio references
    # Fail-fast: a broken database must not silently degrade into a
    # half-functional service that reports healthy.
    await init_db(settings.db_path)
    logger.info("Database initialized successfully at %s", settings.db_path)

    try:
        async with get_db(settings.db_path) as conn:
            healed = await crud.auto_heal_voice_profiles(conn)
            if healed > 0:
                logger.info("Auto-healed %d voice profile(s) with missing/invalid reference audio paths", healed)
    except Exception as exc:
        logger.debug("Startup auto-heal check skipped: %s", exc)

    # 4. Initialize shared GPT-SoVITS client (single inference mutex app-wide).
    #    The DB's gpt_sovits_url takes priority over the .env default so the
    #    settings console is the source of truth.
    try:
        sovits_url = None
        try:
            async with get_db(settings.db_path) as conn:
                db_settings = await crud.get_settings_raw(conn)
                if db_settings and getattr(db_settings, "gpt_sovits_url", None):
                    sovits_url = db_settings.gpt_sovits_url
        except Exception as exc:
            logger.warning("Could not read gpt_sovits_url from DB: %s", exc)
        client = get_gpt_sovits_client()
        if sovits_url and sovits_url.rstrip("/") != client.base_url:
            await client.set_base_url(sovits_url)

        # Pre-seed active voice profile from DB so frontend's initial switch is instantaneous
        try:
            from galgame2voice.services.voice_manager import get_voice_manager
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
                        ref_p = Path(default_profile.ref_audio_path)
                        if not ref_p.is_file() and (settings.project_root / default_profile.ref_audio_path).is_file():
                            ref_p = (settings.project_root / default_profile.ref_audio_path).resolve()
                        client.current_refer_audio = str(ref_p)
                    if not client.current_refer_text:
                        client.current_refer_text = default_profile.prompt_text
                    if not client.current_refer_language:
                        client.current_refer_language = default_profile.prompt_lang
        except Exception as exc:
            logger.debug("Could not pre-seed active voice profile on startup: %s", exc)

        logger.info("GPT-SoVITS client initialized (endpoint: %s)", client.base_url)
    except Exception as exc:
        logger.error("Failed to initialize GPT-SoVITS client: %s", exc, exc_info=True)

    # 5. Start Background Audio Cleanup Loop
    cleanup_task = asyncio.create_task(
        _audio_cleanup_loop(
            audio_dir=settings.audio_dir,
            interval_seconds=settings.audio_cleanup_interval_seconds,
        )
    )

    # 6. Start Telegram Bot Background Polling (non-blocking background task)
    tg_startup_task = None
    try:
        from galgame2voice.telegram_bot.bot import get_telegram_bot_manager
        tg_manager = get_telegram_bot_manager(db_path=settings.db_path)

        async def _start_telegram_bg():
            try:
                tg_started = await tg_manager.start()
                if tg_started:
                    logger.info("Telegram Bot background polling started successfully.")
            except Exception as exc:
                logger.warning("Telegram Bot auto-start on boot skipped or failed: %s", exc)

        tg_startup_task = asyncio.create_task(_start_telegram_bg())
    except Exception as exc:
        logger.warning("Telegram Bot auto-start on boot skipped or failed: %s", exc)

    logger.info(
        "Service startup complete. Listening on http://%s:%d",
        settings.host,
        settings.port,
    )

    yield  # Application serving requests

    # --- SHUTDOWN PHASE ---
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

    # Drain background tasks from ChatService and TTS cache before WAL checkpoint
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
                if hasattr(chat_svc, "tts_service") and hasattr(chat_svc.tts_service, "cache_manager"):
                    if hasattr(chat_svc.tts_service.cache_manager, "aclose"):
                        await chat_svc.tts_service.cache_manager.aclose()
        import galgame2voice.services.tts_cache_manager as tts_cache_mod
        if getattr(tts_cache_mod, "_tts_cache_manager_instance", None) is not None:
            if hasattr(tts_cache_mod._tts_cache_manager_instance, "aclose"):
                await tts_cache_mod._tts_cache_manager_instance.aclose()
    except Exception as exc:
        logger.debug("Error draining background tasks on shutdown: %s", exc)

    # Safe SQLite WAL truncation checkpoint
    try:
        async with get_db(settings.db_path) as conn:
            await conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
    except Exception as exc:
        logger.debug("WAL checkpoint on shutdown: %s", exc)

    logger.info("Shutting down %s...", settings.app_name)
    logger.info("Graceful shutdown complete.")


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
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=settings.cors_allow_credentials,
        allow_methods=settings.cors_allow_methods,
        allow_headers=settings.cors_allow_headers,
    )

    # Request rate limiting (outermost middleware). 429s bypassing CORS is
    # acceptable: the console is same-origin.
    app.add_middleware(RateLimitMiddleware)

    # Compress large static/JS/CSS payloads (settings.html alone is ~170 KB).
    app.add_middleware(GZipMiddleware, minimum_size=1024)

    # Cache headers: static assets are safe to cache for an hour (cache-busted
    # with ?v= query params), but user-generated audio is private.
    class _StaticCacheControlMiddleware:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            path = scope.get("path", "") if scope["type"] == "http" else ""
            if path == "/" or path == "/settings.html" or path == "/index.html":
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
                async def send_with_cache(message):
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

    app.add_middleware(_StaticCacheControlMiddleware)

    # 2. Register API Routers (all management/data routes require console token auth)
    auth_deps = [Depends(require_auth)]
    app.include_router(health.router)
    app.include_router(config.router, dependencies=auth_deps)
    app.include_router(voice.router, dependencies=auth_deps)
    app.include_router(characters.router, dependencies=auth_deps)
    app.include_router(chat.router, dependencies=auth_deps)
    app.include_router(memory.router, dependencies=auth_deps)
    app.include_router(affection.router, dependencies=auth_deps)
    app.include_router(metrics.router, dependencies=auth_deps)


    # 3. Mount Static Audio Storage Directory
    settings.audio_dir.mkdir(parents=True, exist_ok=True)
    app.mount(
        "/audio",
        StaticFiles(directory=str(settings.audio_dir)),
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

        @app.get("/settings.html", include_in_schema=False)
        async def serve_settings():
            settings_path = settings.static_dir / "settings.html"
            if settings_path.exists():
                return FileResponse(str(settings_path))
            return JSONResponse({"message": "Settings UI not found."})

        @app.get("/console", include_in_schema=False)
        @app.get("/settings", include_in_schema=False)
        async def console_redirect(request: Request):
            query = request.url.query
            target_url = f"/settings.html?{query}" if query else "/settings.html"
            return RedirectResponse(url=target_url, status_code=status.HTTP_307_TEMPORARY_REDIRECT)
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
        async def settings_fallback():
            return JSONResponse({"message": "Settings UI not found in static directory."})

        @app.get("/console", include_in_schema=False)
        @app.get("/settings", include_in_schema=False)
        async def console_fallback_redirect(request: Request):
            query = request.url.query
            target_url = f"/settings.html?{query}" if query else "/settings.html"
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
