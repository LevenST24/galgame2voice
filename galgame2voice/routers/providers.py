"""
Provider and Connectivity Testing Router for galgame2voice.
Provides REST endpoints for LLM/STT provider configurations
and in-flight connectivity testing.
"""

import asyncio
import logging
import sqlite3
from typing import Any, Literal

import aiosqlite
from fastapi import APIRouter, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from galgame2voice.adapters.registry import (
    get_llm_adapter,
    get_provider_preset,
    list_builtin_provider_presets,
    list_provider_presets,
)
from galgame2voice.database import crud
from galgame2voice.database.crud import is_masked_key
from galgame2voice.database.models import (
    ProviderCreate,
    ProviderUpdate,
)
from galgame2voice.database.session import get_db
from galgame2voice.security import url_guard
from galgame2voice.utils.error_diagnostics import (
    format_provider_error,
    is_retryable_diagnostic_code,
    normalize_diagnostic_code,
)
from galgame2voice.utils.logger import sanitize_error_detail

logger = logging.getLogger("galgame2voice.routers.providers")
router = APIRouter(tags=["Providers & Testing"])
providers_router = router


async def _allow_private_endpoints() -> bool:
    async with get_db() as conn:
        settings = await crud.get_settings_raw(conn)
    return bool(getattr(settings, "allow_private_llm_endpoints", False))


async def _enforce_llm_url_guard(base_url: str | None) -> bool:
    """Rejects LLM provider base URLs that resolve to loopback/private ranges
    unless the operator explicitly enabled private endpoints."""
    if not base_url:
        return False
    allow_private = await _allow_private_endpoints()
    ok, reason = await asyncio.to_thread(url_guard.validate_llm_base_url, base_url, allow_private)
    if not ok:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=reason)
    return allow_private


class ProviderTestRequest(BaseModel):
    """Payload for real-time provider connectivity and credential testing."""
    provider_type: str | None = None
    id: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    api_base_url: str | None = None
    model: str | None = None
    chat_model: str | None = None
    custom_headers: dict[str, Any] | None = None
    reasoning_effort: Literal["none", "low", "medium", "high"] | None = None


class ProviderTestResponse(BaseModel):
    """Response of provider connectivity and latency test."""
    __test__ = False
    success: bool
    message: str
    latency_ms: float | None = None
    models: list[str] | None = None
    error_code: str | None = None
    error_title: str | None = None
    diagnostic: str | None = None
    status_code: int | None = None
    error: str | None = None
    diagnostic_code: str | None = None
    summary_zh: str | None = None
    action_zh: str | None = None
    retryable: bool | None = None


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
        builtin_presets = list_builtin_provider_presets()
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
                    "reasoning_effort": pr.get("default_reasoning_effort"),
                })
        return {
            "providers": all_providers,
            "presets": presets,
            "builtin_presets": builtin_presets,
        }


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
                        "reasoning_effort": preset.get("default_reasoning_effort"),
                    }
                }
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Provider '{provider_id}' not found")
        return {"provider": provider.model_dump()}


async def _update_existing_provider(
    conn: Any, provider_id: str, provider_data: dict[str, Any]
) -> dict[str, Any]:
    """Updates an existing LLM provider configuration with validation."""
    update_kwargs: dict[str, Any] = {}
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
    if "reasoning_effort" in provider_data:
        update_kwargs["reasoning_effort"] = provider_data["reasoning_effort"]

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


async def _create_new_provider(
    conn: Any, provider_id: str, provider_data: dict[str, Any]
) -> dict[str, Any]:
    """Creates a new LLM provider record populated with preset defaults."""
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
        or (preset or get_provider_preset("openai"))["default_chat_model"]
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
    reasoning_effort = provider_data.get("reasoning_effort")
    if "reasoning_effort" not in provider_data and preset:
        reasoning_effort = preset.get("default_reasoning_effort")

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
            reasoning_effort=reasoning_effort,
        )
        created = await crud.create_provider(conn, new_provider)
        return {"status": "created", "provider": created.model_dump()}
    except Exception as exc:
        safe_err = sanitize_error_detail(exc)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Invalid provider creation parameters: {safe_err}",
        ) from exc


@router.post(
    "/providers",
    summary="Create or Update Provider",
    description="Upserts an LLM/STT provider profile, safely retaining existing secret keys if masked.",
)
async def create_or_update_provider(provider_data: dict[str, Any]):
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
            return await _update_existing_provider(conn, provider_id, provider_data)
        return await _create_new_provider(conn, provider_id, provider_data)


@router.put(
    "/providers/{provider_id}",
    summary="Update Provider",
    description="Updates an existing provider configuration by ID.",
)
async def update_provider(provider_id: str, provider_data: dict[str, Any]):
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
                    reasoning_effort=preset.get("default_reasoning_effort"),
                )
                try:
                    await crud.create_provider(conn, new_provider)
                except (sqlite3.IntegrityError, aiosqlite.IntegrityError):
                    # Concurrently created by parallel activation request (TOCTOU safe)
                    pass
                except Exception as exc:
                    logger.debug("Non-critical: error while creating preset provider: %s", exc)
        success = await crud.set_active_provider(conn, clean_id)
        if not success:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Provider '{provider_id}' not found")
        active = await crud.get_active_provider(conn, mask=True)
        return {"status": "success", "active_provider": active.model_dump() if active else None}


async def _resolve_provider_test_credentials(
    provider_id: str,
    api_key: str,
    base_url: str | None,
) -> tuple[str, str | None]:
    """Resolves unmasked API key and base URL from database; the SSRF guard runs in the caller."""
    if not api_key or "****" in api_key:
        async with get_db() as conn:
            stored = await crud.get_provider_raw(conn, provider_id)
            if stored and stored.api_key:
                # Credential leak guard: stored keys may only be sent to the base_url they were saved with
                if base_url and stored.api_base_url and base_url.rstrip("/") != stored.api_base_url.rstrip("/"):
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="Cannot test custom base_url with stored API key. Please provide the explicit API key."
                    )
                api_key = stored.api_key
                if not base_url:
                    base_url = stored.api_base_url
    return api_key, base_url


def _resolve_provider_preset_defaults(
    provider_id: str,
    model: str | None,
    base_url: str | None,
) -> tuple[str | None, str | None]:
    """Fills missing model or base_url from predefined provider presets."""
    if not model or not base_url:
        preset = get_provider_preset(provider_id)
        if preset:
            if not model:
                model = preset.get("default_chat_model")
            if not base_url:
                base_url = preset.get("default_base_url")
    return model, base_url


def _preflight_bad_request_response(
    *,
    detail: str,
    diagnostic_code: str,
    summary_zh: str,
    action_zh: str,
) -> JSONResponse:
    """Builds a structured HTTP 400 response for preflight provider test rejections while preserving `detail`."""
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={
            "detail": detail,
            "success": False,
            "message": detail,
            "latency_ms": 0.0,
            "models": [],
            "error_code": diagnostic_code,
            "error_title": summary_zh,
            "diagnostic": action_zh,
            "status_code": 400,
            "error": summary_zh,
            "diagnostic_code": diagnostic_code,
            "summary_zh": summary_zh,
            "action_zh": action_zh,
            "retryable": False,
        },
    )


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
    reasoning_effort = req.reasoning_effort
    if "reasoning_effort" not in req.model_fields_set:
        async with get_db() as conn:
            stored = await crud.get_provider_raw(conn, provider_id)
            if stored:
                reasoning_effort = stored.reasoning_effort

    try:
        api_key, base_url = await _resolve_provider_test_credentials(provider_id, api_key, base_url)
    except HTTPException as exc:
        return _preflight_bad_request_response(
            detail=str(exc.detail),
            diagnostic_code="CREDENTIAL_HOST_MISMATCH",
            summary_zh="已保存密钥不能直接发往新的接口地址",
            action_zh="更换接口地址（Base URL）测试时，请重新输入完整的 API Key，避免将原服务商密钥泄露到新地址。",
        )

    model, base_url = _resolve_provider_preset_defaults(provider_id, model, base_url)

    # SSRF guard: the effective base_url (explicit or stored) must pass the
    # private-network check before any credentials are attached to the request.
    try:
        allow_private = await _enforce_llm_url_guard(base_url)
    except HTTPException as exc:
        detail_str = str(exc.detail)
        if "允许私网 LLM 端点" in detail_str:
            return _preflight_bad_request_response(
                detail=detail_str,
                diagnostic_code="PRIVATE_ENDPOINT_BLOCKED",
                summary_zh="私网或本机模型地址未启用",
                action_zh=(
                    "如确需连接本机或局域网模型服务（如 Ollama、vLLM、LM Studio），"
                    "请在设置中开启「允许私网 LLM 端点」后重试；否则请改用公网 HTTPS 接口地址。"
                ),
            )
        return _preflight_bad_request_response(
            detail=detail_str,
            diagnostic_code="INVALID_BASE_URL",
            summary_zh="接口地址格式或协议无效",
            action_zh=f"{detail_str}。请检查 Base URL 配置，填写合法的 http:// 或 https:// 接口地址后重试。",
        )

    # Instantiate adapter via factory
    adapter = get_llm_adapter(
        provider_id_or_config=provider_id,
        api_key=api_key,
        base_url=base_url,
        custom_headers=custom_headers,
        allow_private=allow_private,
        reasoning_effort=reasoning_effort,
    )

    try:
        result = await adapter.test_connection(model=model)
        if not result.success:
            diag_info: dict[str, Any] = {}
            if not (result.error_code and result.error and result.diagnostic):
                diag_info = format_provider_error(
                    provider_id=provider_id,
                    status_code=result.status_code,
                    raw_error=result.error or result.message,
                    api_key_configured=bool(str(getattr(adapter, "api_key", "") or "").strip()),
                )
            error_code = result.error_code or diag_info.get("error_code")
            error_title = (result.error if result.error_code else None) or diag_info.get("error") or "连通性测试未通过"
            diagnostic = result.diagnostic or diag_info.get("diagnostic") or result.message
            status_code = result.status_code if result.status_code is not None else diag_info.get("status_code")
            diagnostic_code = normalize_diagnostic_code(error_code)
            return ProviderTestResponse(
                success=False,
                message=result.message,
                latency_ms=result.latency_ms,
                models=result.models,
                error_code=error_code,
                error_title=error_title,
                diagnostic=diagnostic,
                status_code=status_code,
                error=error_title,
                diagnostic_code=diagnostic_code,
                summary_zh=error_title,
                action_zh=diagnostic,
                retryable=is_retryable_diagnostic_code(diagnostic_code),
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
            status_code=getattr(exc, "status_code", None),
            raw_error=exc,
            api_key_configured=bool(str(getattr(adapter, "api_key", "") or "").strip()),
        )
        return ProviderTestResponse(
            success=False,
            message=f"连接测试异常: {safe_msg}",
            latency_ms=0.0,
            models=[],
            error_code=diag_info["error_code"],
            error_title=diag_info["error"],
            diagnostic=diag_info["diagnostic"],
            status_code=diag_info["status_code"],
            error=diag_info["error"],
            diagnostic_code=diag_info["diagnostic_code"],
            summary_zh=diag_info["summary_zh"],
            action_zh=diag_info["action_zh"],
            retryable=diag_info["retryable"],
        )


test_provider_connectivity = test_provider
