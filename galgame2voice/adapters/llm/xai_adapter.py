"""
xAI Grok LLM Adapter for galgame2voice.
Connects to xAI API (grok-2, grok-beta).
"""

from typing import Any, Optional
from galgame2voice.adapters.llm.openai_adapter import OpenAICompatibleLLMAdapter


class XAILLMAdapter(OpenAICompatibleLLMAdapter):
    """xAI API Adapter (grok-3, grok-3-mini, grok-2)."""
    default_model = "grok-3"
    provider_id = "xai"

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.x.ai/v1",
        client_override: Optional[Any] = None,
        default_model: Optional[str] = None,
        **kwargs: Any,
    ):
        super().__init__(
            api_key=api_key,
            base_url=base_url,
            client_override=client_override,
            default_model=default_model or "grok-3",
            **kwargs,
        )
