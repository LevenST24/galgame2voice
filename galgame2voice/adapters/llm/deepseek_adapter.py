"""
DeepSeek LLM Adapter for galgame2voice.
Connects to DeepSeek API with optimal presets.
"""

from typing import Any
from galgame2voice.adapters.llm.openai_adapter import OpenAICompatibleLLMAdapter


class DeepSeekLLMAdapter(OpenAICompatibleLLMAdapter):
    """DeepSeek API Adapter (deepseek-flash, deepseek-v4-pro)."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.deepseek.com/v1",
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
            return {"thinking": {"type": "disabled"}}
        return super()._reasoning_fields(effort, model)
