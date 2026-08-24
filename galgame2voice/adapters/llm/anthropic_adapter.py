"""
Anthropic Claude Native LLM Adapter for galgame2voice.
Implements native Anthropic /v1/messages API with x-api-key headers,
system parameter separation, and content_block_delta streaming.
"""

import json
import logging
import time
from typing import Any, AsyncGenerator, Dict, List, Optional

import httpx

from galgame2voice.adapters.base import (
    BaseLLMAdapter,
    ChatMessage,
    ChatResponse,
    TestResult,
)

logger = logging.getLogger("galgame2voice.adapters.llm.anthropic_adapter")


class AnthropicAdapter(BaseLLMAdapter):
    """
    Adapter for Anthropic Claude native /v1/messages API.
    """

    DEFAULT_BASE_URL = "https://api.anthropic.com/v1"
    DEFAULT_MODEL = "claude-3-5-sonnet-latest"
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
            if m.role == "system":
                system_content = (system_content + "\n" + m.content).strip() if system_content else m.content
            else:
                user_assistant_msgs.append({
                    "role": "user" if m.role == "user" else "assistant",
                    "content": m.content,
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
            if k not in ("client_override", "custom_headers", "timeout_s"):
                payload[k] = v

        return payload

    async def chat(
        self,
        messages: List[ChatMessage],
        model: Optional[str] = None,
        temperature: float = 0.7,
        **kwargs: Any,
    ) -> ChatResponse:
        """Executes non-streaming chat completion via Anthropic /messages endpoint."""
        url = f"{self.base_url}/messages" if not self.base_url.endswith("/messages") else self.base_url
        payload = self._prepare_anthropic_payload(
            messages, model=model, temperature=temperature, stream=False, **kwargs
        )
        headers = self._get_headers()
        timeout_s = float(self.extra_config.get("timeout_s", 60.0))

        client_override = kwargs.get("client_override")
        if client_override:
            resp = await client_override.post(url, json=payload, headers=headers)
            if resp.status_code != 200:
                raise RuntimeError(f"Anthropic API returned {resp.status_code}: {resp.text}")
            data = resp.json()
            content = ""
            for block in data.get("content", []):
                if block.get("type") == "text":
                    content += block.get("text", "")
            return ChatResponse(content=content, raw=data)

        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code in (401, 403):
                raise ValueError(f"Anthropic authentication failed ({resp.status_code}): {resp.text}")
            if resp.status_code != 200:
                raise RuntimeError(f"Anthropic API error ({resp.status_code}): {resp.text}")
            data = resp.json()
            content = ""
            for block in data.get("content", []):
                if block.get("type") == "text":
                    content += block.get("text", "")
            return ChatResponse(content=content, raw=data)

    async def stream_chat(
        self,
        messages: List[ChatMessage],
        model: Optional[str] = None,
        temperature: float = 0.7,
        **kwargs: Any,
    ) -> AsyncGenerator[str, None]:
        """Streams token chunks from Anthropic SSE /messages stream."""
        url = f"{self.base_url}/messages" if not self.base_url.endswith("/messages") else self.base_url
        payload = self._prepare_anthropic_payload(
            messages, model=model, temperature=temperature, stream=True, **kwargs
        )
        headers = self._get_headers()
        headers["Accept"] = "text/event-stream"
        timeout_s = float(self.extra_config.get("timeout_s", 60.0))

        client_override = kwargs.get("client_override")
        if client_override:
            resp = await client_override.post(url, json=payload, headers=headers)
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

        async with httpx.AsyncClient(timeout=timeout_s) as client:
            async with client.stream("POST", url, json=payload, headers=headers) as response:
                if response.status_code in (401, 403):
                    err_body = await response.aread()
                    raise ValueError(f"Anthropic auth failed ({response.status_code}): {err_body.decode('utf-8', errors='ignore')}")
                if response.status_code != 200:
                    err_body = await response.aread()
                    raise RuntimeError(f"Anthropic API error ({response.status_code}): {err_body.decode('utf-8', errors='ignore')}")

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

    async def list_models(self) -> List[str]:
        """Returns known Anthropic Claude models."""
        return [
            "claude-3-5-sonnet-latest",
            "claude-3-5-haiku-latest",
            "claude-3-opus-latest",
            "claude-3-sonnet-20240229",
            "claude-3-haiku-20240307",
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
