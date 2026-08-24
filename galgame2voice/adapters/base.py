"""
Base abstract interfaces and common data models for LLM and STT adapters in galgame2voice.
Adheres to PROJECT.md §138-150 interface specifications.
"""

from abc import ABC, abstractmethod
from typing import AsyncIterator, Dict, Any, List, Optional
from pydantic import BaseModel, Field


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
