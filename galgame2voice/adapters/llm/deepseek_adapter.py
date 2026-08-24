"""
DeepSeek LLM Adapter for galgame2voice.
Connects to DeepSeek API with optimal presets.
"""

from typing import Any, Optional
from galgame2voice.adapters.llm.openai_adapter import OpenAICompatibleLLMAdapter


class DeepSeekLLMAdapter(OpenAICompatibleLLMAdapter):
    """DeepSeek API Adapter (deepseek-chat, deepseek-reasoner)."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.deepseek.com/v1",
        client_override: Optional[Any] = None,
        **kwargs: Any,
    ):
        super().__init__(
            api_key=api_key,
            base_url=base_url,
            client_override=client_override,
            **kwargs,
        )
