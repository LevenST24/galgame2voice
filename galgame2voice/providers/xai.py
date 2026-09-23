"""
xAI Grok LLM Provider implementation for galgame2voice.
"""

from typing import Any, Optional
from galgame2voice.providers.base import BaseLLMProvider
from galgame2voice.adapters.llm.xai_adapter import XAILLMAdapter


class XAIProvider(BaseLLMProvider, XAILLMAdapter):
    """
    xAI Grok Provider supporting grok-2, grok-beta, and Grok 3.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.x.ai/v1",
        client_override: Optional[Any] = None,
        **kwargs: Any,
    ):
        XAILLMAdapter.__init__(
            self,
            api_key=api_key,
            base_url=base_url,
            client_override=client_override,
            **kwargs,
        )


__all__ = ["XAIProvider"]
