"""
FastAPI Main Application Entry Point for galgame2voice.
Manages application lifespan, CORS, static routing, and router registration.
"""

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from galgame2voice.config import get_settings
from galgame2voice.database import crud
from galgame2voice.database.session import get_db, init_db
from galgame2voice.routers import chat, config, health, voice, memory, affection, metrics
from galgame2voice.services.gpt_sovits_client import get_gpt_sovits_client, close_gpt_sovits_client
from galgame2voice.utils.logger import setup_logger


logger = logging.getLogger("galgame2voice.main")


async def _audio_cleanup_loop(audio_dir: Path, retention_minutes: int, interval_seconds: int):
    """Periodically removes ephemeral audio files exceeding retention duration while protecting persistent cache."""
    logger.info("Started background audio cleanup worker (retention=%d min, interval=%d sec)",
                retention_minutes, interval_seconds)
    while True:
        try:
            await asyncio.sleep(interval_seconds)
            now = time.time()
            cutoff = now - (retention_minutes * 60)
            cleaned_count = 0
            if audio_dir.exists():
                for f in audio_dir.iterdir():
                    # Strictly protect cache directory and non-file entries
                    if f.is_dir() or f.name.lower() == "cache":
                        continue
                    if f.is_file() and f.suffix.lower() in (".wav", ".ogg", ".mp3", ".opus"):
                        try:
                            if f.stat().st_mtime < cutoff:
                                f.unlink()
                                cleaned_count += 1
                        except Exception as e:
                            logger.debug("Failed to remove audio file %s: %s", f, e)
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

    # 3. Initialize SQLite Database Schema (WAL Mode)
    try:
        await init_db(settings.db_path)
        logger.info("Database initialized successfully at %s", settings.db_path)
    except Exception as exc:
        logger.error("Failed to initialize database: %s", exc, exc_info=True)

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
        logger.info("GPT-SoVITS client initialized (endpoint: %s)", client.base_url)
    except Exception as exc:
        logger.error("Failed to initialize GPT-SoVITS client: %s", exc, exc_info=True)

    # 5. Start Background Audio Cleanup Loop
    cleanup_task = asyncio.create_task(
        _audio_cleanup_loop(
            audio_dir=settings.audio_dir,
            retention_minutes=settings.audio_retention_minutes,
            interval_seconds=settings.audio_cleanup_interval_seconds,
        )
    )

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

    # Release the shared GPT-SoVITS connection pool.
    try:
        await close_gpt_sovits_client()
    except Exception as exc:
        logger.debug("Error closing GPT-SoVITS client: %s", exc)

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
        docs_url="/docs",
        redoc_url="/redoc",
    )

    # 1. Configure CORS Middleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=settings.cors_allow_credentials,
        allow_methods=settings.cors_allow_methods,
        allow_headers=settings.cors_allow_headers,
    )

    # 2. Register API Routers
    app.include_router(health.router)
    app.include_router(config.router)
    app.include_router(voice.router)
    app.include_router(chat.router)
    app.include_router(memory.router)
    app.include_router(affection.router)
    app.include_router(metrics.router)


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
