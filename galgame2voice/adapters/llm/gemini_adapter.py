"""
Google Gemini OpenAI-compatible LLM Adapter for galgame2voice.
Connects to Google Generative Language OpenAI compatibility endpoint.
"""

from typing import Any
from galgame2voice.adapters.llm.openai_adapter import OpenAICompatibleLLMAdapter


class GeminiLLMAdapter(OpenAICompatibleLLMAdapter):
    """Gemini OpenAI-compatible API Adapter (gemini-3.8-flash, gemini-3.5-flash-lite)."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai",
        client_override: Any | None = None,
        **kwargs: Any,
    ):
        super().__init__(
            api_key=api_key,
            base_url=base_url,
            client_override=client_override,
            **kwargs,
        )

    def _reasoning_fields(self, effort: str | None, model: str) -> dict[str, Any]:
        if effort == "none":
            return {"reasoning_effort": "minimal"}
        return super()._reasoning_fields(effort, model)
