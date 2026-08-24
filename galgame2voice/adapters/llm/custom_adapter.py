"""
Custom OpenAI-compatible LLM Adapter for galgame2voice.
Connects to local/self-hosted endpoints (Ollama, vLLM, LM Studio, LocalAI, etc.).
"""

from typing import Any, Optional
from galgame2voice.adapters.llm.openai_adapter import OpenAICompatibleLLMAdapter


class CustomLLMAdapter(OpenAICompatibleLLMAdapter):
    """Custom / Local OpenAI-compatible API Adapter."""

    def __init__(
        self,
        api_key: str = "custom",
        base_url: str = "http://127.0.0.1:11434/v1",
        client_override: Optional[Any] = None,
        **kwargs: Any,
    ):
        # Allow default or user api_key for local proxies
        super().__init__(
            api_key=api_key or "custom",
            base_url=base_url,
            client_override=client_override,
            **kwargs,
        )
