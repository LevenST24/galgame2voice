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
)

logger = logging.getLogger("galgame2voice.adapters.llm.anthropic")


def _parse_retry_after(headers: Optional[Any]) -> Optional[float]:
    """Extracts and parses Retry-After header (seconds or HTTP date)."""
    if not headers:
        return None
    val = None
    if hasattr(headers, "get"):
        val = headers.get("retry-after") or headers.get("Retry-After")
    elif isinstance(headers, dict):
        val = headers.get("retry-after") or headers.get("Retry-After")
    if not val:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        try:
            import datetime
            retry_date = email.utils.parsedate_to_datetime(str(val))
            now = datetime.datetime.now(datetime.timezone.utc)
            delta = (retry_date - now).total_seconds()
            return max(0.0, delta)
        except Exception:
            return None


def _calculate_backoff_delay(
    attempt: int,
    base_delay: float = 1.0,
    retry_after: Optional[float] = None,
) -> float:
    """Calculates backoff delay with exponential scaling and random jitter."""
    if retry_after is not None and retry_after > 0:
        return min(60.0, retry_after + random.uniform(0.1, 0.4))
    exp_backoff = min(10.0, base_delay * (2 ** attempt))
    jitter = random.uniform(0.1, 0.4)
    return exp_backoff + jitter


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

        # Anthropic requires at least one user message
        if not user_assistant_msgs:
            user_assistant_msgs.append({"role": "user", "content": "Hello"})

        payload: Dict[str, Any] = {
            "model": model or self.default_model,
            "messages": user_assistant_msgs,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": stream,
        }
        if system_content:
            payload["system"] = system_content

        for k, v in kwargs.items():
            if k not in ("client_override", "custom_headers", "timeout_s", "max_retries", "base_delay"):
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
                if resp.status_code in (429, 502, 503, 504):
                    if attempt < max_retries:
                        retry_after = _parse_retry_after(getattr(resp, "headers", None))
                        delay = _calculate_backoff_delay(attempt, base_delay, retry_after)
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
                except (
                    httpx.ConnectTimeout,
                    httpx.ReadTimeout,
                    httpx.WriteTimeout,
                    httpx.PoolTimeout,
                    httpx.ConnectError,
                    httpx.RemoteProtocolError,
                    httpx.NetworkError,
                    httpx.RequestError,
                ) as exc:
                    if attempt < max_retries:
                        delay = _calculate_backoff_delay(attempt, base_delay)
                        logger.warning(
                            "Transient network error connecting to %s (%s: %s). Retrying (%d/%d) in %.2fs...",
                            url, type(exc).__name__, exc, attempt + 1, max_retries, delay
                        )
                        await asyncio.sleep(delay)
                        continue
                    raise RuntimeError(f"Network error connecting to {url}: {exc}") from exc

                if resp.status_code in (401, 403):
                    raise ValueError(f"Anthropic authentication failed ({resp.status_code}): {resp.text}")

                if resp.status_code in (429, 502, 503, 504):
                    if attempt < max_retries:
                        retry_after = _parse_retry_after(resp.headers)
                        delay = _calculate_backoff_delay(attempt, base_delay, retry_after)
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
                if resp.status_code in (429, 502, 503, 504):
                    if attempt < max_retries:
                        retry_after = _parse_retry_after(getattr(resp, "headers", None))
                        delay = _calculate_backoff_delay(attempt, base_delay, retry_after)
                        await asyncio.sleep(delay)
                        continue
                    raise RuntimeError(f"Anthropic API returned status {resp.status_code}: {resp.text}")
                if resp.status_code != 200:
                    raise RuntimeError(f"Anthropic API returned status {resp.status_code}: {resp.text}")
                for line in resp.text.split("\n"):
                    line = line.strip()
                    if line.startswith("data:"):
                        data_str = line[5:].strip()
                        try:
                            chunk = json.loads(data_str)
                            if chunk.get("type") == "content_block_delta":
                                text = chunk.get("delta", {}).get("text", "")
                                if text:
                                    yield text
                        except Exception:
                            continue
                return

        for attempt in range(max_retries + 1):
            client = httpx.AsyncClient(timeout=timeout_s)
            stream_ctx = None
            try:
                stream_ctx = client.stream("POST", url, json=payload, headers=headers)
                response = await stream_ctx.__aenter__()
            except (
                httpx.ConnectTimeout,
                httpx.ReadTimeout,
                httpx.WriteTimeout,
                httpx.PoolTimeout,
                httpx.ConnectError,
                httpx.RemoteProtocolError,
                httpx.NetworkError,
                httpx.RequestError,
            ) as exc:
                if stream_ctx:
                    try:
                        await stream_ctx.__aexit__(None, None, None)
                    except Exception:
                        pass
                await client.aclose()
                if attempt < max_retries:
                    delay = _calculate_backoff_delay(attempt, base_delay)
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

            if response.status_code in (429, 502, 503, 504):
                err_body = await response.aread()
                retry_after = _parse_retry_after(response.headers)
                await stream_ctx.__aexit__(None, None, None)
                await client.aclose()
                if attempt < max_retries:
                    delay = _calculate_backoff_delay(attempt, base_delay, retry_after)
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

            try:
                async for line in response.aiter_lines():
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    data_str = line[5:].strip()
                    try:
                        chunk = json.loads(data_str)
                        if chunk.get("type") == "content_block_delta":
                            text = chunk.get("delta", {}).get("text", "")
                            if text:
                                yield text
                        elif chunk.get("type") == "error":
                            raise RuntimeError(f"Anthropic stream error: {chunk.get('error')}")
                    except json.JSONDecodeError:
                        continue
                return
            except httpx.RequestError as exc:
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
                message=f"Anthropic connection failed: {exc}",
                latency_ms=latency,
            )
