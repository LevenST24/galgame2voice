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
)


class ProviderError(BaseModel):
    """Normalized error details across LLM and STT providers."""
    code: str = Field(default="UNKNOWN_ERROR", description="Standardized error code")
    message: str = Field(default="", description="Original provider error message")
    status_code: int | None = Field(default=None, description="HTTP status code if applicable")
    diagnostic: str | None = Field(default=None, description="User-friendly diagnosis or troubleshooting steps")
    retryable: bool = Field(default=False, description="Whether request can be retried safely")


@runtime_checkable
class LLMProvider(Protocol):
    """
    Protocol defining the standardized LLM Provider interface.
    Every provider must support streaming chat, connectivity testing,
    model discovery, and error normalization.
    """

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


__all__ = [
    "LLMProvider",
    "BaseLLMProvider",
    "ChatMessage",
    "LLMResponse",
    "TestResult",
    "ProviderError",
]
