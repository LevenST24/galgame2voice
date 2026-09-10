"""
Anthropic Claude Native LLM Adapter for galgame2voice.
Implements native Anthropic /v1/messages API with x-api-key headers,
system parameter separation, content_block_delta streaming, and exponential backoff retry.
"""

import asyncio
import email.utils
import json
import logging
import random
import time
from typing import Any, AsyncGenerator, Dict, List, Optional

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

logger = logging.getLogger("galgame2voice.adapters.llm.anthropic")

# Backward compatibility aliases
_parse_retry_after = parse_retry_after
_calculate_backoff_delay = calculate_backoff_delay


class AnthropicAdapter(BaseLLMAdapter):
    """
    Adapter for Anthropic Claude native /v1/messages API.
    """

    DEFAULT_BASE_URL = "https://api.anthropic.com/v1"
    DEFAULT_MODEL = "claude-sonnet-4-20250514"
    ANTHROPIC_VERSION = "2023-06-01"

    def __init__(
        self,
        api_key: str = "",
        base_url: Optional[str] = None,
        default_model: str = DEFAULT_MODEL,
        custom_headers: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ):
        raw_url = base_url or self.DEFAULT_BASE_URL
        super().__init__(
            api_key=api_key,
            base_url=raw_url.rstrip("/"),
            default_model=default_model,
            custom_headers=custom_headers,
            **kwargs,
        )
        # BaseLLMAdapter stores extra kwargs in extra_config; set these explicitly.
        self.default_model = default_model
        self.custom_headers = custom_headers or {}

    def _get_headers(self) -> Dict[str, str]:
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": self.ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
        if self.custom_headers:
            for k, v in self.custom_headers.items():
                headers[str(k)] = str(v)
        return headers

    def _prepare_anthropic_payload(
        self,
        messages: List[ChatMessage],
        model: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 1024,
        stream: bool = False,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Separates system prompt and formats messages for Anthropic API."""
        system_content = ""
        user_assistant_msgs: List[Dict[str, str]] = []

        for m in messages:
            role = m.role if hasattr(m, "role") else m.get("role")
            content = m.content if hasattr(m, "content") else m.get("content")
            if role == "system":
                system_content = (system_content + "\n" + content).strip() if system_content else content
            else:
                user_assistant_msgs.append({
                    "role": "user" if role == "user" else "assistant",
                    "content": content,
                })

        # Anthropic requires:
        # 1. Non-empty string for each message content (>= 1 char)
        # 2. Alternating user/assistant roles (consecutive same roles must be merged)
        # 3. First message must be 'user'
        merged_msgs: List[Dict[str, str]] = []
        for m in user_assistant_msgs:
            role = m["role"]
            raw_content = m.get("content") or ""
            content = str(raw_content).strip()
            if not content:
                content = "..."

            if merged_msgs and merged_msgs[-1]["role"] == role:
                merged_msgs[-1]["content"] += "\n\n" + content
            else:
                merged_msgs.append({"role": role, "content": content})

        if not merged_msgs:
            merged_msgs.append({"role": "user", "content": "Hello"})
        elif merged_msgs[0]["role"] != "user":
            merged_msgs.insert(0, {"role": "user", "content": "Hello"})

        payload: Dict[str, Any] = {
            "model": model or self.default_model,
            "messages": merged_msgs,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": stream,
        }
        if system_content:
            payload["system"] = system_content

        # Anthropic API 不支持这些 OpenAI 风格参数，透传会导致 400
        _UNSUPPORTED = ("frequency_penalty", "presence_penalty", "top_k")
        for k, v in kwargs.items():
            if k not in ("client_override", "custom_headers", "timeout_s", "max_retries", "base_delay") and k not in _UNSUPPORTED:
                payload[k] = v

        return payload

    async def chat(
        self,
        messages: List[ChatMessage],
        model: Optional[str] = None,
        temperature: float = 0.7,
        **kwargs: Any,
    ) -> LLMResponse:
        """Executes non-streaming chat completion via Anthropic /messages endpoint with exponential retries."""
        url = f"{self.base_url}/messages" if not self.base_url.endswith("/messages") else self.base_url
        payload = self._prepare_anthropic_payload(
            messages, model=model, temperature=temperature, stream=False, **kwargs
        )
        headers = self._get_headers()
        timeout_s = float(self.extra_config.get("timeout_s", kwargs.get("timeout_s", 60.0)))

        max_retries = int(kwargs.get("max_retries", self.extra_config.get("max_retries", 3)))
        client_override = kwargs.get("client_override") or getattr(self, "mock_server", None) or getattr(self, "client_override", None)
        default_base_delay = 0.01 if client_override else 1.0
        base_delay = float(kwargs.get("base_delay", self.extra_config.get("base_delay", default_base_delay)))

        if client_override:
            for attempt in range(max_retries + 1):
                resp = await client_override.post(url, json=payload, headers=headers)
                if resp.status_code in (401, 403):
                    raise ValueError(f"Anthropic authentication failed ({resp.status_code}): {resp.text}")
                if resp.status_code in TRANSIENT_STATUS_CODES:
                    if attempt < max_retries:
                        retry_after = parse_retry_after(getattr(resp, "headers", None))
                        delay = calculate_backoff_delay(attempt, base_delay, retry_after)
                        logger.warning("Anthropic client_override returned %d. Retrying (%d/%d) in %.2fs...", resp.status_code, attempt + 1, max_retries, delay)
                        await asyncio.sleep(delay)
                        continue
                    raise RuntimeError(f"Anthropic API returned {resp.status_code}: {resp.text}")
                if resp.status_code != 200:
                    raise RuntimeError(f"Anthropic API returned {resp.status_code}: {resp.text}")
                data = resp.json()
                content = ""
                for block in data.get("content", []):
                    if block.get("type") == "text":
                        content += block.get("text", "")
                return LLMResponse(content=content, usage=None)

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
                    raise ValueError(f"Anthropic authentication failed ({resp.status_code}): {resp.text}")

                if resp.status_code in TRANSIENT_STATUS_CODES:
                    if attempt < max_retries:
                        retry_after = parse_retry_after(resp.headers)
                        delay = calculate_backoff_delay(attempt, base_delay, retry_after)
                        logger.warning(
                            "Anthropic API returned HTTP %d. Retrying (%d/%d) in %.2fs...",
                            resp.status_code, attempt + 1, max_retries, delay
                        )
                        await asyncio.sleep(delay)
                        continue
                    raise RuntimeError(f"Anthropic API error ({resp.status_code}): {resp.text}")

                if resp.status_code != 200:
                    raise RuntimeError(f"Anthropic API error ({resp.status_code}): {resp.text}")

                data = resp.json()
                content = ""
                for block in data.get("content", []):
                    if block.get("type") == "text":
                        content += block.get("text", "")
                return LLMResponse(content=content, usage=None)
        finally:
            await client.aclose()

    async def stream_chat(
        self,
        messages: List[ChatMessage],
        model: Optional[str] = None,
        temperature: float = 0.7,
        **kwargs: Any,
    ) -> AsyncGenerator[str, None]:
        """Streams token chunks from Anthropic SSE /messages stream with connection resilience."""
        url = f"{self.base_url}/messages" if not self.base_url.endswith("/messages") else self.base_url
        payload = self._prepare_anthropic_payload(
            messages, model=model, temperature=temperature, stream=True, **kwargs
        )
        headers = self._get_headers()
        headers["Accept"] = "text/event-stream"
        timeout_s = float(self.extra_config.get("timeout_s", kwargs.get("timeout_s", 60.0)))

        max_retries = int(kwargs.get("max_retries", self.extra_config.get("max_retries", 3)))
        client_override = kwargs.get("client_override") or getattr(self, "mock_server", None) or getattr(self, "client_override", None)
        default_base_delay = 0.01 if client_override else 1.0
        base_delay = float(kwargs.get("base_delay", self.extra_config.get("base_delay", default_base_delay)))

        if client_override:
            for attempt in range(max_retries + 1):
                resp = await client_override.post(url, json=payload, headers=headers)
                if resp.status_code in (401, 403):
                    raise ValueError(f"Anthropic authentication failed ({resp.status_code}): {resp.text}")
                if resp.status_code in TRANSIENT_STATUS_CODES:
                    if attempt < max_retries:
                        retry_after = parse_retry_after(getattr(resp, "headers", None))
                        delay = calculate_backoff_delay(attempt, base_delay, retry_after)
                        await asyncio.sleep(delay)
                        continue
                    raise RuntimeError(f"Anthropic API returned status {resp.status_code}: {resp.text}")
                if resp.status_code != 200:
                    raise RuntimeError(f"Anthropic API returned status {resp.status_code}: {resp.text}")

                async def _mock_lines_iter():
                    for line in resp.text.split("\n"):
                        yield line

                async for token in parse_sse_lines(_mock_lines_iter()):
                    yield token
                return

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
                err_body = await response.aread()
                await stream_ctx.__aexit__(None, None, None)
                await client.aclose()
                raise ValueError(f"Anthropic auth failed ({response.status_code}): {err_body.decode('utf-8', errors='ignore')}")

            if response.status_code in TRANSIENT_STATUS_CODES:
                err_body = await response.aread()
                retry_after = parse_retry_after(response.headers)
                await stream_ctx.__aexit__(None, None, None)
                await client.aclose()
                if attempt < max_retries:
                    delay = calculate_backoff_delay(attempt, base_delay, retry_after)
                    logger.warning(
                        "Anthropic streaming endpoint returned HTTP %d. Retrying (%d/%d) in %.2fs...",
                        response.status_code, attempt + 1, max_retries, delay
                    )
                    await asyncio.sleep(delay)
                    continue
                raise RuntimeError(f"Anthropic API error ({response.status_code}): {err_body.decode('utf-8', errors='ignore')}")

            if response.status_code != 200:
                err_body = await response.aread()
                await stream_ctx.__aexit__(None, None, None)
                await client.aclose()
                raise RuntimeError(f"Anthropic API error ({response.status_code}): {err_body.decode('utf-8', errors='ignore')}")

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
                        "Anthropic streaming read error from %s (%s). Retrying (%d/%d) in %.2fs...",
                        url, exc, attempt + 1, max_retries, delay
                    )
                    await asyncio.sleep(delay)
                    continue
                raise RuntimeError(f"Streaming request failed to {url}: {exc}") from exc
            finally:
                await stream_ctx.__aexit__(None, None, None)
                await client.aclose()

    async def list_models(self) -> List[str]:
        """Returns known Anthropic Claude models."""
        return [
            "claude-opus-4-20250514",
            "claude-sonnet-4-20250514",
            "claude-haiku-4-20250414",
            "claude-3-7-sonnet-20250224",
            "claude-3-5-sonnet-20241022",
            "claude-3-5-haiku-20241022",
        ]

    async def test_connection(self, model: Optional[str] = None) -> TestResult:
        """Tests Anthropic API credentials with a minimal prompt."""
        t0 = time.time()
        try:
            resp = await self.chat(
                messages=[ChatMessage(role="user", content="Hi")],
                model=model or self.default_model,
                max_tokens=10,
            )
            latency = (time.time() - t0) * 1000
            return TestResult(
                success=True,
                message=f"Anthropic connection successful. Response: {resp.content[:30]}...",
                latency_ms=latency,
                models=await self.list_models(),
            )
        except Exception as exc:
            latency = (time.time() - t0) * 1000
            return TestResult(
                success=False,
                message=f"Anthropic connection failed: {sanitize_error_detail(exc)}",
                latency_ms=latency,
            )
