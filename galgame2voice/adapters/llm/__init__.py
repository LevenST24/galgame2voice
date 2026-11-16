"""
LLM Adapters package for galgame2voice.
Exports base OpenAI-compatible adapter and provider-specific specializations.
"""

import importlib
from typing import Any

from galgame2voice.adapters.llm.openai_adapter import OpenAICompatibleLLMAdapter, OpenAILLMAdapter
from galgame2voice.adapters.llm.deepseek_adapter import DeepSeekLLMAdapter
from galgame2voice.adapters.llm.qwen_adapter import QwenLLMAdapter
from galgame2voice.adapters.llm.glm_adapter import GLMLLMAdapter
from galgame2voice.adapters.llm.moonshot_adapter import MoonshotLLMAdapter
from galgame2voice.adapters.llm.siliconflow_adapter import SiliconFlowLLMAdapter
from galgame2voice.adapters.llm.xai_adapter import XAILLMAdapter
from galgame2voice.adapters.llm.gemini_adapter import GeminiLLMAdapter
from galgame2voice.adapters.llm.anthropic_adapter import AnthropicAdapter
from galgame2voice.adapters.llm.custom_adapter import CustomLLMAdapter

# Lazy re-exports from providers for backward compatibility without creating an
# adapters <-> providers import cycle when providers is imported first.
_PROVIDER_REEXPORTS: dict[str, str] = {
    "LLMProvider": "galgame2voice.providers.base",
    "BaseLLMProvider": "galgame2voice.providers.base",
    "ProviderError": "galgame2voice.providers.base",
    "OpenAIProvider": "galgame2voice.providers.openai",
    "GeminiProvider": "galgame2voice.providers.gemini",
    "AnthropicProvider": "galgame2voice.providers.anthropic",
    "DeepSeekProvider": "galgame2voice.providers.deepseek",
    "XAIProvider": "galgame2voice.providers.xai",
}


def __getattr__(name: str) -> Any:
    module_path = _PROVIDER_REEXPORTS.get(name)
    if module_path is not None:
        module = importlib.import_module(module_path)
        value = getattr(module, name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = [
    "OpenAICompatibleLLMAdapter",
    "OpenAILLMAdapter",
    "DeepSeekLLMAdapter",
    "QwenLLMAdapter",
    "GLMLLMAdapter",
    "MoonshotLLMAdapter",
    "SiliconFlowLLMAdapter",
    "XAILLMAdapter",
    "GeminiLLMAdapter",
    "AnthropicAdapter",
    "CustomLLMAdapter",
    "LLMProvider",
    "BaseLLMProvider",
    "ProviderError",
    "OpenAIProvider",
    "GeminiProvider",
    "AnthropicProvider",
    "DeepSeekProvider",
    "XAIProvider",
]
