"""
Configuration and Settings Router for galgame2voice.
Provides REST endpoints for global system configuration.
"""

import logging
from typing import Any, Dict, Optional, Union

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from galgame2voice.database import crud
from galgame2voice.database.models import SettingsUpdate
from galgame2voice.database.session import get_db
from galgame2voice.security.url_guard import validate_local_service_url
from galgame2voice.routers.providers import (
    ProviderTestRequest,
    ProviderTestResponse,
    TelegramTestRequest,
    _allow_private_endpoints,
    _enforce_llm_url_guard,
    activate_provider,
    create_or_update_provider,
    delete_provider,
    get_presets,
    get_provider,
    get_provider_models,
    list_providers,
    router as providers_router,
    test_provider,
    test_provider_connectivity,
    test_telegram_bot,
    update_provider,
)
from galgame2voice.utils.logger import sanitize_error_detail

logger = logging.getLogger("galgame2voice.routers.config")
router = APIRouter(prefix="/api", tags=["Configuration & Providers"])

# Include modularized providers & connectivity testing router
router.include_router(providers_router)

__all__ = [
    "router",
    "ConfigPayload",
    "ProviderTestRequest",
    "ProviderTestResponse",
    "TelegramTestRequest",
    "_allow_private_endpoints",
    "_enforce_llm_url_guard",
    "providers_router",
    "list_providers",
    "get_presets",
    "get_provider",
    "create_or_update_provider",
    "update_provider",
    "delete_provider",
    "activate_provider",
    "get_provider_models",
    "test_provider",
    "test_provider_connectivity",
    "test_telegram_bot",
]


class ConfigPayload(BaseModel):
    """Flexible configuration update payload accepting nested settings or direct attributes."""
    settings: Optional[Dict[str, Any]] = None


# ============================================================================
# 1. Global Settings Endpoints
# ============================================================================

@router.get(
    "/config",
    summary="Get Global Configuration",
    description="Returns current system settings and active provider with masked sensitive keys.",
)
async def get_config():
    async with get_db() as conn:
        settings = await crud.get_settings(conn, mask=True)
        active_provider = await crud.get_active_provider(conn, mask=True)

        settings_dict = settings.model_dump()
        # Ensure compatibility with both dict mapping and model schema
        return {
            "status": "ok",
            "settings": settings_dict,
            "active_provider": active_provider.model_dump() if active_provider else None,
        }


TELEGRAM_CONFIG_KEYS = frozenset({
    "telegram_enabled",
    "telegram_bot_token",
    "telegram_bot_username",
    "telegram_proxy_enabled",
    "telegram_proxy_host",
    "telegram_proxy_port",
    "telegram_admin_ids",
    "telegram_chat_id",
})


async def _apply_sovits_url_update(new_sovits_url: Optional[str]) -> None:
    """Hot-applies GPT-SoVITS endpoint change to the shared client so it takes effect immediately."""
    if not new_sovits_url:
        return
    try:
        from galgame2voice.services.gpt_sovits_client import reload_gpt_sovits_client_base_url
        await reload_gpt_sovits_client_base_url(str(new_sovits_url))
        logger.info("GPT-SoVITS endpoint hot-applied: %s", new_sovits_url)
    except Exception as exc:
        logger.error("Failed to hot-apply GPT-SoVITS URL '%s': %s", new_sovits_url, exc)


async def _reload_telegram_if_needed(sanitized_updates: Dict[str, Any], updated_settings: Any) -> None:
    """Hot-reloads Telegram Bot service when Telegram credentials/proxy/enabled/admin state changes."""
    if not any(k in sanitized_updates for k in TELEGRAM_CONFIG_KEYS):
        return
    try:
        from galgame2voice.telegram_bot.bot import get_telegram_bot_manager
        tg_manager = get_telegram_bot_manager()
        await tg_manager.stop()
        if getattr(updated_settings, "telegram_enabled", False):
            await tg_manager.start()
            logger.info("Telegram Bot service hot-reloaded with new configuration.")
        else:
            logger.info("Telegram Bot service is disabled.")
    except Exception as exc:
        logger.warning("Failed to hot-reload Telegram Bot: %s", exc)


def _sync_precision_cache(new_precision: Optional[str]) -> None:
    """Syncs precision cache files on disk when inference_precision is updated."""
    if not new_precision:
        return
    try:
        from galgame2voice.utils.precision import write_precision_cache, write_sovits_yaml_config
        from galgame2voice.config import get_settings
        app_settings = get_settings()
        sovits_dir_file = app_settings.project_root / "data" / "sovits_dir.txt"
        sovits_dir_str = sovits_dir_file.read_text(encoding="utf-8-sig").strip() if sovits_dir_file.exists() else ""
        prec_lower = str(new_precision).lower()
        precision_map = {
            "cpu": (False, "cpu"),
            "fp16": (True, "cuda"),
            "half": (True, "cuda"),
            "fp32": (False, "cuda"),
            "float32": (False, "cuda"),
        }
        if prec_lower in precision_map:
            is_half, device = precision_map[prec_lower]
            write_precision_cache(app_settings.project_root, sovits_dir_str, is_half=is_half, device=device)
            if sovits_dir_str:
                write_sovits_yaml_config(sovits_dir_str, is_half=is_half, device=device)
        elif prec_lower == "auto":
            cache_file = app_settings.project_root / "data" / "precision.json"
            if cache_file.exists():
                cache_file.unlink(missing_ok=True)
        logger.info("Inference precision configuration synced: %s", new_precision)
    except Exception as exc:
        logger.warning("Failed to sync precision cache on config update: %s", exc)


@router.post(
    "/config",
    summary="Update Global Configuration",
    description="Updates system configuration values in SQLite persistence. GPT-SoVITS URL changes are applied live (no restart needed).",
)
async def update_config(payload: Union[ConfigPayload, SettingsUpdate, Dict[str, Any]]):
    update_data: Dict[str, Any] = {}
    if isinstance(payload, ConfigPayload) and payload.settings is not None:
        update_data = payload.settings
    elif isinstance(payload, SettingsUpdate):
        update_data = payload.model_dump(exclude_unset=True)
    elif isinstance(payload, dict):
        update_data = payload.get("settings", payload)

    async with get_db() as conn:
        # Filter valid settings fields for update
        valid_fields = SettingsUpdate.model_fields.keys()
        sanitized_updates = {k: v for k, v in update_data.items() if k in valid_fields}

        # SSRF guard: the GPT-SoVITS endpoint is a legitimate LAN/local service,
        # but it must still be a plain http(s) URL (no file://, gopher://,
        # embedded credentials, ...).
        if "gpt_sovits_url" in sanitized_updates and sanitized_updates["gpt_sovits_url"]:
            ok, reason = validate_local_service_url(str(sanitized_updates["gpt_sovits_url"]))
            if not ok:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid gpt_sovits_url: {reason}",
                )

        if sanitized_updates:
            try:
                update_model = SettingsUpdate(**sanitized_updates)
            except Exception as val_err:
                safe_detail = sanitize_error_detail(val_err)
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail=f"Invalid configuration parameters: {safe_detail}",
                ) from val_err
            updated_settings = await crud.update_settings(conn, update_model)
        else:
            updated_settings = await crud.get_settings(conn, mask=True)

    await _apply_sovits_url_update(sanitized_updates.get("gpt_sovits_url"))
    await _reload_telegram_if_needed(sanitized_updates, updated_settings)
    _sync_precision_cache(sanitized_updates.get("inference_precision"))

    return {
        "status": "success",
        "updated_count": len(sanitized_updates) if sanitized_updates else len(update_data),
        "settings": updated_settings.model_dump(),
    }
