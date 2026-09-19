"""
Provider and Connectivity Testing Router for galgame2voice.
Provides REST endpoints for LLM/STT provider configurations,
presets, live model discovery, in-flight connectivity testing,
and Telegram bot token verification.
"""

import asyncio
import logging
import sqlite3
import time
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from galgame2voice.adapters.registry import (
    get_llm_adapter,
    get_provider_preset,
    list_provider_presets,
)
from galgame2voice.database import crud
from galgame2voice.database.crud import is_masked_key
from galgame2voice.database.models import (
    ProviderCreate,
    ProviderUpdate,
    SettingsInDB,
)
from galgame2voice.database.session import get_db
from galgame2voice.security import url_guard
from galgame2voice.telegram_bot.proxy import get_proxy_url
from galgame2voice.utils.error_diagnostics import format_provider_error
from galgame2voice.utils.logger import sanitize_error_detail

logger = logging.getLogger("galgame2voice.routers.providers")
router = APIRouter(tags=["Providers & Testing"])
providers_router = router


async def _allow_private_endpoints() -> bool:
    async with get_db() as conn:
        settings = await crud.get_settings_raw(conn)
    return bool(getattr(settings, "allow_private_llm_endpoints", False))


async def _enforce_llm_url_guard(base_url: Optional[str]) -> None:
    """Rejects LLM provider base URLs that resolve to loopback/private ranges
    unless the operator explicitly enabled private endpoints."""
    if not base_url:
        return
    allow_private = await _allow_private_endpoints()
    ok, reason = await asyncio.to_thread(url_guard.validate_llm_base_url, base_url, allow_private)
    if not ok:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=reason)


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
    error: Optional[str] = None
    diagnostic: Optional[str] = None


class TelegramTestRequest(BaseModel):
    """Payload for real-time Telegram bot token connectivity testing."""
    token: Optional[str] = None
    bot_token: Optional[str] = None
    proxy_enabled: Optional[bool] = False
    proxy_host: Optional[str] = "127.0.0.1"
    proxy_port: Optional[int] = Field(default=10809, ge=1, le=65535)


# ============================================================================
# Provider Management Endpoints
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
        existing_ids = {p.id for p in providers}
        all_providers = [p.model_dump() for p in providers]
        for pr in presets:
            if pr["id"] not in existing_ids:
                all_providers.append({
                    "id": pr["id"],
                    "name": pr.get("name", pr["id"]),
                    "api_base_url": pr.get("default_base_url", ""),
                    "chat_model": pr.get("default_chat_model", ""),
                    "stt_model": pr.get("default_stt_model", ""),
                    "api_key": "",
                    "is_active": False,
                    "custom_headers": {},
                })
        return {
            "providers": all_providers,
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
    clean_id = (provider_id or "").strip().lower()
    if not clean_id or len(clean_id) > 64:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Provider '{provider_id}' not found")
    async with get_db() as conn:
        provider = await crud.get_provider(conn, clean_id, mask=True)
        if not provider:
            preset = get_provider_preset(clean_id)
            if preset:
                return {
                    "provider": {
                        "id": clean_id,
                        "name": preset.get("name", clean_id),
                        "api_base_url": preset.get("default_base_url", ""),
                        "chat_model": preset.get("default_chat_model", ""),
                        "stt_model": preset.get("default_stt_model", ""),
                        "api_key": "",
                        "is_active": False,
                        "custom_headers": {},
                    }
                }
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Provider '{provider_id}' not found")
        return {"provider": provider.model_dump()}


@router.post(
    "/providers",
    summary="Create or Update Provider",
    description="Upserts an LLM/STT provider profile, safely retaining existing secret keys if masked.",
)
async def create_or_update_provider(provider_data: Dict[str, Any]):
    raw_id = provider_data.get("id") or provider_data.get("provider_type")
    if not raw_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Missing required field 'id' or 'provider_type'",
        )

    provider_id = str(raw_id).strip().lower()
    if not provider_id or len(provider_id) > 64:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Provider identifier must be between 1 and 64 characters",
        )

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
                    await _enforce_llm_url_guard(url_val)
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

            try:
                updates = ProviderUpdate(**update_kwargs)
                updated = await crud.update_provider(conn, provider_id, updates)
                return {"status": "success", "provider": updated.model_dump() if updated else None}
            except Exception as exc:
                safe_err = sanitize_error_detail(exc)
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail=f"Invalid provider parameters: {safe_err}",
                ) from exc
        else:
            # Preset default values if not provided
            preset = get_provider_preset(provider_id)
            name = provider_data.get("name") or (preset["name"] if preset else provider_id.capitalize())
            user_supplied_url = provider_data.get("api_base_url") or provider_data.get("base_url")
            if user_supplied_url:
                await _enforce_llm_url_guard(user_supplied_url)
            base_url = (
                user_supplied_url
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
            if is_masked_key(api_key):
                api_key = ""
            custom_headers = provider_data.get("custom_headers") or {}

            try:
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
            except Exception as exc:
                safe_err = sanitize_error_detail(exc)
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail=f"Invalid provider creation parameters: {safe_err}",
                ) from exc


@router.put(
    "/providers/{provider_id}",
    summary="Update Provider",
    description="Updates an existing provider configuration by ID.",
)
async def update_provider(provider_id: str, provider_data: Dict[str, Any]):
    data = dict(provider_data)
    data["id"] = provider_id
    return await create_or_update_provider(data)


@router.delete(
    "/providers/{provider_id}",
    summary="Delete Provider",
    description="Deletes a provider configuration by ID.",
)
async def delete_provider(provider_id: str):
    clean_id = (provider_id or "").strip().lower()
    if not clean_id or len(clean_id) > 64:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Provider '{provider_id}' not found")
    async with get_db() as conn:
        success = await crud.delete_provider(conn, clean_id)
        if not success:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Provider '{provider_id}' not found")
        return {"status": "deleted", "provider_id": clean_id}


@router.post(
    "/providers/{provider_id}/activate",
    summary="Set Active Provider",
    description="Sets specified provider as the active LLM provider.",
)
async def activate_provider(provider_id: str):
    clean_id = (provider_id or "").strip().lower()
    if not clean_id or len(clean_id) > 64:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Provider ID must be between 1 and 64 characters")
    async with get_db() as conn:
        existing = await crud.get_provider_raw(conn, clean_id)
        if not existing:
            preset = get_provider_preset(clean_id)
            if preset:
                new_provider = ProviderCreate(
                    id=clean_id,
                    name=preset["name"],
                    api_base_url=preset["default_base_url"],
                    api_key="",
                    chat_model=preset["default_chat_model"],
                    stt_model=preset["default_stt_model"],
                    is_active=True,
                )
                try:
                    await crud.create_provider(conn, new_provider)
                except (sqlite3.IntegrityError, Exception):
                    # Concurrently created by parallel activation request (TOCTOU safe)
                    pass
        success = await crud.set_active_provider(conn, clean_id)
        if not success:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Provider '{provider_id}' not found")
        active = await crud.get_active_provider(conn, mask=True)
        return {"status": "success", "active_provider": active.model_dump() if active else None}


@router.get(
    "/providers/{provider_id}/models",
    summary="Fetch Provider Model List",
    description="Fetches live available model list directly from the provider's /models API.",
)
async def get_provider_models(provider_id: str):
    clean_id = (provider_id or "").strip().lower()
    if not clean_id or len(clean_id) > 64:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Provider '{provider_id}' not found")
    async with get_db() as conn:
        stored = await crud.get_provider_raw(conn, clean_id)
        if not stored:
            preset = get_provider_preset(clean_id)
            if not preset:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Provider '{provider_id}' not found",
                )
            return {"provider_id": clean_id, "models": preset["preset_models"]}

        adapter = get_llm_adapter(
            provider_id_or_config=stored,
            api_key=stored.api_key,
            base_url=stored.api_base_url,
            custom_headers=stored.custom_headers,
        )

        try:
            models = await adapter.list_models()
            return {"provider_id": clean_id, "models": models}
        except Exception as exc:
            logger.warning("Failed to list models for provider %s: %s", clean_id, exc)
            preset = get_provider_preset(clean_id)
            fallback = preset["preset_models"] if preset else [stored.chat_model]
            return {"provider_id": clean_id, "models": fallback, "warning": sanitize_error_detail(exc)}


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
    # If model is not specified, resolve from preset defaults
    if not model:
        preset = get_provider_preset(provider_id)
        if preset:
            model = preset.get("default_chat_model")

    # If base_url is not specified, resolve from preset defaults
    if not base_url:
        preset = get_provider_preset(provider_id)
        if preset:
            base_url = preset.get("default_base_url")

    # SSRF guard: the effective base_url (explicit or stored) must pass the
    # private-network check before any credentials are attached to the request.
    await _enforce_llm_url_guard(base_url)

    # Instantiate adapter via factory
    adapter = get_llm_adapter(
        provider_id_or_config=provider_id,
        api_key=api_key,
        base_url=base_url,
        custom_headers=custom_headers,
    )

    try:
        result = await adapter.test_connection(model=model)
        if not result.success:
            diag_info = format_provider_error(
                provider_id=provider_id,
                status_code=getattr(result, "status_code", None),
                raw_error=result.error or result.message,
            )
            return ProviderTestResponse(
                success=False,
                message=result.message,
                latency_ms=result.latency_ms,
                models=result.models,
                error=result.error or diag_info.get("error") or result.message,
                diagnostic=result.diagnostic or diag_info.get("diagnostic"),
            )
        return ProviderTestResponse(
            success=result.success,
            message=result.message,
            latency_ms=result.latency_ms,
            models=result.models,
        )
    except Exception as exc:
        logger.error("Provider test failed for '%s': %s", provider_id, exc, exc_info=True)
        safe_msg = sanitize_error_detail(exc)
        diag_info = format_provider_error(
            provider_id=provider_id,
            status_code=504 if "timeout" in type(exc).__name__.lower() else 502,
            raw_error=f"{type(exc).__name__}: {safe_msg}",
        )
        return ProviderTestResponse(
            success=False,
            message=f"连接测试异常: {safe_msg}",
            latency_ms=0.0,
            models=[],
            error=diag_info.get("error") or safe_msg,
            diagnostic=diag_info.get("diagnostic"),
        )


test_provider_connectivity = test_provider


# ============================================================================
# Telegram Bot Testing Endpoints
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
    if req.proxy_enabled:
        # Build the proxy URL through the exact same code path the bot uses at
        # runtime, so "test passes" implies "runtime works".
        pseudo_settings = SettingsInDB(
            id=1,
            telegram_proxy_enabled=True,
            telegram_proxy_host=req.proxy_host or "127.0.0.1",
            telegram_proxy_port=req.proxy_port or 10809,
        )
        single = get_proxy_url(pseudo_settings)
        proxy_urls = [single] if single else [None]
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
    sanitized_err_msg = sanitize_error_detail(last_err)
    if "ConnectError" in str(type(last_err)) or "10061" in sanitized_err_msg or "refused" in sanitized_err_msg.lower():
        hint = "连接被拒绝。请检查代理端口是否填写正确（例如 v2rayN 常用 10808，Clash 常用 7890）且代理客户端处于运行状态。"
        return {
            "success": False,
            "message": f"代理连接失败: {hint}",
            "latency_ms": latency,
        }
    return {
        "success": False,
        "message": f"连接失败: {type(last_err).__name__} - {sanitized_err_msg}",
        "latency_ms": latency,
    }
