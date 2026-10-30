"""
Base abstract interfaces and common data models for LLM and STT adapters in galgame2voice.
"""

from abc import ABC, abstractmethod
import asyncio
from contextvars import ContextVar, Token
import datetime
import email.utils
import json
import logging
import random
from typing import AsyncIterator, Any, Callable
import httpx
from pydantic import BaseModel, Field

logger = logging.getLogger("galgame2voice.adapters.base")

# The chat producer installs this for one reply. Reasoning text never leaves the parser.
_stream_progress_hook: ContextVar[Callable[[str], None] | None] = ContextVar(
    "g2v_stream_progress_hook",
    default=None,
)

# HTTP status codes that indicate transient failure and can be retried with exponential backoff
TRANSIENT_STATUS_CODES: set[int] = {408, 429, 500, 502, 503, 504}

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


def parse_retry_after(headers: Any | None) -> float | None:
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
    retry_after: float | None = None,
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


def extract_stream_token(chunk: dict[str, Any]) -> str | None:
    """
    Extracts delta text token from raw OpenAI or Anthropic streaming SSE JSON chunks.
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


def bind_stream_progress_hook(hook: Callable[[str], None] | None) -> Token[Callable[[str], None] | None]:
    """Installs the per-reply progress hook seen by parse_sse_lines on this task."""
    return _stream_progress_hook.set(hook)


def reset_stream_progress_hook(token: Token[Callable[[str], None] | None]) -> None:
    """Restores the progress hook that was current before bind_stream_progress_hook."""
    _stream_progress_hook.reset(token)


def _has_reasoning_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _progress_stage(chunk: dict[str, Any]) -> str | None:
    """Returns a progress stage for model thinking, without the thinking text."""
    choices = chunk.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        delta = choices[0].get("delta")
        if isinstance(delta, dict) and (
            _has_reasoning_text(delta.get("reasoning_content")) or _has_reasoning_text(delta.get("reasoning"))
        ):
            return "thinking"
    if chunk.get("type") == "content_block_delta":
        delta = chunk.get("delta")
        if (
            isinstance(delta, dict)
            and delta.get("type") == "thinking_delta"
            and _has_reasoning_text(delta.get("thinking"))
        ):
            return "thinking"
    return None


def _notify_stream_progress(chunk: dict[str, Any]) -> None:
    stage = _progress_stage(chunk)
    if stage is None:
        return
    hook = _stream_progress_hook.get()
    if hook is not None:
        hook(stage)


def _parse_and_extract_token(text: str, on_chunk: Callable[[dict[str, Any]], None] | None = None) -> tuple[bool, str | None]:
    """Attempts to decode JSON and extract token. Returns (is_valid_json, token)."""
    try:
        chunk = json.loads(text)
        if on_chunk and isinstance(chunk, dict):
            on_chunk(chunk)
        token = extract_stream_token(chunk)
        if isinstance(chunk, dict):
            _notify_stream_progress(chunk)
        return True, token
    except (json.JSONDecodeError, TypeError):
        return False, None


def _extract_sse_data(line: str, has_buffer: bool) -> str | None:
    """Extracts data content from an SSE line, accounting for data: prefixes and continuation lines."""
    if line.startswith("data: "):
        return line[6:]
    if line.startswith("data:"):
        return line[5:].lstrip()
    if has_buffer and not any(line.startswith(prefix) for prefix in ("event:", "id:", "retry:")):
        return line
    return None


def _flush_sse_data_buffer(data_buffer: list[str], on_chunk: Callable[[dict[str, Any]], None] | None = None) -> tuple[str | None, bool]:
    """Flushes data buffer into a combined payload. Returns (token, is_done)."""
    if not data_buffer:
        return None, False
    combined_data = "\n".join(data_buffer).strip()
    data_buffer.clear()
    if combined_data == "[DONE]":
        return None, True
    if not combined_data:
        return None, False
    is_json, token = _parse_and_extract_token(combined_data, on_chunk)
    if is_json and token:
        return token, False
    return None, False


async def parse_sse_lines(lines_iter: AsyncIterator[str], on_chunk: Callable[[dict[str, Any]], None] | None = None) -> AsyncIterator[str]:
    """
    Asynchronously parses Server-Sent Events (SSE) lines into text tokens.
    Guarantees resilience against:
    - Fragmented lines / multi-line data blocks
    - Malformed or partial JSON chunks (retried across lines, then skipped without logging)
    - SSE comments (: keepalive)
    - Whitespace variations in 'data:' prefix
    - Stream termination tokens ([DONE])
    """
    data_buffer: list[str] = []

    async for raw_line in lines_iter:
        line = raw_line.rstrip("\r\n")
        stripped = line.strip()

        if not stripped:
            # Blank line: event boundary per SSE standard
            token, is_done = _flush_sse_data_buffer(data_buffer, on_chunk)
            if is_done:
                break
            if token:
                yield token
            continue

        if stripped.startswith(":"):
            # Comment or keepalive
            continue

        data_str = _extract_sse_data(line, bool(data_buffer))

        if data_str is not None:
            if data_str.strip() == "[DONE]":
                break

            if data_buffer:
                # 1. Try joining with accumulated buffer
                joined = "\n".join(data_buffer + [data_str])
                is_json, token = _parse_and_extract_token(joined, on_chunk)
                if is_json:
                    if token:
                        yield token
                    data_buffer.clear()
                    continue

                # 2. Joined parse failed: check if data_str alone is a valid standalone chunk.
                # If so, the prior buffer was corrupted/unfinishable: discard it and process data_str.
                is_json, token = _parse_and_extract_token(data_str, on_chunk)
                if is_json:
                    data_buffer.clear()
                    if token:
                        yield token
                    continue

                # Both joined and standalone failed: keep accumulating
                data_buffer.append(data_str)
            else:
                # Buffer is empty: try eager single-line parse (supports streams without blank delimiters)
                is_json, token = _parse_and_extract_token(data_str, on_chunk)
                if is_json:
                    if token:
                        yield token
                else:
                    data_buffer.append(data_str)

    # Flush any trailing buffer
    token, _ = _flush_sse_data_buffer(data_buffer, on_chunk)
    if token:
        yield token


async def aclose_stream_context(stream_ctx: Any = None, client: Any = None) -> None:
    """Defensively closes an httpx stream context and client, absorbing any cleanup exceptions."""
    if stream_ctx is not None:
        try:
            await stream_ctx.__aexit__(None, None, None)
        except Exception as exc:
            logger.debug("Failed closing stream context: %s", exc)
    if client is not None:
        try:
            await client.aclose()
        except Exception as exc:
            logger.debug("Failed closing client: %s", exc)



class ChatMessage(BaseModel):
    """Normalized chat message structure."""
    role: str = Field(..., description="Message role: 'system', 'user', or 'assistant'")
    content: str = Field(..., description="Message text content")


class LLMResponse(BaseModel):
    """Normalized LLM completion response."""
    content: str = Field(..., description="Generated text content")
    usage: dict[str, Any] | None = Field(default=None, description="Token usage statistics")


# Alias for backward compatibility
ChatResponse = LLMResponse


class TestResult(BaseModel):
    """Result of provider connectivity, authentication, and latency testing."""
    __test__ = False  # Avoid pytest test collector discovery
    success: bool = Field(..., description="Whether connection and auth succeeded")
    message: str = Field(..., description="Informative status message or error details")
    latency_ms: float | None = Field(default=None, description="Round-trip latency in milliseconds")
    models: list[str] | None = Field(default=None, description="Discovered available models")
    error: str | None = Field(default=None, description="Error classification")
    diagnostic: str | None = Field(default=None, description="User-friendly Chinese troubleshooting guidance")
    status_code: int | None = Field(default=None, description="HTTP status code from probe")


# Alias for backward compatibility with tests
ProviderTestResult = TestResult


class MissingApiKeyError(ValueError):
    """Raised before any request when a provider that requires an API key has none configured."""

    def __init__(self) -> None:
        super().__init__("Authentication error: API key is not configured")


REASONING_EFFORT_LEVELS = frozenset({"none", "low", "medium", "high"})


def configured_reasoning_effort(adapter: object) -> str | None:
    """Returns the adapter's reasoning effort when it is a known level, else None."""
    value = getattr(adapter, "reasoning_effort", None)
    return value if isinstance(value, str) and value in REASONING_EFFORT_LEVELS else None


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
        self.extra_config: dict[str, Any] = kwargs
        self.reasoning_effort: str | None = kwargs.get("reasoning_effort")

    @abstractmethod
    async def chat(
        self,
        messages: list[ChatMessage],
        model: str,
        temperature: float = 1.0,
        **kwargs: Any,
    ) -> LLMResponse:
        """
        Executes non-streaming completion for given message history.
        """

    @abstractmethod
    def stream_chat(
        self,
        messages: list[ChatMessage],
        model: str,
        temperature: float = 1.0,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        """
        Asynchronously streams incremental text delta tokens.
        """

    @abstractmethod
    async def test_connection(self, model: str | None = None) -> TestResult:
        """
        Verifies API credentials and measures endpoint latency.
        """

    @abstractmethod
    async def list_models(self) -> list[str]:
        """
        Discovers supported or available model IDs from provider endpoint.
        """


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
        self.extra_config: dict[str, Any] = kwargs

    @abstractmethod
    async def transcribe(
        self,
        audio_bytes: bytes,
        filename: str = "audio.wav",
        language: str | None = None,
        **kwargs: Any,
    ) -> str:
        """
        Transcribes binary audio payload into plain text.
        """

    @abstractmethod
    async def test_connection(self) -> TestResult:
        """
        Verifies STT credentials and measures endpoint latency.
        """
