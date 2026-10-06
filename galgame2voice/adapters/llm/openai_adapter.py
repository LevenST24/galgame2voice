"""
OpenAI-compatible LLM Adapter implementation for galgame2voice.
Supports standard OpenAI REST endpoints, streaming SSE parsing, connection testing, and model discovery.
"""

import asyncio
import logging
import time
from typing import AsyncIterator, Any
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
    aclose_stream_context,
)
from galgame2voice.utils.logger import sanitize_error_detail
from galgame2voice.utils.http_client import create_async_client

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

# Default test models mapped by base URL substrings
_URL_DEFAULT_MODELS: tuple[tuple[str, str], ...] = (
    ("x.ai", "grok-3"),
    ("googleapis.com", "gemini-2.0-flash"),
    ("deepseek.com", "deepseek-chat"),
    ("bigmodel.cn", "glm-4-flash"),
    ("aliyuncs.com", "qwen-plus"),
    ("siliconflow.cn", "deepseek-ai/DeepSeek-V3"),
    ("anthropic.com", "claude-3-5-sonnet-20241022"),
)

_AUTH_ERROR_KEYWORDS: tuple[str, ...] = (
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


async def _mock_lines_iter(text: str) -> AsyncIterator[str]:
    for line in text.split("\n"):
        yield line


class OpenAICompatibleLLMAdapter(BaseLLMAdapter):
    """
    Adapter for OpenAI and OpenAI-compatible API providers (DeepSeek, Groq, Qwen, GLM, etc.).
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        client_override: Any | None = None,
        default_model: str | None = None,
        **kwargs: Any,
    ):
        super().__init__(api_key=api_key, base_url=base_url, **kwargs)
        self.mock_server = client_override
        self.default_model = default_model or kwargs.get("chat_model") or kwargs.get("model")

    def _resolve_test_model(self, model: str | None = None) -> str:
        """Resolves the test model: arg, then configured default, then provider preset/domain map, then gpt-4o-mini."""
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
            except Exception as exc:
                logger.debug("Failed getting provider preset for default model: %s", exc)
        burl = (self.base_url or "").lower()
        for domain_pattern, default_m in _URL_DEFAULT_MODELS:
            if domain_pattern in burl:
                return default_m
        return "gpt-4o-mini"

    def _get_headers(self) -> dict[str, str]:
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
        """Validates that an API key is present."""
        if not self.api_key:
            raise ValueError("Authentication error: Invalid API key")

    @property
    def _is_gemini(self) -> bool:
        """Returns True if base_url points to Google Gemini's OpenAI-compat endpoint."""
        return "googleapis.com" in (self.base_url or "")

    def _filter_payload_kwargs(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        """Filters out kwargs that are internal or unsupported by the current provider."""
        _INTERNAL = {"client_override", "custom_headers", "timeout_s", "max_retries", "base_delay"}
        skip = _INTERNAL | (_GEMINI_UNSUPPORTED_PARAMS if self._is_gemini else frozenset())
        return {k: v for k, v in kwargs.items() if k not in skip}

    def _build_payload(
        self,
        messages: list[ChatMessage],
        model: str,
        temperature: float = 1.0,
        stream: bool = False,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Formats chat messages and parameters into standard OpenAI completion payload."""
        payload: dict[str, Any] = {
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
        if stream:
            payload["stream"] = True
        if extra:
            for k, v in self._filter_payload_kwargs(extra).items():
                payload[k] = v
        return payload

    async def _chat_mock(
        self,
        messages: list[ChatMessage],
        model: str,
        temperature: float,
        max_retries: int,
        base_delay: float,
        kwargs: dict[str, Any],
    ) -> LLMResponse:
        """Executes non-streaming completion against test mock server with retry logic."""
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
        raise RuntimeError("Max retries exceeded without a response")

    async def chat(
        self,
        messages: list[ChatMessage],
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
            return await self._chat_mock(
                messages=messages,
                model=model,
                temperature=temperature,
                max_retries=max_retries,
                base_delay=base_delay,
                kwargs=kwargs,
            )

        url = f"{self.base_url}/chat/completions"
        payload = self._build_payload(messages, model, temperature=temperature, stream=False, extra=kwargs)

        timeout_s = float(self.extra_config.get("timeout_s", kwargs.get("timeout_s", 60.0)))
        headers = self._get_headers()

        allow_private = bool(self.extra_config.get("allow_private", False))
        from galgame2voice.security.url_guard import assert_llm_url_safe
        await assert_llm_url_safe(url, allow_private=allow_private)

        client = create_async_client(timeout=timeout_s)
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
            raise RuntimeError("Max retries exceeded without a response")
        finally:
            await client.aclose()

    async def _stream_chat_mock(
        self,
        messages: list[ChatMessage],
        model: str,
        temperature: float,
        max_retries: int,
        base_delay: float,
        kwargs: dict[str, Any],
    ) -> AsyncIterator[str]:
        """Executes streaming completion against test mock server with retry logic."""
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

            async for token in parse_sse_lines(_mock_lines_iter(resp.text)):
                yield token
            return

    async def stream_chat(
        self,
        messages: list[ChatMessage],
        model: str,
        temperature: float = 1.0,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        """
        Asynchronously streams text tokens via Server-Sent Events (SSE) with retries.
        """
        client = kwargs.get("client_override")
        if client is not None:
            # Client provided by caller (e.g. test mock); execute directly without retry loop
            resp = await client.post(
                f"{self.base_url}/chat/completions",
                json=self._build_payload(messages, model, temperature=temperature, stream=True),
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
            if resp.status_code in TRANSIENT_STATUS_CODES:
                if resp.status_code == 429:
                    raise RuntimeError(f"Rate limit exceeded (429): {resp.text}")
                raise RuntimeError(f"API returned status {resp.status_code}: {resp.text}")
            if resp.status_code != 200:
                raise RuntimeError(f"API returned status {resp.status_code}: {resp.text}")

            async for token in parse_sse_lines(_mock_lines_iter(resp.text)):
                yield token
            return
        self._validate_credentials()

        max_retries = int(kwargs.get("max_retries", self.extra_config.get("max_retries", 3)))
        default_base_delay = 0.01 if self.mock_server else 1.0
        base_delay = float(kwargs.get("base_delay", self.extra_config.get("base_delay", default_base_delay)))

        # Handle mock server for test environments
        if self.mock_server:
            async for token in self._stream_chat_mock(
                messages=messages,
                model=model,
                temperature=temperature,
                max_retries=max_retries,
                base_delay=base_delay,
                kwargs=kwargs,
            ):
                yield token
            return

        url = f"{self.base_url}/chat/completions"
        payload = self._build_payload(messages, model, temperature=temperature, stream=True, extra=kwargs)

        timeout_s = float(self.extra_config.get("timeout_s", kwargs.get("timeout_s", 60.0)))
        headers = self._get_headers()
        headers["Accept"] = "text/event-stream"

        allow_private = bool(self.extra_config.get("allow_private", False))
        from galgame2voice.security.url_guard import assert_llm_url_safe
        await assert_llm_url_safe(url, allow_private=allow_private)

        for attempt in range(max_retries + 1):
            client = create_async_client(timeout=timeout_s)
            stream_ctx = None
            try:
                stream_ctx = client.stream("POST", url, json=payload, headers=headers)
                response = await stream_ctx.__aenter__()
            except TRANSIENT_NETWORK_EXCEPTIONS as exc:
                await aclose_stream_context(stream_ctx, client)
                if attempt < max_retries:
                    delay = calculate_backoff_delay(attempt, base_delay)
                    logger.warning(
                        "Streaming connection error to %s (%s). Retrying (%d/%d) in %.2fs...",
                        url, exc, attempt + 1, max_retries, delay
                    )
                    await asyncio.sleep(delay)
                    continue
                raise RuntimeError(f"Streaming request failed to {url}: {exc}") from exc
            except BaseException:
                await aclose_stream_context(stream_ctx, client)
                raise

            if response.status_code in (401, 403):
                try:
                    error_body = await response.aread()
                finally:
                    await aclose_stream_context(stream_ctx, client)
                raise ValueError(f"Authentication error ({response.status_code}): {error_body.decode('utf-8', errors='ignore')}")

            if response.status_code in TRANSIENT_STATUS_CODES:
                try:
                    error_body = await response.aread()
                finally:
                    await aclose_stream_context(stream_ctx, client)
                retry_after = parse_retry_after(response.headers)
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
                try:
                    error_body = await response.aread()
                finally:
                    await aclose_stream_context(stream_ctx, client)
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
                await aclose_stream_context(stream_ctx, client)

    @staticmethod
    def _diagnose_failure(
        provider_id: str | None,
        status_code: int,
        raw_error: str,
    ) -> tuple[dict[str, Any], str]:
        from galgame2voice.utils.error_diagnostics import format_provider_error
        diag = format_provider_error(
            provider_id=provider_id,
            status_code=status_code,
            raw_error=raw_error,
        )
        return diag, diag.get("diagnostic", "")

    async def _probe_chat_fallback(
        self,
        client: httpx.AsyncClient,
        test_model: str,
        headers: dict[str, str],
        provider_id: str | None,
        t0: float,
    ) -> TestResult:
        """Fallback connectivity probe via 1-token chat completion when /models is unsupported."""
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

        diag, diag_guide = self._diagnose_failure(provider_id, chat_resp.status_code, chat_text)
        return TestResult(
            success=False,
            message=f"Provider test returned HTTP {chat_resp.status_code}: {diag_guide or chat_text[:200]}",
            latency_ms=latency_ms,
            error=diag.get("error", f"HTTP {chat_resp.status_code}"),
            diagnostic=diag_guide,
            status_code=chat_resp.status_code,
        )

    async def test_connection(self, model: str | None = None) -> TestResult:
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

        async with create_async_client(timeout=10.0) as client:
            try:
                resp = await client.get(url, headers=headers)
                latency_ms = round((time.perf_counter() - t0) * 1000, 2)
                resp_text = resp.text or ""
                resp_lower = resp_text.lower()

                # Intercept HTTP 400 auth errors: xAI returns 400 with "Incorrect API key provided"
                # and Gemini returns 400 with "API key not valid" or "Please pass a valid API key".
                is_auth_error = resp.status_code in (401, 403) or (
                    resp.status_code == 400
                    and any(kw in resp_lower for kw in _AUTH_ERROR_KEYWORDS)
                )

                if resp.status_code == 200:
                    models = []
                    try:
                        data = resp.json()
                        models = [m["id"] for m in data.get("data", []) if "id" in m]
                    except (ValueError, KeyError, TypeError):
                        pass
                    return TestResult(
                        success=True,
                        message=f"Connected successfully to {self.base_url}",
                        latency_ms=latency_ms,
                        models=models if models else None,
                    )
                elif is_auth_error:
                    diag, diag_guide = self._diagnose_failure(provider_id, resp.status_code, resp_text)
                    return TestResult(
                        success=False,
                        message=f"Authentication failed ({resp.status_code}): {diag_guide or 'Invalid credentials'}",
                        latency_ms=latency_ms,
                        error=diag.get("error", "Authentication failed"),
                        diagnostic=diag_guide,
                        status_code=resp.status_code,
                    )
                else:
                    return await self._probe_chat_fallback(client, test_model, headers, provider_id, t0)
            except Exception as exc:
                latency_ms = round((time.perf_counter() - t0) * 1000, 2)
                status_code_num = 504 if "timeout" in type(exc).__name__.lower() else 502
                diag, diag_guide = self._diagnose_failure(provider_id, status_code_num, str(exc))
                return TestResult(
                    success=False,
                    message=f"Connection error: {type(exc).__name__} - {sanitize_error_detail(exc)}",
                    latency_ms=latency_ms,
                    error=diag.get("error", type(exc).__name__),
                    diagnostic=diag_guide,
                    status_code=status_code_num,
                )

    async def list_models(self) -> list[str]:
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

        async with create_async_client(timeout=10.0) as client:
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

        # Provider does not support the model listing endpoint
        return []
