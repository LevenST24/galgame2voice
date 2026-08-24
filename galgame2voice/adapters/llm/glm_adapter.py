"""
Zhipu GLM LLM Adapter for galgame2voice.
Connects to Zhipu BigModel Open Platform API.
"""

from typing import Any, Optional
from galgame2voice.adapters.llm.openai_adapter import OpenAICompatibleLLMAdapter


class GLMLLMAdapter(OpenAICompatibleLLMAdapter):
    """Zhipu AI GLM (glm-4-plus, glm-4-flash) Adapter."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://open.bigmodel.cn/api/paas/v4",
        client_override: Optional[Any] = None,
        **kwargs: Any,
    ):
        super().__init__(
            api_key=api_key,
            base_url=base_url,
            client_override=client_override,
            **kwargs,
        )
