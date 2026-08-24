"""
Adapters package for galgame2voice.
Provides unified LLM and STT adapter interfaces, provider specializations, and factory registry.
"""

from galgame2voice.adapters.base import (
    ChatMessage,
    LLMResponse,
    TestResult,
    ProviderTestResult,
    BaseLLMAdapter,
    BaseSTTAdapter,
)
from galgame2voice.adapters.registry import (
    PROVIDER_PRESETS,
    list_provider_presets,
    get_provider_preset,
    get_llm_adapter,
    get_stt_adapter,
)

__all__ = [
    "ChatMessage",
    "LLMResponse",
    "TestResult",
    "ProviderTestResult",
    "BaseLLMAdapter",
    "BaseSTTAdapter",
    "PROVIDER_PRESETS",
    "list_provider_presets",
    "get_provider_preset",
    "get_llm_adapter",
    "get_stt_adapter",
]
