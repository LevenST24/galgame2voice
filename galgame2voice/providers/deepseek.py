"""
DeepSeek LLM Provider implementation for galgame2voice.
"""

from typing import Any
from galgame2voice.providers.base import BaseLLMProvider
from galgame2voice.adapters.llm.deepseek_adapter import DeepSeekLLMAdapter


class DeepSeekProvider(BaseLLMProvider, DeepSeekLLMAdapter):
    """
    DeepSeek Provider supporting deepseek-chat (V3) and deepseek-reasoner (R1).
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.deepseek.com/v1",
        client_override: Any | None = None,
        **kwargs: Any,
    ):
        DeepSeekLLMAdapter.__init__(
            self,
            api_key=api_key,
            base_url=base_url,
            client_override=client_override,
            **kwargs,
        )


__all__ = ["DeepSeekProvider"]
