"""
Google Gemini LLM Provider implementation for galgame2voice.
"""

from typing import Any
from galgame2voice.providers.base import BaseLLMProvider
from galgame2voice.adapters.llm.gemini_adapter import GeminiLLMAdapter


class GeminiProvider(BaseLLMProvider, GeminiLLMAdapter):
    """
    Google Gemini Provider supporting gemini-2.5-flash, gemini-2.0-flash, and gemini-1.5 series.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai",
        client_override: Any | None = None,
        **kwargs: Any,
    ):
        GeminiLLMAdapter.__init__(
            self,
            api_key=api_key,
            base_url=base_url,
            client_override=client_override,
            **kwargs,
        )


__all__ = ["GeminiProvider"]
