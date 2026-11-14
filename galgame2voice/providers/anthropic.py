"""
Anthropic Claude LLM Provider implementation for galgame2voice.
"""

from typing import Any
from galgame2voice.providers.base import BaseLLMProvider
from galgame2voice.adapters.llm.anthropic_adapter import AnthropicAdapter


class AnthropicProvider(BaseLLMProvider, AnthropicAdapter):
    """
    Anthropic Claude Provider supporting claude-sonnet-5-5, claude-haiku-5-5, and claude-opus-5-5.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.anthropic.com/v1",
        client_override: Any | None = None,
        **kwargs: Any,
    ):
        AnthropicAdapter.__init__(
            self,
            api_key=api_key,
            base_url=base_url,
            client_override=client_override,
            **kwargs,
        )


__all__ = ["AnthropicProvider"]
