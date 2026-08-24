"""
LLM Adapters package for galgame2voice.
Exports base OpenAI-compatible adapter and provider-specific specializations.
"""

from galgame2voice.adapters.llm.openai_adapter import OpenAICompatibleLLMAdapter
from galgame2voice.adapters.llm.deepseek_adapter import DeepSeekLLMAdapter
from galgame2voice.adapters.llm.qwen_adapter import QwenLLMAdapter
from galgame2voice.adapters.llm.glm_adapter import GLMLLMAdapter
from galgame2voice.adapters.llm.moonshot_adapter import MoonshotLLMAdapter
from galgame2voice.adapters.llm.siliconflow_adapter import SiliconFlowLLMAdapter
from galgame2voice.adapters.llm.groq_adapter import GroqLLMAdapter
from galgame2voice.adapters.llm.xai_adapter import XAILLMAdapter
from galgame2voice.adapters.llm.gemini_adapter import GeminiLLMAdapter
from galgame2voice.adapters.llm.anthropic_adapter import AnthropicAdapter
from galgame2voice.adapters.llm.custom_adapter import CustomLLMAdapter

__all__ = [
    "OpenAICompatibleLLMAdapter",
    "DeepSeekLLMAdapter",
    "QwenLLMAdapter",
    "GLMLLMAdapter",
    "MoonshotLLMAdapter",
    "SiliconFlowLLMAdapter",
    "GroqLLMAdapter",
    "XAILLMAdapter",
    "GeminiLLMAdapter",
    "AnthropicAdapter",
    "CustomLLMAdapter",
]
