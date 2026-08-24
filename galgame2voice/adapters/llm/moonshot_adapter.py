"""
Moonshot / Kimi LLM Adapter for galgame2voice.
Connects to Moonshot AI Open Platform API.
"""

from typing import Any, Optional
from galgame2voice.adapters.llm.openai_adapter import OpenAICompatibleLLMAdapter


class MoonshotLLMAdapter(OpenAICompatibleLLMAdapter):
    """Moonshot AI (moonshot-v1-8k, moonshot-v1-32k) Adapter."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.moonshot.cn/v1",
        client_override: Optional[Any] = None,
        **kwargs: Any,
    ):
        super().__init__(
            api_key=api_key,
            base_url=base_url,
            client_override=client_override,
            **kwargs,
        )
