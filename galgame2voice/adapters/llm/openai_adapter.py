"""
OpenAI-compatible LLM Adapter implementation for galgame2voice.
Supports standard OpenAI REST endpoints, streaming SSE parsing, connection testing, and model discovery.
"""

import asyncio
import json
import time
from typing import AsyncIterator, Dict, Any, List, Optional
import httpx

from galgame2voice.adapters.base import BaseLLMAdapter, ChatMessage, LLMResponse, TestResult


class OpenAICompatibleLLMAdapter(BaseLLMAdapter):
    """
    Adapter for OpenAI and OpenAI-compatible API providers (DeepSeek, Groq, Qwen, GLM, etc.).
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        client_override: Optional[Any] = None,
        **kwargs: Any,
    ):
        super().__init__(api_key=api_key, base_url=base_url, **kwargs)
        self.mock_server = client_override

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

    async def chat(
        self,
        messages: List[ChatMessage],
        model: str,
        temperature: float = 1.0,
        **kwargs: Any,
    ) -> LLMResponse:
        """
        Performs a non-streaming chat completion request.
        """
        self._validate_credentials()

        # Handle mock server for test environments
        if self.mock_server:
            resp = await self.mock_server.handle_chat_completion(
                {
                    "messages": [m.model_dump() for m in messages],
                    "model": model,
                    "temperature": temperature,
                    **kwargs,
                },
                headers={"authorization": f"Bearer {self.api_key}"},
            )
            if resp.status_code == 401 or resp.status_code == 403:
                raise ValueError(f"Authentication error: {resp.json() if hasattr(resp, 'json') else resp.text}")
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
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": temperature,
        }
        for k, v in kwargs.items():
            if k not in ("client_override", "custom_headers", "timeout_s"):
                payload[k] = v

        timeout_s = float(self.extra_config.get("timeout_s", 60.0))

        async with httpx.AsyncClient(timeout=timeout_s) as client:
            try:
                resp = await client.post(url, json=payload, headers=self._get_headers())
            except httpx.RequestError as exc:
                raise RuntimeError(f"Network error connecting to {url}: {exc}") from exc

            if resp.status_code in (401, 403):
                raise ValueError(f"Authentication error ({resp.status_code}): {resp.text}")
            if resp.status_code == 429:
                raise RuntimeError(f"Rate limit exceeded (429): {resp.text}")
            if resp.status_code != 200:
                raise RuntimeError(f"API returned status {resp.status_code}: {resp.text}")

            try:
                data = resp.json()
                content = data["choices"][0]["message"]["content"]
                usage = data.get("usage")
                return LLMResponse(content=content, usage=usage)
            except Exception as exc:
                raise RuntimeError(f"Failed to parse LLM response JSON: {exc} | Body: {resp.text[:200]}") from exc

    async def stream_chat(
        self,
        messages: List[ChatMessage],
        model: str,
        temperature: float = 1.0,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        """
        Streams Server-Sent Event (SSE) tokens from the provider.
        """
        self._validate_credentials()

        # Handle mock server for test environments
        if self.mock_server:
            resp = await self.mock_server.handle_chat_completion(
                {
                    "messages": [m.model_dump() for m in messages],
                    "model": model,
                    "temperature": temperature,
                    "stream": True,
                    **kwargs,
                },
                headers={"authorization": f"Bearer {self.api_key}"},
            )
            if resp.status_code == 401 or resp.status_code == 403:
                raise ValueError(f"Authentication error: {resp.text}")
            if resp.status_code != 200:
                raise RuntimeError(f"API returned status {resp.status_code}: {resp.text}")

            lines = resp.text.split("\n")
            for line in lines:
                line = line.strip()
                if not line or not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if data_str == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                    choices = chunk.get("choices", [])
                    if choices:
                        delta = choices[0].get("delta", {})
                        token = delta.get("content")
                        if token:
                            yield token
                except json.JSONDecodeError:
                    continue
            return

        url = f"{self.base_url}/chat/completions"
        payload: Dict[str, Any] = {
            "model": model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": temperature,
            "stream": True,
        }
        for k, v in kwargs.items():
            if k not in ("client_override", "custom_headers", "timeout_s"):
                payload[k] = v

        timeout_s = float(self.extra_config.get("timeout_s", 60.0))
        headers = self._get_headers()
        headers["Accept"] = "text/event-stream"

        async with httpx.AsyncClient(timeout=timeout_s) as client:
            try:
                async with client.stream("POST", url, json=payload, headers=headers) as response:
                    if response.status_code in (401, 403):
                        error_body = await response.aread()
                        raise ValueError(f"Authentication error ({response.status_code}): {error_body.decode('utf-8', errors='ignore')}")
                    if response.status_code == 429:
                        error_body = await response.aread()
                        raise RuntimeError(f"Rate limit exceeded (429): {error_body.decode('utf-8', errors='ignore')}")
                    if response.status_code != 200:
                        error_body = await response.aread()
                        raise RuntimeError(f"API returned status {response.status_code}: {error_body.decode('utf-8', errors='ignore')}")

                    async for line in response.aiter_lines():
                        line = line.strip()
                        if not line or not line.startswith("data:"):
                            continue
                        data_str = line[5:].strip()
                        if data_str == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data_str)
                            choices = chunk.get("choices", [])
                            if choices:
                                delta = choices[0].get("delta", {})
                                token = delta.get("content")
                                if token:
                                    yield token
                        except json.JSONDecodeError:
                            continue
            except httpx.RequestError as exc:
                raise RuntimeError(f"Streaming request failed to {url}: {exc}") from exc

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
        # Probe using models list endpoint or minimal completion
        test_model = model or "gpt-4o-mini"
        url = f"{self.base_url}/models"
        headers = self._get_headers()

        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                resp = await client.get(url, headers=headers)
                latency_ms = round((time.perf_counter() - t0) * 1000, 2)
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
                elif resp.status_code in (401, 403):
                    return TestResult(
                        success=False,
                        message=f"Authentication failed ({resp.status_code}): Invalid credentials",
                        latency_ms=latency_ms,
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
                    if chat_resp.status_code == 200:
                        return TestResult(
                            success=True,
                            message=f"Connected successfully to {self.base_url}",
                            latency_ms=latency_ms,
                        )
                    return TestResult(
                        success=False,
                        message=f"Provider test returned HTTP {chat_resp.status_code}: {chat_resp.text[:200]}",
                        latency_ms=latency_ms,
                    )
            except Exception as exc:
                latency_ms = round((time.perf_counter() - t0) * 1000, 2)
                return TestResult(
                    success=False,
                    message=f"Connection error: {type(exc).__name__} - {exc}",
                    latency_ms=latency_ms,
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
