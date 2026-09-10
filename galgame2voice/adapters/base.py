"""
Base abstract interfaces and common data models for LLM and STT adapters in galgame2voice.
Adheres to PROJECT.md §138-150 interface specifications.
"""

from abc import ABC, abstractmethod
import asyncio
import datetime
import email.utils
import json
import logging
import random
from typing import AsyncIterator, Dict, Any, List, Optional, Set
import httpx
from pydantic import BaseModel, Field

logger = logging.getLogger("galgame2voice.adapters.base")

# HTTP status codes that indicate transient failure and can be retried with exponential backoff
TRANSIENT_STATUS_CODES: Set[int] = {408, 429, 500, 502, 503, 504}

# Network-level exceptions that indicate transient transport issues
TRANSIENT_NETWORK_EXCEPTIONS = (
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.ConnectError,
    httpx.RemoteProtocolError,
    httpx.NetworkError,
    httpx.RequestError,
    asyncio.TimeoutError,
    ConnectionResetError,
    ConnectionError,
)


def parse_retry_after(headers: Optional[Any]) -> Optional[float]:
    """
    Extracts and parses Retry-After header (seconds or RFC HTTP date).
    Returns non-negative delay in seconds, or None if missing or unparseable.
    """
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
        return max(0.0, float(val))
    except (ValueError, TypeError):
        try:
            retry_date = email.utils.parsedate_to_datetime(str(val))
            now = datetime.datetime.now(datetime.timezone.utc)
            delta = (retry_date - now).total_seconds()
            return max(0.0, delta)
        except Exception:
            return None


def calculate_backoff_delay(
    attempt: int,
    base_delay: float = 1.0,
    retry_after: Optional[float] = None,
    max_delay: float = 60.0,
    jitter_min: float = 0.1,
    jitter_max: float = 0.4,
) -> float:
    """
    Calculates backoff delay using exponential scaling and jitter.
    If retry_after is present and positive, respects it with added jitter up to max_delay.
    """
    jitter = random.uniform(jitter_min, jitter_max)
    if retry_after is not None and retry_after > 0:
        return min(max_delay, retry_after + jitter)
    exp_backoff = min(10.0, base_delay * (2 ** attempt))
    return min(max_delay, exp_backoff + jitter)


def extract_stream_token(chunk: Dict[str, Any]) -> Optional[str]:
    """
    Extracts delta text token from normalized OpenAI or Anthropic streaming SSE JSON chunks.
    Raises RuntimeError if provider returns an explicit stream error object.
    """
    if not isinstance(chunk, dict):
        return None

    if chunk.get("type") == "error":
        err = chunk.get("error", {})
        raise RuntimeError(f"Anthropic stream error: {err}")

    # Explicit provider error payload (must check truthiness: ignore {"error": null} or {"error": false})
    if chunk.get("error"):
        err = chunk["error"]
        msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
        raise RuntimeError(f"Streaming error from provider: {msg}")

    # OpenAI-compatible format: choices[0].delta.content
    choices = chunk.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            delta = first.get("delta")
            if isinstance(delta, dict):
                content = delta.get("content")
                if content is not None:
                    return str(content)
            if "text" in first and isinstance(first["text"], str):
                return first["text"]

    # Anthropic Claude format: type="content_block_delta", delta.text
    if chunk.get("type") == "content_block_delta":
        delta = chunk.get("delta", {})
        if isinstance(delta, dict):
            text = delta.get("text")
            if text is not None:
                return str(text)

    return None


async def parse_sse_lines(lines_iter: AsyncIterator[str]) -> AsyncIterator[str]:
    """
    Asynchronously parses Server-Sent Events (SSE) lines into text tokens.
    Guarantees resilience against:
    - Fragmented lines / multi-line data blocks
    - Malformed or partial JSON chunks (logged and skipped without crashing stream)
    - SSE comments (: keepalive)
    - Whitespace variations in 'data:' prefix
    - Stream termination tokens ([DONE])
    """
    data_buffer: List[str] = []

    async for raw_line in lines_iter:
        line = raw_line.rstrip("\r\n")
        stripped = line.strip()

        if not stripped:
            # Blank line: event boundary per SSE standard
            if data_buffer:
                combined_data = "\n".join(data_buffer).strip()
                data_buffer.clear()
                if combined_data == "[DONE]":
                    break
                try:
                    chunk = json.loads(combined_data)
                    token = extract_stream_token(chunk)
                    if token:
                        yield token
                except json.JSONDecodeError:
                    continue
            continue

        if stripped.startswith(":"):
            # Comment or keepalive
            continue

        # Extract data content
        data_str: Optional[str] = None
        if line.startswith("data: "):
            data_str = line[6:]
        elif line.startswith("data:"):
            data_str = line[5:].lstrip()
        elif data_buffer and not any(line.startswith(prefix) for prefix in ("event:", "id:", "retry:")):
            # Continuation line of a multi-line fragmented block
            data_str = line

        if data_str is not None:
            if data_str.strip() == "[DONE]":
                break

            if data_buffer:
                # 1. Try joining with accumulated buffer
                joined = "\n".join(data_buffer + [data_str])
                try:
                    chunk = json.loads(joined)
                    token = extract_stream_token(chunk)
                    if token:
                        yield token
                    data_buffer.clear()
                    continue
                except json.JSONDecodeError:
                    pass

                # 2. Joined parse failed: check if data_str alone is a valid standalone chunk.
                # If so, the prior buffer was corrupted/unfinishable: discard it and process data_str.
                try:
                    chunk = json.loads(data_str)
                    token = extract_stream_token(chunk)
                    data_buffer.clear()
                    if token:
                        yield token
                    continue
                except json.JSONDecodeError:
                    # Both joined and standalone failed: keep accumulating
                    data_buffer.append(data_str)
            else:
                # Buffer is empty: try eager single-line parse (supports streams without blank delimiters)
                try:
                    chunk = json.loads(data_str)
                    token = extract_stream_token(chunk)
                    if token:
                        yield token
                except json.JSONDecodeError:
                    data_buffer.append(data_str)

    # Flush any trailing buffer
    if data_buffer:
        combined_data = "\n".join(data_buffer).strip()
        if combined_data and combined_data != "[DONE]":
            try:
                chunk = json.loads(combined_data)
                token = extract_stream_token(chunk)
                if token:
                    yield token
            except Exception:
                pass



class ChatMessage(BaseModel):
    """Normalized chat message structure."""
    role: str = Field(..., description="Message role: 'system', 'user', or 'assistant'")
    content: str = Field(..., description="Message text content")


class LLMResponse(BaseModel):
    """Normalized LLM completion response."""
    content: str = Field(..., description="Generated text content")
    usage: Optional[Dict[str, Any]] = Field(default=None, description="Token usage statistics")


# Alias for backward compatibility
ChatResponse = LLMResponse


class TestResult(BaseModel):
    """Result of provider connectivity, authentication, and latency testing."""
    __test__ = False  # Avoid pytest test collector discovery
    success: bool = Field(..., description="Whether connection and auth succeeded")
    message: str = Field(..., description="Informative status message or error details")
    latency_ms: Optional[float] = Field(default=None, description="Round-trip latency in milliseconds")
    models: Optional[List[str]] = Field(default=None, description="Discovered available models")


# Alias for backward compatibility with tests
ProviderTestResult = TestResult


class BaseLLMAdapter(ABC):
    """
    Abstract Base Class for Large Language Model Adapters.
    Provides standard unified interface for synchronous chat, streaming chat,
    connection testing, and model discovery.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        **kwargs: Any,
    ):
        self.api_key = str(api_key).strip() if api_key else ""
        self.base_url = str(base_url).rstrip("/") if base_url else ""
        self.extra_config: Dict[str, Any] = kwargs

    @abstractmethod
    async def chat(
        self,
        messages: List[ChatMessage],
        model: str,
        temperature: float = 1.0,
        **kwargs: Any,
    ) -> LLMResponse:
        """
        Executes non-streaming completion for given message history.
        """
        pass

    @abstractmethod
    async def stream_chat(
        self,
        messages: List[ChatMessage],
        model: str,
        temperature: float = 1.0,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        """
        Asynchronously streams incremental text delta tokens.
        """
        pass

    @abstractmethod
    async def test_connection(self, model: Optional[str] = None) -> TestResult:
        """
        Verifies API credentials and measures endpoint latency.
        """
        pass

    @abstractmethod
    async def list_models(self) -> List[str]:
        """
        Discovers supported or available model IDs from provider endpoint.
        """
        pass


class BaseSTTAdapter(ABC):
    """
    Abstract Base Class for Speech-to-Text (ASR) Adapters.
    Provides standard unified interface for transcribing audio bytes
    and testing STT service connectivity.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        **kwargs: Any,
    ):
        self.api_key = str(api_key).strip() if api_key else ""
        self.base_url = str(base_url).rstrip("/") if base_url else ""
        self.extra_config: Dict[str, Any] = kwargs

    @abstractmethod
    async def transcribe(
        self,
        audio_bytes: bytes,
        filename: str = "audio.wav",
        language: Optional[str] = None,
        **kwargs: Any,
    ) -> str:
        """
        Transcribes binary audio payload into plain text.
        """
        pass

    @abstractmethod
    async def test_connection(self) -> TestResult:
        """
        Verifies STT credentials and measures endpoint latency.
        """
        pass
