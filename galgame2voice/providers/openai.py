"""
OpenAI LLM Provider implementation for galgame2voice.
"""

from typing import Any, Optional
from galgame2voice.providers.base import BaseLLMProvider
from galgame2voice.adapters.llm.openai_adapter import OpenAICompatibleLLMAdapter


class OpenAIProvider(BaseLLMProvider, OpenAICompatibleLLMAdapter):
    """
    Standard OpenAI Provider supporting GPT-4o, GPT-4o-mini, o3-mini and compatible gateways.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        client_override: Optional[Any] = None,
        **kwargs: Any,
    ):
        OpenAICompatibleLLMAdapter.__init__(
            self,
            api_key=api_key,
            base_url=base_url,
            client_override=client_override,
            **kwargs,
        )


__all__ = ["OpenAIProvider"]
