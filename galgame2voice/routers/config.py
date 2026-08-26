"""
Configuration and Provider Management Router for galgame2voice.
Provides REST endpoints for global settings, provider configurations,
real-time connectivity testing, and model discovery.
"""

import json
import logging
import time
from typing import Any, Dict, List, Optional, Union
import httpx
from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from galgame2voice.adapters.base import TestResult
from galgame2voice.adapters.registry import (
    get_llm_adapter,
    get_stt_adapter,
    list_provider_presets,
    get_provider_preset,
)
from galgame2voice.database.session import get_db
from galgame2voice.database import crud
from galgame2voice.database.models import (
    SettingsResponse,
    SettingsUpdate,
    ProviderCreate,
    ProviderUpdate,
    ProviderResponse,
)

logger = logging.getLogger("galgame2voice.routers.config")
router = APIRouter(prefix="/api", tags=["Configuration & Providers"])


class ConfigPayload(BaseModel):
    """Flexible configuration update payload accepting nested settings or direct attributes."""
    settings: Optional[Dict[str, Any]] = None


class ProviderTestRequest(BaseModel):
    """Payload for real-time provider connectivity and credential testing."""
    provider_type: Optional[str] = None
    id: Optional[str] = None
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    api_base_url: Optional[str] = None
    model: Optional[str] = None
    chat_model: Optional[str] = None
    custom_headers: Optional[Dict[str, Any]] = None


class ProviderTestResponse(BaseModel):
    """Response of provider connectivity and latency test."""
    __test__ = False
    success: bool
    message: str
    latency_ms: Optional[float] = None
    models: Optional[List[str]] = None


class TelegramTestRequest(BaseModel):
    """Payload for real-time Telegram bot token connectivity testing."""
    token: Optional[str] = None
    bot_token: Optional[str] = None
    proxy_enabled: Optional[bool] = False
    proxy_host: Optional[str] = "127.0.0.1"
    proxy_port: Optional[int] = 10809


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

        if sanitized_updates:
            update_model = SettingsUpdate(**sanitized_updates)
            updated_settings = await crud.update_settings(conn, update_model)
        else:
            updated_settings = await crud.get_settings(conn, mask=True)

    # Hot-apply GPT-SoVITS endpoint change to the shared client so the new URL
    # takes effect immediately (previously this setting was saved but never used).
    new_sovits_url = sanitized_updates.get("gpt_sovits_url")
    if new_sovits_url:
        try:
            from galgame2voice.services.gpt_sovits_client import reload_gpt_sovits_client_base_url
            await reload_gpt_sovits_client_base_url(str(new_sovits_url))
            logger.info("GPT-SoVITS endpoint hot-applied: %s", new_sovits_url)
        except Exception as exc:
            logger.error("Failed to hot-apply GPT-SoVITS URL '%s': %s", new_sovits_url, exc)

    return {
        "status": "success",
        "updated_count": len(sanitized_updates) if sanitized_updates else len(update_data),
        "settings": updated_settings.model_dump(),
    }


# ============================================================================
# 2. Provider Management Endpoints
# ============================================================================

@router.get(
    "/providers",
    summary="List Configured Providers",
    description="Returns all configured LLM/STT providers (with masked keys) and available presets.",
)
async def list_providers():
    async with get_db() as conn:
        providers = await crud.list_providers(conn, mask=True)
        presets = list_provider_presets()
        return {
            "providers": [p.model_dump() for p in providers],
            "presets": presets,
        }


@router.get(
    "/providers/presets",
    summary="List Provider Presets",
    description="Returns built-in templates for 10+ major LLM providers.",
)
async def get_presets():
    return {"presets": list_provider_presets()}


@router.get(
    "/providers/{provider_id}",
    summary="Get Single Provider",
    description="Returns provider details by ID with masked API key.",
)
async def get_provider(provider_id: str):
    async with get_db() as conn:
        provider = await crud.get_provider(conn, provider_id, mask=True)
        if not provider:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Provider '{provider_id}' not found")
        return {"provider": provider.model_dump()}


@router.post(
    "/providers",
    summary="Create or Update Provider",
    description="Upserts an LLM/STT provider profile, safely retaining existing secret keys if masked.",
)
async def create_or_update_provider(provider_data: Dict[str, Any]):
    provider_id = provider_data.get("id") or provider_data.get("provider_type")
    if not provider_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Missing required field 'id' or 'provider_type'",
        )

    provider_id = str(provider_id).strip().lower()

    async with get_db() as conn:
        existing = await crud.get_provider_raw(conn, provider_id)
        if existing:
            # Update existing provider
            update_kwargs = {}
            if "name" in provider_data and provider_data["name"] is not None:
                update_kwargs["name"] = provider_data["name"]
            if "api_base_url" in provider_data or "base_url" in provider_data:
                url_val = provider_data.get("api_base_url") or provider_data.get("base_url")
                if url_val is not None:
                    update_kwargs["api_base_url"] = url_val
            if "api_key" in provider_data and provider_data["api_key"] is not None:
                update_kwargs["api_key"] = provider_data["api_key"]
            if "chat_model" in provider_data or "model" in provider_data:
                model_val = provider_data.get("chat_model") or provider_data.get("model")
                if model_val is not None:
                    update_kwargs["chat_model"] = model_val
            if "stt_model" in provider_data and provider_data["stt_model"] is not None:
                update_kwargs["stt_model"] = provider_data["stt_model"]
            if "is_active" in provider_data and provider_data["is_active"] is not None:
                update_kwargs["is_active"] = provider_data["is_active"]
            if "custom_headers" in provider_data and provider_data["custom_headers"] is not None:
                update_kwargs["custom_headers"] = provider_data["custom_headers"]

            updates = ProviderUpdate(**update_kwargs)
            updated = await crud.update_provider(conn, provider_id, updates)
            return {"status": "success", "provider": updated.model_dump() if updated else None}
        else:
            # Preset default values if not provided
            preset = get_provider_preset(provider_id)
            name = provider_data.get("name") or (preset["name"] if preset else provider_id.capitalize())
            base_url = (
                provider_data.get("api_base_url")
                or provider_data.get("base_url")
                or (preset["default_base_url"] if preset else "https://api.openai.com/v1")
            )
            chat_model = (
                provider_data.get("chat_model")
                or provider_data.get("model")
                or (preset["default_chat_model"] if preset else "gpt-4o-mini")
            )
            stt_model = (
                provider_data.get("stt_model")
                or (preset["default_stt_model"] if preset else "")
            )
            is_active = bool(provider_data.get("is_active", False))
            api_key = provider_data.get("api_key", "")
            custom_headers = provider_data.get("custom_headers") or {}

            new_provider = ProviderCreate(
                id=provider_id,
                name=name,
                api_base_url=base_url,
                api_key=api_key,
                chat_model=chat_model,
                stt_model=stt_model,
                is_active=is_active,
                custom_headers=custom_headers,
            )
            created = await crud.create_provider(conn, new_provider)
            return {"status": "created", "provider": created.model_dump()}


@router.delete(
    "/providers/{provider_id}",
    summary="Delete Provider",
    description="Deletes a provider configuration by ID.",
)
async def delete_provider(provider_id: str):
    async with get_db() as conn:
        success = await crud.delete_provider(conn, provider_id)
        if not success:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Provider '{provider_id}' not found")
        return {"status": "deleted", "provider_id": provider_id}


# ============================================================================
# 3. Real-Time Connectivity Testing & Model Discovery
# ============================================================================

@router.post(
    "/providers/test",
    response_model=ProviderTestResponse,
    summary="Test Provider Connectivity",
    description="Probes connection, verifies credentials, measures latency, and discovers available models.",
)
async def test_provider(req: ProviderTestRequest):
    provider_id = (req.provider_type or req.id or "openai").strip().lower()
    api_key = req.api_key or ""
    base_url = req.base_url or req.api_base_url
    model = req.model or req.chat_model
    custom_headers = req.custom_headers or {}

    # If api_key is omitted or masked, lookup the stored unmasked key from DB
    if not api_key or "****" in api_key:
        async with get_db() as conn:
            stored = await crud.get_provider_raw(conn, provider_id)
            if stored and stored.api_key:
                # SSRF Protection: only allow stored credentials against the configured base_url
                if base_url and stored.api_base_url and base_url.rstrip("/") != stored.api_base_url.rstrip("/"):
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="Cannot test custom base_url with stored API key. Please provide the explicit API key."
                    )
                api_key = stored.api_key
                if not base_url:
                    base_url = stored.api_base_url
                if not model:
                    model = stored.chat_model

    # Instantiate adapter via factory
    adapter = get_llm_adapter(
        provider_id_or_config=provider_id,
        api_key=api_key,
        base_url=base_url,
        custom_headers=custom_headers,
    )

    result = await adapter.test_connection(model=model)
    return ProviderTestResponse(
        success=result.success,
        message=result.message,
        latency_ms=result.latency_ms,
        models=result.models,
    )


@router.get(
    "/providers/{provider_id}/models",
    summary="Fetch Provider Model List",
    description="Fetches live available model list directly from the provider's /models API.",
)
async def get_provider_models(provider_id: str):
    async with get_db() as conn:
        stored = await crud.get_provider_raw(conn, provider_id)
        if not stored:
            preset = get_provider_preset(provider_id)
            if not preset:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Provider '{provider_id}' not found",
                )
            return {"provider_id": provider_id, "models": preset["preset_models"]}

        adapter = get_llm_adapter(
            provider_id_or_config=stored,
            api_key=stored.api_key,
            base_url=stored.api_base_url,
            custom_headers=stored.custom_headers,
        )

        try:
            models = await adapter.list_models()
            return {"provider_id": provider_id, "models": models}
        except Exception as exc:
            logger.warning("Failed to list models for provider %s: %s", provider_id, exc)
            preset = get_provider_preset(provider_id)
            fallback = preset["preset_models"] if preset else [stored.chat_model]
            return {"provider_id": provider_id, "models": fallback, "warning": str(exc)}


@router.post(
    "/providers/{provider_id}/activate",
    summary="Set Active Provider",
    description="Sets specified provider as the active LLM provider.",
)
async def activate_provider(provider_id: str):
    provider_id = provider_id.strip().lower()
    async with get_db() as conn:
        existing = await crud.get_provider_raw(conn, provider_id)
        if not existing:
            preset = get_provider_preset(provider_id)
            if preset:
                new_provider = ProviderCreate(
                    id=provider_id,
                    name=preset["name"],
                    api_base_url=preset["default_base_url"],
                    api_key="",
                    chat_model=preset["default_chat_model"],
                    stt_model=preset["default_stt_model"],
                    is_active=True,
                )
                await crud.create_provider(conn, new_provider)
        success = await crud.set_active_provider(conn, provider_id)
        if not success:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Provider '{provider_id}' not found")
        active = await crud.get_active_provider(conn, mask=True)
        return {"status": "success", "active_provider": active.model_dump() if active else None}


# ============================================================================
# 4. Telegram Bot Testing Endpoints
# ============================================================================

@router.post(
    "/telegram/test",
    summary="Test Telegram Bot Token & Connectivity",
    description="Probes Telegram getMe API endpoint with configured token and optional proxy.",
)
async def test_telegram_bot(req: TelegramTestRequest):
    token = (req.token or req.bot_token or "").strip()
    if not token or "****" in token:
        async with get_db() as conn:
            stored_settings = await crud.get_settings_raw(conn)
            token = stored_settings.telegram_bot_token or ""

    token = token.replace(" ", "").replace("\r", "").replace("\n", "").strip()

    if not token:
        return {"success": False, "message": "未配置 Telegram Bot Token"}

    proxy_urls = []
    if req.proxy_enabled and req.proxy_host and req.proxy_port:
        host = str(req.proxy_host).strip()
        port = str(req.proxy_port).strip()
        if host.startswith("http://") or host.startswith("https://") or host.startswith("socks5://") or host.startswith("socks4://"):
            proxy_urls = [f"{host}:{port}" if ":" not in host.split("//")[-1] else host]
        else:
            proxy_urls = [f"http://{host}:{port}", f"socks5://{host}:{port}"]
    else:
        proxy_urls = [None]

    t0 = time.perf_counter()
    last_err = None
    for p_url in proxy_urls:
        try:
            async with httpx.AsyncClient(proxy=p_url, timeout=6.0) as client:
                resp = await client.get(f"https://api.telegram.org/bot{token}/getMe")
                latency = round((time.perf_counter() - t0) * 1000, 2)
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("ok"):
                        bot_user = data.get("result", {}).get("username", "")
                        return {
                            "success": True,
                            "message": f"连接成功！Bot: @{bot_user}",
                            "latency_ms": latency,
                            "bot_info": data.get("result"),
                        }
                    else:
                        return {
                            "success": False,
                            "message": f"Telegram API 错误: {data.get('description', '未知错误')}",
                            "latency_ms": latency,
                        }
                elif resp.status_code == 401:
                    return {
                        "success": False,
                        "message": "Telegram 验证失败 (401 Unauthorized): Token 错误或已失效，请在 Telegram 中私聊 @BotFather 发送 /token 重新获取最新 Token",
                        "latency_ms": latency,
                    }
                elif resp.status_code == 404:
                    return {
                        "success": False,
                        "message": "Telegram 验证失败 (404 Not Found): 无效的 Bot Token 格式，请检查 Token 是否包含多余字符或从 @BotFather 完整复制",
                        "latency_ms": latency,
                    }
                else:
                    data = {}
                    try:
                        data = resp.json()
                    except Exception:
                        pass
                    err_desc = data.get("description") if isinstance(data, dict) else f"HTTP {resp.status_code}"
                    return {
                        "success": False,
                        "message": f"Telegram 验证失败 ({resp.status_code}): {err_desc}",
                        "latency_ms": latency,
                    }
        except Exception as exc:
            last_err = exc
            continue

    latency = round((time.perf_counter() - t0) * 1000, 2)
    err_msg = str(last_err)
    if "ConnectError" in str(type(last_err)) or "10061" in err_msg or "refused" in err_msg.lower():
        hint = f"连接被拒绝。请检查代理端口是否填写正确（例如 v2rayN 常用 10808，Clash 常用 7890）且代理客户端处于运行状态。"
        return {
            "success": False,
            "message": f"代理连接失败: {hint}",
            "latency_ms": latency,
        }
    return {
        "success": False,
        "message": f"连接失败: {type(last_err).__name__} - {err_msg}",
        "latency_ms": latency,
    }
