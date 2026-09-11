"""
OpenAI-compatible LLM Adapter implementation for galgame2voice.
Supports standard OpenAI REST endpoints, streaming SSE parsing, connection testing, and model discovery.
"""

import asyncio
import email.utils
import json
import logging
import random
import time
from typing import AsyncIterator, Dict, Any, List, Optional
import httpx

from galgame2voice.adapters.base import (
    BaseLLMAdapter,
    ChatMessage,
    LLMResponse,
    TestResult,
    TRANSIENT_STATUS_CODES,
    TRANSIENT_NETWORK_EXCEPTIONS,
    parse_retry_after,
    calculate_backoff_delay,
    parse_sse_lines,
)
from galgame2voice.utils.logger import sanitize_error_detail

logger = logging.getLogger("galgame2voice.adapters.llm.openai")

# Backward compatibility aliases
_parse_retry_after = parse_retry_after
_calculate_backoff_delay = calculate_backoff_delay

# Parameters that are valid for OpenAI but rejected by Google Gemini's
# OpenAI-compatible endpoint (generativelanguage.googleapis.com).
_GEMINI_UNSUPPORTED_PARAMS = frozenset({
    "frequency_penalty", "presence_penalty", "logit_bias",
    "logprobs", "top_logprobs", "n", "seed", "user",
})


class OpenAICompatibleLLMAdapter(BaseLLMAdapter):
    """
    Adapter for OpenAI and OpenAI-compatible API providers (DeepSeek, Groq, Qwen, GLM, etc.).
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        client_override: Optional[Any] = None,
        default_model: Optional[str] = None,
        **kwargs: Any,
    ):
        super().__init__(api_key=api_key, base_url=base_url, **kwargs)
        self.mock_server = client_override
        self.default_model = default_model or kwargs.get("chat_model") or kwargs.get("model")

    def _resolve_test_model(self, model: Optional[str] = None) -> str:
        """Resolves appropriate test model for connection testing without defaulting to gpt-4o-mini."""
        if model and str(model).strip():
            return str(model).strip()
        if getattr(self, "default_model", None) and str(self.default_model).strip():
            return str(self.default_model).strip()
        chat_model = self.extra_config.get("chat_model") or self.extra_config.get("model")
        if chat_model and str(chat_model).strip():
            return str(chat_model).strip()
        pid = self.extra_config.get("provider_id") or self.extra_config.get("provider_type") or getattr(self, "provider_id", None)
        if pid:
            try:
                from galgame2voice.adapters.registry import get_provider_preset
                preset = get_provider_preset(str(pid))
                if preset and preset.get("default_chat_model"):
                    return str(preset["default_chat_model"]).strip()
            except Exception:
                pass
        burl = (self.base_url or "").lower()
        if "x.ai" in burl:
            return "grok-3"
        if "googleapis.com" in burl:
            return "gemini-2.0-flash"
        if "deepseek.com" in burl:
            return "deepseek-chat"
        if "bigmodel.cn" in burl:
            return "glm-4-flash"
        if "aliyuncs.com" in burl:
            return "qwen-plus"
        if "siliconflow.cn" in burl:
            return "deepseek-ai/DeepSeek-V3"
        if "anthropic.com" in burl:
            return "claude-3-5-sonnet-20241022"
        return "gpt-4o-mini"

    def _get_headers(self) -> Dict[str, str]:
        """Constructs request headers including bearer auth and custom extra headers."""
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        custom_headers = self.extra_config.get("custom_headers")
        if isinstance(custom_headers, dict):
            headers.update(custom_headers)
        return headers

    def _validate_credentials(self) -> None:
        """Validates presence and syntax of API key."""
        if not self.api_key:
            raise ValueError("Authentication error: Invalid API key")

    @property
    def _is_gemini(self) -> bool:
        """Returns True if base_url points to Google Gemini's OpenAI-compat endpoint."""
        return "googleapis.com" in (self.base_url or "")

    def _filter_payload_kwargs(self, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        """Filters out kwargs that are internal or unsupported by the current provider."""
        _INTERNAL = {"client_override", "custom_headers", "timeout_s", "max_retries", "base_delay"}
        skip = _INTERNAL | (_GEMINI_UNSUPPORTED_PARAMS if self._is_gemini else frozenset())
        return {k: v for k, v in kwargs.items() if k not in skip}

    async def chat(
        self,
        messages: List[ChatMessage],
        model: str,
        temperature: float = 1.0,
        **kwargs: Any,
    ) -> LLMResponse:
        """
        Performs a non-streaming chat completion request with exponential backoff retries.
        """
        self._validate_credentials()

        max_retries = int(kwargs.get("max_retries", self.extra_config.get("max_retries", 3)))
        default_base_delay = 0.01 if self.mock_server else 1.0
        base_delay = float(kwargs.get("base_delay", self.extra_config.get("base_delay", default_base_delay)))

        # Handle mock server for test environments
        if self.mock_server:
            for attempt in range(max_retries + 1):
                resp = await self.mock_server.handle_chat_completion(
                    {
                        "messages": [m.model_dump() if hasattr(m, "model_dump") else m for m in messages],
                        "model": model,
                        "temperature": temperature,
                        **kwargs,
                    },
                    headers={"authorization": f"Bearer {self.api_key}"},
                )
                if resp.status_code in (401, 403):
                    raise ValueError(f"Authentication error: {resp.json() if hasattr(resp, 'json') else resp.text}")
                if resp.status_code in TRANSIENT_STATUS_CODES:
                    if attempt < max_retries:
                        retry_after = parse_retry_after(getattr(resp, "headers", None))
                        delay = calculate_backoff_delay(attempt, base_delay, retry_after)
                        logger.warning(
                            "Mock LLM chat received status %d. Retrying (%d/%d) in %.2fs...",
                            resp.status_code, attempt + 1, max_retries, delay
                        )
                        await asyncio.sleep(delay)
                        continue
                    if resp.status_code == 429:
                        raise RuntimeError(f"Rate limit exceeded (429): {resp.json() if hasattr(resp, 'json') else resp.text}")
                    raise RuntimeError(f"API returned status {resp.status_code}: {resp.json() if hasattr(resp, 'json') else resp.text}")
                if resp.status_code != 200:
                    raise RuntimeError(f"API returned status {resp.status_code}: {resp.json() if hasattr(resp, 'json') else resp.text}")
                data = resp.json()
                return LLMResponse(
                    content=data["choices"][0]["message"]["content"],
                    usage=data.get("usage"),
                )

        url = f"{self.base_url}/chat/completions"
        payload: Dict[str, Any] = {
            "model": model,
            "messages": [
                {
                    "role": m.role if hasattr(m, "role") else m.get("role"),
                    "content": m.content if hasattr(m, "content") else m.get("content"),
                }
                for m in messages
            ],
            "temperature": temperature,
        }
        for k, v in self._filter_payload_kwargs(kwargs).items():
            payload[k] = v

        timeout_s = float(self.extra_config.get("timeout_s", kwargs.get("timeout_s", 60.0)))
        headers = self._get_headers()

        client = httpx.AsyncClient(timeout=timeout_s)
        try:
            for attempt in range(max_retries + 1):
                try:
                    resp = await client.post(url, json=payload, headers=headers)
                except TRANSIENT_NETWORK_EXCEPTIONS as exc:
                    if attempt < max_retries:
                        delay = calculate_backoff_delay(attempt, base_delay)
                        logger.warning(
                            "Transient network error connecting to %s (%s: %s). Retrying (%d/%d) in %.2fs...",
                            url, type(exc).__name__, exc, attempt + 1, max_retries, delay
                        )
                        await asyncio.sleep(delay)
                        continue
                    raise RuntimeError(f"Network error connecting to {url}: {exc}") from exc

                if resp.status_code in (401, 403):
                    raise ValueError(f"Authentication error ({resp.status_code}): {resp.text}")

                if resp.status_code in TRANSIENT_STATUS_CODES:
                    if attempt < max_retries:
                        retry_after = parse_retry_after(resp.headers)
                        delay = calculate_backoff_delay(attempt, base_delay, retry_after)
                        logger.warning(
                            "LLM endpoint %s returned HTTP %d. Retrying (%d/%d) in %.2fs...",
                            url, resp.status_code, attempt + 1, max_retries, delay
                        )
                        await asyncio.sleep(delay)
                        continue
                    if resp.status_code == 429:
                        raise RuntimeError(f"Rate limit exceeded (429): {resp.text}")
                    raise RuntimeError(f"API returned status {resp.status_code}: {resp.text}")

                if resp.status_code != 200:
                    raise RuntimeError(f"API returned status {resp.status_code}: {resp.text}")

                try:
                    data = resp.json()
                    content = data["choices"][0]["message"]["content"]
                    usage = data.get("usage")
                    return LLMResponse(content=content, usage=usage)
                except Exception as exc:
                    raise RuntimeError(f"Failed to parse LLM response JSON: {exc} | Body: {resp.text[:200]}") from exc
        finally:
            await client.aclose()

    async def stream_chat(
        self,
        messages: List[ChatMessage],
        model: str,
        temperature: float = 1.0,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        """
        Streams Server-Sent Event (SSE) tokens from the provider with connection resilience.
        """
        self._validate_credentials()

        max_retries = int(kwargs.get("max_retries", self.extra_config.get("max_retries", 3)))
        default_base_delay = 0.01 if self.mock_server else 1.0
        base_delay = float(kwargs.get("base_delay", self.extra_config.get("base_delay", default_base_delay)))

        # Handle mock server for test environments
        if self.mock_server:
            for attempt in range(max_retries + 1):
                resp = await self.mock_server.handle_chat_completion(
                    {
                        "messages": [m.model_dump() if hasattr(m, "model_dump") else m for m in messages],
                        "model": model,
                        "temperature": temperature,
                        "stream": True,
                        **kwargs,
                    },
                    headers={"authorization": f"Bearer {self.api_key}"},
                )
                if resp.status_code in (401, 403):
                    raise ValueError(f"Authentication error: {resp.text}")
                if resp.status_code in TRANSIENT_STATUS_CODES:
                    if attempt < max_retries:
                        retry_after = parse_retry_after(getattr(resp, "headers", None))
                        delay = calculate_backoff_delay(attempt, base_delay, retry_after)
                        await asyncio.sleep(delay)
                        continue
                    if resp.status_code == 429:
                        raise RuntimeError(f"Rate limit exceeded (429): {resp.text}")
                    raise RuntimeError(f"API returned status {resp.status_code}: {resp.text}")
                if resp.status_code != 200:
                    raise RuntimeError(f"API returned status {resp.status_code}: {resp.text}")

                async def _mock_lines_iter():
                    for line in resp.text.split("\n"):
                        yield line

                async for token in parse_sse_lines(_mock_lines_iter()):
                    yield token
                return

        url = f"{self.base_url}/chat/completions"
        payload: Dict[str, Any] = {
            "model": model,
            "messages": [
                {
                    "role": m.role if hasattr(m, "role") else m.get("role"),
                    "content": m.content if hasattr(m, "content") else m.get("content"),
                }
                for m in messages
            ],
            "temperature": temperature,
            "stream": True,
        }
        for k, v in self._filter_payload_kwargs(kwargs).items():
            payload[k] = v

        timeout_s = float(self.extra_config.get("timeout_s", kwargs.get("timeout_s", 60.0)))
        headers = self._get_headers()
        headers["Accept"] = "text/event-stream"

        for attempt in range(max_retries + 1):
            client = httpx.AsyncClient(timeout=timeout_s)
            stream_ctx = None
            try:
                stream_ctx = client.stream("POST", url, json=payload, headers=headers)
                response = await stream_ctx.__aenter__()
            except TRANSIENT_NETWORK_EXCEPTIONS as exc:
                if stream_ctx:
                    try:
                        await stream_ctx.__aexit__(None, None, None)
                    except Exception:
                        pass
                await client.aclose()
                if attempt < max_retries:
                    delay = calculate_backoff_delay(attempt, base_delay)
                    logger.warning(
                        "Streaming connection error to %s (%s). Retrying (%d/%d) in %.2fs...",
                        url, exc, attempt + 1, max_retries, delay
                    )
                    await asyncio.sleep(delay)
                    continue
                raise RuntimeError(f"Streaming request failed to {url}: {exc}") from exc

            if response.status_code in (401, 403):
                error_body = await response.aread()
                await stream_ctx.__aexit__(None, None, None)
                await client.aclose()
                raise ValueError(f"Authentication error ({response.status_code}): {error_body.decode('utf-8', errors='ignore')}")

            if response.status_code in TRANSIENT_STATUS_CODES:
                error_body = await response.aread()
                retry_after = parse_retry_after(response.headers)
                await stream_ctx.__aexit__(None, None, None)
                await client.aclose()
                if attempt < max_retries:
                    delay = calculate_backoff_delay(attempt, base_delay, retry_after)
                    logger.warning(
                        "Streaming endpoint %s returned HTTP %d. Retrying (%d/%d) in %.2fs...",
                        url, response.status_code, attempt + 1, max_retries, delay
                    )
                    await asyncio.sleep(delay)
                    continue
                if response.status_code == 429:
                    raise RuntimeError(f"Rate limit exceeded (429): {error_body.decode('utf-8', errors='ignore')}")
                raise RuntimeError(f"API returned status {response.status_code}: {error_body.decode('utf-8', errors='ignore')}")

            if response.status_code != 200:
                error_body = await response.aread()
                await stream_ctx.__aexit__(None, None, None)
                await client.aclose()
                raise RuntimeError(f"API returned status {response.status_code}: {error_body.decode('utf-8', errors='ignore')}")

            yielded_any = False
            try:
                async for token in parse_sse_lines(response.aiter_lines()):
                    yielded_any = True
                    yield token
                return
            except TRANSIENT_NETWORK_EXCEPTIONS as exc:
                if not yielded_any and attempt < max_retries:
                    delay = calculate_backoff_delay(attempt, base_delay)
                    logger.warning(
                        "Transient network error reading stream from %s (%s). Retrying (%d/%d) in %.2fs...",
                        url, exc, attempt + 1, max_retries, delay
                    )
                    await asyncio.sleep(delay)
                    continue
                raise RuntimeError(f"Streaming request failed to {url}: {exc}") from exc
            finally:
                await stream_ctx.__aexit__(None, None, None)
                await client.aclose()

    async def test_connection(self, model: Optional[str] = None) -> TestResult:
        """
        Tests connectivity and validates API credentials against the provider.
        """
        if not self.api_key:
            return TestResult(
                success=False,
                message="Authentication failed: Invalid API key",
                latency_ms=None,
            )

        if self.mock_server:
            if self.mock_server.force_error_code:
                return TestResult(
                    success=False,
                    message=f"Mocked error ({self.mock_server.force_error_code}): {self.mock_server.force_error_message}",
                    latency_ms=None,
                )
            return TestResult(
                success=True,
                message=f"Connected successfully to {self.base_url}",
                latency_ms=35.0,
                models=self.mock_server.simulated_models,
            )

        t0 = time.perf_counter()
        test_model = self._resolve_test_model(model)
        url = f"{self.base_url}/models"
        headers = self._get_headers()
        provider_id = (
            self.extra_config.get("provider_id")
            or self.extra_config.get("provider_type")
            or getattr(self, "provider_id", None)
        )

        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                resp = await client.get(url, headers=headers)
                latency_ms = round((time.perf_counter() - t0) * 1000, 2)
                resp_text = resp.text or ""
                resp_lower = resp_text.lower()

                # Intercept HTTP 400 auth errors: xAI returns 400 with "Incorrect API key provided"
                # and Gemini returns 400 with "API key not valid" or "Please pass a valid API key".
                is_auth_error = resp.status_code in (401, 403) or (
                    resp.status_code == 400
                    and any(
                        kw in resp_lower
                        for kw in (
                            "api key",
                            "apikey",
                            "unauthorized",
                            "invalid key",
                            "incorrect api key",
                            "valid api key",
                            "invalid-argument",
                            "invalid_argument",
                            "authentication",
                        )
                    )
                )

                if resp.status_code == 200:
                    models = []
                    try:
                        data = resp.json()
                        models = [m["id"] for m in data.get("data", []) if "id" in m]
                    except Exception:
                        pass
                    return TestResult(
                        success=True,
                        message=f"Connected successfully to {self.base_url}",
                        latency_ms=latency_ms,
                        models=models if models else None,
                    )
                elif is_auth_error:
                    from galgame2voice.utils.error_diagnostics import format_provider_error
                    diag = format_provider_error(
                        provider_id=provider_id,
                        status_code=resp.status_code,
                        raw_error=resp_text,
                    )
                    diag_guide = diag.get("diagnostic", "")
                    return TestResult(
                        success=False,
                        message=f"Authentication failed ({resp.status_code}): {diag_guide or 'Invalid credentials'}",
                        latency_ms=latency_ms,
                        error=diag.get("error", "Authentication failed"),
                        diagnostic=diag_guide,
                        status_code=resp.status_code,
                    )
                else:
                    # Fallback probe via chat completion
                    chat_url = f"{self.base_url}/chat/completions"
                    chat_resp = await client.post(
                        chat_url,
                        json={"model": test_model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1},
                        headers=headers,
                    )
                    latency_ms = round((time.perf_counter() - t0) * 1000, 2)
                    chat_text = chat_resp.text or ""
                    if chat_resp.status_code == 200:
                        return TestResult(
                            success=True,
                            message=f"Connected successfully to {self.base_url}",
                            latency_ms=latency_ms,
                        )

                    from galgame2voice.utils.error_diagnostics import format_provider_error
                    diag = format_provider_error(
                        provider_id=provider_id,
                        status_code=chat_resp.status_code,
                        raw_error=chat_text,
                    )
                    diag_guide = diag.get("diagnostic", "")
                    return TestResult(
                        success=False,
                        message=f"Provider test returned HTTP {chat_resp.status_code}: {diag_guide or chat_text[:200]}",
                        latency_ms=latency_ms,
                        error=diag.get("error", f"HTTP {chat_resp.status_code}"),
                        diagnostic=diag_guide,
                        status_code=chat_resp.status_code,
                    )
            except Exception as exc:
                latency_ms = round((time.perf_counter() - t0) * 1000, 2)
                from galgame2voice.utils.error_diagnostics import format_provider_error
                status_code_num = 504 if "timeout" in type(exc).__name__.lower() else 502
                diag = format_provider_error(
                    provider_id=provider_id,
                    status_code=status_code_num,
                    raw_error=str(exc),
                )
                diag_guide = diag.get("diagnostic", "")
                return TestResult(
                    success=False,
                    message=f"Connection error: {type(exc).__name__} - {sanitize_error_detail(exc)}",
                    latency_ms=latency_ms,
                    error=diag.get("error", type(exc).__name__),
                    diagnostic=diag_guide,
                    status_code=status_code_num,
                )

    async def list_models(self) -> List[str]:
        """
        Fetches the available model list from the provider API.
        """
        self._validate_credentials()

        if self.mock_server:
            resp = await self.mock_server.handle_models_list()
            if resp.status_code != 200:
                raise RuntimeError(f"Failed to list models: status {resp.status_code}")
            data = resp.json()
            return [m["id"] for m in data.get("data", []) if "id" in m]

        url = f"{self.base_url}/models"
        headers = self._get_headers()

        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                resp = await client.get(url, headers=headers)
                if resp.status_code in (401, 403):
                    raise ValueError(f"Authentication error listing models: {resp.text}")
                if resp.status_code == 200:
                    data = resp.json()
                    models = [m["id"] for m in data.get("data", []) if "id" in m]
                    if models:
                        return models
            except (httpx.RequestError, ValueError) as exc:
                if isinstance(exc, ValueError):
                    raise
                pass

        # Provider does not support the model listing endpoint
        return []
