"""
Unified LLM Provider Protocol and data models for galgame2voice.
Abstracts all LLM service integrations with typed protocols and error normalization.
"""

from typing import (
    Any,
    AsyncIterator,
    Protocol,
    runtime_checkable,
)
from pydantic import BaseModel, Field

from galgame2voice.adapters.base import (
    ChatMessage,
    LLMResponse,
    TestResult,
    BaseLLMAdapter,
    TRANSIENT_STATUS_CODES,
    TRANSIENT_NETWORK_EXCEPTIONS,
)


class ProviderError(BaseModel):
    """Normalized error details produced by LLM provider error normalization."""
    code: str = Field(default="UNKNOWN_ERROR", description="Standardized error code")
    message: str = Field(default="", description="Original provider error message")
    status_code: int | None = Field(default=None, description="HTTP status code if applicable")
    diagnostic: str | None = Field(default=None, description="User-friendly diagnosis or troubleshooting steps")
    retryable: bool = Field(default=False, description="Whether request can be retried safely")


@runtime_checkable
class LLMProvider(Protocol):
    """
    Protocol defining the standardized LLM Provider interface.
    Every provider must support chat completion, streaming, connectivity testing,
    model discovery, and error normalization.
    """

    async def chat(
        self,
        messages: list[ChatMessage],
        model: str,
        temperature: float = 1.0,
        **kwargs: Any,
    ) -> LLMResponse:
        """Executes non-streaming completion for given message history."""
        ...

    def stream_chat(
        self,
        messages: list[ChatMessage],
        model: str,
        temperature: float = 1.0,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        """Asynchronously streams incremental text delta tokens."""
        ...

    async def test_connection(self, model: str | None = None) -> TestResult:
        """Verifies API credentials and measures endpoint latency."""
        ...

    async def list_models(self) -> list[str]:
        """Discovers supported or available model IDs from provider endpoint."""
        ...

    def normalize_error(self, error: Exception) -> ProviderError:
        """Normalizes an exception into a structured ProviderError."""
        ...


class BaseLLMProvider(BaseLLMAdapter):
    """
    Base implementation providing default error normalization and stream alias
    for all provider subclasses.
    """

    def stream(
        self,
        messages: list[ChatMessage],
        model: str,
        temperature: float = 1.0,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        """Convenience alias for stream_chat."""
        return self.stream_chat(messages, model, temperature=temperature, **kwargs)

    def normalize_error(self, error: Exception) -> ProviderError:
        """Default error normalization inspecting status codes and error messages."""
        msg = str(error)
        status_code = getattr(error, "status_code", None)
        if hasattr(error, "response") and hasattr(error.response, "status_code"):
            status_code = error.response.status_code

        code = "PROVIDER_ERROR"
        retryable = False
        diagnostic = None

        if status_code == 401:
            code = "AUTHENTICATION_FAILED"
            diagnostic = "API Key 无效或已过期，请核对凭证。"
        elif status_code == 429:
            code = "RATE_LIMIT_EXCEEDED"
            retryable = True
            diagnostic = "触发服务商频率限制或额度不足，系统将自动重试。"
        elif status_code and status_code in TRANSIENT_STATUS_CODES:
            code = "SERVER_ERROR"
            retryable = True
            diagnostic = "服务商服务器暂时故障，系统将自动重试。"
        elif isinstance(error, TRANSIENT_NETWORK_EXCEPTIONS) or "timeout" in msg.lower():
            code = "TIMEOUT"
            retryable = True
            diagnostic = "请求服务商超时或网络异常，请检查网络或代理连接。"

        return ProviderError(
            code=code,
            message=msg,
            status_code=status_code,
            diagnostic=diagnostic,
            retryable=retryable,
        )


__all__ = [
    "LLMProvider",
    "BaseLLMProvider",
    "ChatMessage",
    "LLMResponse",
    "TestResult",
    "ProviderError",
]
