"""
LLM Provider abstractions and implementations for galgame2voice.
Decouples LLM service gateways with standard LLMProvider Protocol and error normalization.
"""

from galgame2voice.providers.base import (
    LLMProvider,
    BaseLLMProvider,
    ChatMessage,
    LLMResponse,
    TestResult,
    ProviderError,
)
from galgame2voice.providers.openai import OpenAIProvider
from galgame2voice.providers.gemini import GeminiProvider
from galgame2voice.providers.anthropic import AnthropicProvider
from galgame2voice.providers.deepseek import DeepSeekProvider
from galgame2voice.providers.xai import XAIProvider
from galgame2voice.providers.registry import (
    PROVIDER_REGISTRY,
    get_provider_class,
    create_provider,
)

__all__ = [
    "LLMProvider",
    "BaseLLMProvider",
    "ChatMessage",
    "LLMResponse",
    "TestResult",
    "ProviderError",
    "OpenAIProvider",
    "GeminiProvider",
    "AnthropicProvider",
    "DeepSeekProvider",
    "XAIProvider",
    "PROVIDER_REGISTRY",
    "get_provider_class",
    "create_provider",
]
