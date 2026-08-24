"""
SiliconFlow LLM Adapter for galgame2voice.
Connects to SiliconFlow API for hosting open-source models.
"""

from typing import Any, Optional
from galgame2voice.adapters.llm.openai_adapter import OpenAICompatibleLLMAdapter


class SiliconFlowLLMAdapter(OpenAICompatibleLLMAdapter):
    """SiliconFlow API Adapter (DeepSeek-V3, Qwen2.5, etc.)."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.siliconflow.cn/v1",
        client_override: Optional[Any] = None,
        **kwargs: Any,
    ):
        super().__init__(
            api_key=api_key,
            base_url=base_url,
            client_override=client_override,
            **kwargs,
        )
