"""
Groq LLM Adapter for galgame2voice.
Connects to Groq ultra-low-latency LPU inference endpoint.
"""

from typing import Any, Optional
from galgame2voice.adapters.llm.openai_adapter import OpenAICompatibleLLMAdapter


class GroqLLMAdapter(OpenAICompatibleLLMAdapter):
    """Groq API Adapter (llama-3.3-70b-versatile, mixtral-8x7b-32768, etc.)."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.groq.com/openai/v1",
        client_override: Optional[Any] = None,
        **kwargs: Any,
    ):
        super().__init__(
            api_key=api_key,
            base_url=base_url,
            client_override=client_override,
            **kwargs,
        )
