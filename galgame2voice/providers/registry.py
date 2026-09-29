"""
Provider Registry and Factory for galgame2voice.
Decoupled provider resolution layer mapping provider IDs to LLMProvider implementations.
"""

from typing import Any, Dict, List, Optional, Type

from galgame2voice.providers.base import LLMProvider, BaseLLMProvider
from galgame2voice.providers.openai import OpenAIProvider
from galgame2voice.providers.gemini import GeminiProvider
from galgame2voice.providers.anthropic import AnthropicProvider
from galgame2voice.providers.deepseek import DeepSeekProvider
from galgame2voice.providers.xai import XAIProvider


PROVIDER_REGISTRY: Dict[str, Type[BaseLLMProvider]] = {
    "openai": OpenAIProvider,
    "gemini": GeminiProvider,
    "anthropic": AnthropicProvider,
    "deepseek": DeepSeekProvider,
    "xai": XAIProvider,
}


def register_provider(provider_id: str, provider_class: Type[BaseLLMProvider]) -> None:
    """Registers a new LLM provider implementation."""
    PROVIDER_REGISTRY[provider_id.lower().strip()] = provider_class


def get_provider_class(provider_id: str) -> Optional[Type[BaseLLMProvider]]:
    """Retrieves the provider implementation class by ID."""
    return PROVIDER_REGISTRY.get(provider_id.lower().strip())


def create_provider(
    provider_id: str,
    api_key: str,
    base_url: Optional[str] = None,
    **kwargs: Any,
) -> LLMProvider:
    """
    Factory creating a concrete LLMProvider instance.
    Falls back to OpenAIProvider for generic OpenAI-compatible gateways.
    """
    cls = get_provider_class(provider_id) or OpenAIProvider
    if base_url:
        return cls(api_key=api_key, base_url=base_url, **kwargs)
    return cls(api_key=api_key, **kwargs)


def list_registered_providers() -> List[str]:
    """Lists registered provider identifiers."""
    return sorted(list(PROVIDER_REGISTRY.keys()))


__all__ = [
    "PROVIDER_REGISTRY",
    "register_provider",
    "get_provider_class",
    "create_provider",
    "list_registered_providers",
]
