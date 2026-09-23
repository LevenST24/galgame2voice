"""
Qwen / DashScope LLM Adapter for galgame2voice.
Connects to Alibaba Cloud DashScope OpenAI-compatible endpoint.
"""

from typing import Any, Optional
from galgame2voice.adapters.llm.openai_adapter import OpenAICompatibleLLMAdapter


class QwenLLMAdapter(OpenAICompatibleLLMAdapter):
    """Qwen / DashScope OpenAI-compatible API Adapter."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1",
        client_override: Optional[Any] = None,
        **kwargs: Any,
    ):
        super().__init__(
            api_key=api_key,
            base_url=base_url,
            client_override=client_override,
            **kwargs,
        )
