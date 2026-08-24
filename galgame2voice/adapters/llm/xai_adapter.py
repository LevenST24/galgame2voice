"""
xAI Grok LLM Adapter for galgame2voice.
Connects to xAI API (grok-2, grok-beta).
"""

from typing import Any, Optional
from galgame2voice.adapters.llm.openai_adapter import OpenAICompatibleLLMAdapter


class XAILLMAdapter(OpenAICompatibleLLMAdapter):
    """xAI API Adapter (grok-2)."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.x.ai/v1",
        client_override: Optional[Any] = None,
        **kwargs: Any,
    ):
        super().__init__(
            api_key=api_key,
            base_url=base_url,
            client_override=client_override,
            **kwargs,
        )
