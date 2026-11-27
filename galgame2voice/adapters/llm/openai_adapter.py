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
    MissingApiKeyError,
    TestResult,
    TRANSIENT_STATUS_CODES,
    TRANSIENT_NETWORK_EXCEPTIONS,
    parse_retry_after,
    calculate_backoff_delay,
    parse_sse_lines,
    aclose_stream_context,
    validate_stream_completion,
    is_html_response,
)
from galgame2voice.utils.error_diagnostics import WebpageEndpointError, is_out_of_balance
from galgame2voice.utils.logger import sanitize_error_detail
from galgame2voice.utils.http_client import create_async_client, get_shared_async_client, is_local_or_lan_url

logger = logging.getLogger("galgame2voice.adapters.llm.openai")

# Parameters that are valid for OpenAI but rejected by Google Gemini's
# OpenAI-compatible endpoint (generativelanguage.googleapis.com).
_GEMINI_UNSUPPORTED_PARAMS = frozenset({
    "frequency_penalty", "presence_penalty", "logit_bias",
    "logprobs", "top_logprobs", "n", "seed", "user",
})

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


_CHAT_COMPLETIONS_SUFFIX = "/chat/completions"


def _strip_chat_completions_suffix(base_url: str) -> str:
    """Drop a pasted trailing /chat/completions, as the Anthropic adapter drops /messages."""
    url = base_url.rstrip("/")
    if url.lower().endswith(_CHAT_COMPLETIONS_SUFFIX):
        url = url[: -len(_CHAT_COMPLETIONS_SUFFIX)]
    return url


async def _mock_lines_iter(text: str) -> AsyncIterator[str]:
    for line in text.split("\n"):
        yield line


def _has_choices(resp: httpx.Response) -> bool:
    """A real chat completion: a JSON object with choices and no error."""
    try:
        data = resp.json()
    except ValueError:
        return False
    return isinstance(data, dict) and bool(data.get("choices")) and not data.get("error")


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
        self.last_finish_reason: str | None = None
        self.default_model = default_model or kwargs.get("chat_model") or kwargs.get("model")

    def _endpoint_base_url(self) -> str:
        return _strip_chat_completions_suffix(self.base_url)

    def _record_finish_reason(self, chunk: dict[str, Any]) -> None:
        choices = chunk.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict) and choices[0].get("finish_reason"):
            self.last_finish_reason = choices[0]["finish_reason"]

    def _resolve_test_model(self, model: str | None = None) -> str:
        """Resolves the test model: arg, then configured default, then preset/domain map, then OpenAI default."""
        if model and str(model).strip():
            return str(model).strip()
        from galgame2voice.adapters.registry import get_provider_preset, resolve_preset_default_model_by_url
        openai_default = get_provider_preset("openai")["default_chat_model"]
        default_model = str(getattr(self, "default_model", "") or "").strip()
        if default_model and default_model != openai_default:
            return default_model
        chat_model = self.extra_config.get("chat_model") or self.extra_config.get("model")
        if chat_model and str(chat_model).strip():
            return str(chat_model).strip()
        pid = self.extra_config.get("provider_id") or self.extra_config.get("provider_type") or getattr(self, "provider_id", None)
        if pid:
            try:
                preset = get_provider_preset(str(pid))
                if preset and preset.get("default_chat_model"):
                    return str(preset["default_chat_model"]).strip()
            except Exception as exc:
                logger.debug("Failed getting provider preset for default model: %s", exc)
        return resolve_preset_default_model_by_url(self.base_url) or openai_default

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
            raise MissingApiKeyError()

    @property
    def _is_gemini(self) -> bool:
        """Returns True if base_url points to Google Gemini's OpenAI-compat endpoint."""
        return "googleapis.com" in (self.base_url or "")

    def _filter_payload_kwargs(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        """Filters out kwargs that are internal or unsupported by the current provider."""
        _INTERNAL = {"client_override", "custom_headers", "timeout_s", "max_retries", "base_delay"}
        skip = _INTERNAL | (_GEMINI_UNSUPPORTED_PARAMS if self._is_gemini else frozenset())
        return {k: v for k, v in kwargs.items() if k not in skip}

    def _reasoning_fields(self, effort: str | None, model: str) -> dict[str, Any]:
        if effort is None:
            return {}
        return {"reasoning_effort": effort}

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
        reasoning_effort = (extra or {}).get("reasoning_effort", self.reasoning_effort)
        payload.update(self._reasoning_fields(reasoning_effort, model))
        if extra:
            for k, v in self._filter_payload_kwargs(extra).items():
                if k != "reasoning_effort":
                    payload[k] = v
        return payload

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
                self._build_payload(messages, model, temperature=temperature, stream=True, extra=kwargs),
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

            async for token in parse_sse_lines(_mock_lines_iter(resp.text), self._record_finish_reason):
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
        self.last_finish_reason = None
        client = kwargs.get("client_override")
        if client is not None:
            # Client provided by caller (e.g. test mock); execute directly without retry loop
            resp = await client.post(
                f"{self._endpoint_base_url()}/chat/completions",
                json=self._build_payload(messages, model, temperature=temperature, stream=True, extra=kwargs),
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
            if resp.status_code in TRANSIENT_STATUS_CODES:
                if resp.status_code == 429:
                    raise RuntimeError(f"Rate limit exceeded (429): {resp.text}")
                raise RuntimeError(f"API returned status {resp.status_code}: {resp.text}")
            if resp.status_code != 200:
                raise RuntimeError(f"API returned status {resp.status_code}: {resp.text}")

            async for token in parse_sse_lines(_mock_lines_iter(resp.text), self._record_finish_reason):
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

        url = f"{self._endpoint_base_url()}/chat/completions"
        payload = self._build_payload(messages, model, temperature=temperature, stream=True, extra=kwargs)

        timeout_s = float(self.extra_config.get("timeout_s", kwargs.get("timeout_s", 60.0)))
        headers = self._get_headers()
        headers["Accept"] = "text/event-stream"

        allow_private = bool(self.extra_config.get("allow_private", False))
        from galgame2voice.security.url_guard import assert_llm_url_safe
        await assert_llm_url_safe(url, allow_private=allow_private)

        for attempt in range(max_retries + 1):
            if attempt:
                await assert_llm_url_safe(url, allow_private=allow_private)
            client = get_shared_async_client(factory=create_async_client, trust_env=False) if is_local_or_lan_url(url) else get_shared_async_client(factory=create_async_client)
            stream_ctx = None
            try:
                stream_ctx = client.stream("POST", url, json=payload, headers=headers, timeout=timeout_s)
                response = await stream_ctx.__aenter__()
            except TRANSIENT_NETWORK_EXCEPTIONS as exc:
                await aclose_stream_context(stream_ctx)
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
                await aclose_stream_context(stream_ctx)
                raise

            if response.status_code in (401, 403):
                try:
                    error_body = await response.aread()
                finally:
                    await aclose_stream_context(stream_ctx)
                raise ValueError(f"Authentication error ({response.status_code}): {error_body.decode('utf-8', errors='ignore')}")

            if response.status_code in TRANSIENT_STATUS_CODES:
                try:
                    error_body = await response.aread()
                finally:
                    await aclose_stream_context(stream_ctx)
                retry_after = parse_retry_after(response.headers)
                out_of_balance = response.status_code == 429 and is_out_of_balance(
                    error_body.decode("utf-8", errors="ignore")
                )
                if attempt < max_retries and not out_of_balance:
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
                    await aclose_stream_context(stream_ctx)
                raise RuntimeError(f"API returned status {response.status_code}: {error_body.decode('utf-8', errors='ignore')}")

            if is_html_response(response):
                await aclose_stream_context(stream_ctx)
                raise WebpageEndpointError(url)

            yielded_any = False
            saw_done = False
            local_finish_reason: str | None = None

            def _on_done() -> None:
                nonlocal saw_done
                saw_done = True

            def _on_chunk(chunk: dict[str, Any]) -> None:
                nonlocal local_finish_reason
                self._record_finish_reason(chunk)
                choices = chunk.get("choices")
                if isinstance(choices, list) and choices and isinstance(choices[0], dict) and choices[0].get("finish_reason"):
                    local_finish_reason = choices[0]["finish_reason"]
            try:
                async for token in parse_sse_lines(response.aiter_lines(), _on_chunk, _on_done):
                    yielded_any = True
                    yield token

                validate_stream_completion(
                    yielded_any=yielded_any,
                    saw_terminal=saw_done,
                    finish_reason=local_finish_reason,
                )
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
                await aclose_stream_context(stream_ctx)

    @staticmethod
    def _diagnose_failure(
        provider_id: str | None,
        status_code: int | None,
        raw_error: Exception | str,
        *,
        api_key_configured: bool | None = None,
    ) -> tuple[dict[str, Any], str]:
        from galgame2voice.utils.error_diagnostics import format_provider_error
        diag = format_provider_error(
            provider_id=provider_id,
            status_code=status_code,
            raw_error=raw_error,
            api_key_configured=api_key_configured,
        )
        resolved_status = status_code if status_code is not None else diag.get("status_code")
        logger.warning(
            "Provider connectivity check failed (provider=%s, status=%s): %s",
            provider_id or "openai",
            resolved_status,
            sanitize_error_detail(raw_error),
        )
        return diag, diag.get("diagnostic", "")

    _MAX_TOKENS_KEY = "max_tokens"

    async def _probe_chat_fallback(
        self,
        client: httpx.AsyncClient,
        test_model: str,
        headers: dict[str, str],
        provider_id: str | None,
        t0: float,
    ) -> TestResult:
        """Fallback connectivity probe via 1-token chat completion when /models is unsupported."""
        chat_url = f"{self._endpoint_base_url()}/chat/completions"
        chat_resp = await client.post(
            chat_url,
            json={
                "model": test_model, "messages": [{"role": "user", "content": "ping"}], self._MAX_TOKENS_KEY: 1,
                **self._reasoning_fields(self.reasoning_effort, test_model),
            },
            headers=headers,
        )
        latency_ms = round((time.perf_counter() - t0) * 1000, 2)
        chat_text = chat_resp.text or ""
        if chat_resp.status_code == 200 and _has_choices(chat_resp):
            return TestResult(
                success=True,
                message=f"Connected successfully to {self.base_url}",
                latency_ms=latency_ms,
                models=[test_model] if test_model else None,
            )

        webpage = chat_resp.status_code == 200 and is_html_response(chat_resp)
        diag, diag_guide = self._diagnose_failure(
            provider_id,
            chat_resp.status_code,
            WebpageEndpointError(chat_url) if webpage else chat_text,
            api_key_configured=bool(self.api_key),
        )
        return TestResult(
            success=False,
            message=f"Provider test returned HTTP {chat_resp.status_code}: {diag_guide or chat_text[:200]}",
            latency_ms=latency_ms,
            error_code=diag.get("error_code"),
            error=diag.get("error", f"HTTP {chat_resp.status_code}"),
            diagnostic=diag_guide,
            status_code=diag.get("status_code") if diag.get("status_code") is not None else chat_resp.status_code,
        )

    async def test_connection(self, model: str | None = None) -> TestResult:
        """
        Tests connectivity and validates API credentials against the provider.
        """
        provider_id = (
            self.extra_config.get("provider_id")
            or self.extra_config.get("provider_type")
            or getattr(self, "provider_id", None)
        )
        if not self.api_key:
            diag, diag_guide = self._diagnose_failure(
                provider_id,
                None,
                "Authentication failed: API key is not configured",
                api_key_configured=False,
            )
            return TestResult(
                success=False,
                message="Authentication failed: Invalid API key",
                latency_ms=None,
                error_code=diag.get("error_code"),
                error=diag.get("error"),
                diagnostic=diag_guide,
                status_code=diag.get("status_code"),
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
        url = f"{self._endpoint_base_url()}/models"
        from galgame2voice.security.url_guard import assert_llm_url_safe
        await assert_llm_url_safe(url, allow_private=bool(self.extra_config.get("allow_private", False)))
        headers = self._get_headers()

        async with create_async_client(timeout=10.0, trust_env=not is_local_or_lan_url(url)) as client:
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
                    if not models and test_model:
                        return await self._probe_chat_fallback(client, test_model, headers, provider_id, t0)
                    return TestResult(
                        success=True,
                        message=f"Connected successfully to {self.base_url}",
                        latency_ms=latency_ms,
                        models=models if models else None,
                    )
                elif is_auth_error:
                    diag, diag_guide = self._diagnose_failure(
                        provider_id,
                        resp.status_code,
                        resp_text,
                        api_key_configured=bool(self.api_key),
                    )
                    return TestResult(
                        success=False,
                        message=f"Authentication failed ({resp.status_code}): {diag_guide or 'Invalid credentials'}",
                        latency_ms=latency_ms,
                        error_code=diag.get("error_code"),
                        error=diag.get("error", "Authentication failed"),
                        diagnostic=diag_guide,
                        status_code=diag.get("status_code") if diag.get("status_code") is not None else resp.status_code,
                    )
                else:
                    return await self._probe_chat_fallback(client, test_model, headers, provider_id, t0)
            except Exception as exc:
                latency_ms = round((time.perf_counter() - t0) * 1000, 2)
                diag, diag_guide = self._diagnose_failure(
                    provider_id,
                    getattr(exc, "status_code", None),
                    exc,
                    api_key_configured=bool(self.api_key),
                )
                return TestResult(
                    success=False,
                    message=f"Connection error: {type(exc).__name__} - {sanitize_error_detail(exc)}",
                    latency_ms=latency_ms,
                    error_code=diag.get("error_code"),
                    error=diag.get("error", type(exc).__name__),
                    diagnostic=diag_guide,
                    status_code=diag.get("status_code"),
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

        url = f"{self._endpoint_base_url()}/models"
        from galgame2voice.security.url_guard import assert_llm_url_safe
        await assert_llm_url_safe(url, allow_private=bool(self.extra_config.get("allow_private", False)))
        headers = self._get_headers()

        async with create_async_client(timeout=10.0, trust_env=not is_local_or_lan_url(url)) as client:
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


_OPENAI_REASONING_PREFIXES = ("gpt-5", "gpt-6", "o1", "o3", "o4")
_OPENAI_NO_NONE_EFFORT_PREFIXES = ("gpt-6.1-sol", "gpt-6-astra")
_OPENAI_REASONING_REJECTED_PARAMS = ("temperature", "top_p", "logprobs", "top_logprobs")


class OpenAILLMAdapter(OpenAICompatibleLLMAdapter):
    """Official OpenAI API Adapter (GPT-6 Luna, GPT-6.1 Sol)."""

    _MAX_TOKENS_KEY = "max_completion_tokens"

    def _reasoning_fields(self, effort: str | None, model: str) -> dict[str, Any]:
        if effort == "none" and model.strip().lower().startswith(_OPENAI_NO_NONE_EFFORT_PREFIXES):
            return {"reasoning_effort": "low"}
        return super()._reasoning_fields(effort, model)

    def _build_payload(
        self, messages: list[ChatMessage], model: str, temperature: float = 1.0,
        stream: bool = False, extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = super()._build_payload(messages, model, temperature=temperature, stream=stream, extra=extra)
        if model.strip().lower().startswith(_OPENAI_REASONING_PREFIXES) and payload.get("reasoning_effort") != "none":
            for key in _OPENAI_REASONING_REJECTED_PARAMS:
                payload.pop(key, None)
        if "max_tokens" in payload:
            payload["max_completion_tokens"] = payload.pop("max_tokens")
        return payload
