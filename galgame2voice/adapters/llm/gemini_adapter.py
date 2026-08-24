"""
Google Gemini OpenAI-compatible LLM Adapter for galgame2voice.
Connects to Google Generative Language OpenAI compatibility endpoint.
"""

from typing import Any, Optional
from galgame2voice.adapters.llm.openai_adapter import OpenAICompatibleLLMAdapter


class GeminiLLMAdapter(OpenAICompatibleLLMAdapter):
    """Gemini OpenAI-compatible API Adapter (gemini-2.0-flash, gemini-1.5-pro)."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai",
        client_override: Optional[Any] = None,
        **kwargs: Any,
    ):
        super().__init__(
            api_key=api_key,
            base_url=base_url,
            client_override=client_override,
            **kwargs,
        )
