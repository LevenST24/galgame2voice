"""
Adapters package for galgame2voice.
Provides unified LLM adapter interfaces, provider specializations, and factory registry.
"""

from galgame2voice.adapters.base import (
    ChatMessage,
    LLMResponse,
    TestResult,
    BaseLLMAdapter,
)
from galgame2voice.adapters.registry import (
    PROVIDER_PRESETS,
    list_provider_presets,
    get_provider_preset,
    get_llm_adapter,
)

__all__ = [
    "ChatMessage",
    "LLMResponse",
    "TestResult",
    "BaseLLMAdapter",
    "PROVIDER_PRESETS",
    "list_provider_presets",
    "get_provider_preset",
    "get_llm_adapter",
]
