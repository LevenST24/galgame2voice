"""
xAI Grok LLM Adapter for galgame2voice.
Connects to xAI API (grok-4.7, grok-4.20-0309-non-reasoning).
"""

from typing import Any
from galgame2voice.adapters.base import ChatMessage
from galgame2voice.adapters.llm.openai_adapter import OpenAICompatibleLLMAdapter


class XAILLMAdapter(OpenAICompatibleLLMAdapter):
    """xAI API Adapter (grok-4.7, grok-4.20-0309-non-reasoning)."""
    default_model = "grok-4.7"
    provider_id = "xai"

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.x.ai/v1",
        client_override: Any | None = None,
        default_model: str | None = None,
        **kwargs: Any,
    ):
        super().__init__(
            api_key=api_key,
            base_url=base_url,
            client_override=client_override,
            default_model=default_model or "grok-4.7",
            **kwargs,
        )

    def _reasoning_fields(self, effort: str | None, model: str) -> dict[str, Any]:
        if "non-reasoning" in model:
            return {}
        if effort == "none":
            return {"reasoning_effort": "low"}
        return super()._reasoning_fields(effort, model)

    def _build_payload(
        self,
        messages: list[ChatMessage],
        model: str,
        temperature: float = 1.0,
        stream: bool = False,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = super()._build_payload(messages, model, temperature=temperature, stream=stream, extra=extra)
        if "non-reasoning" not in model:
            for key in ("frequency_penalty", "presence_penalty", "stop"):
                payload.pop(key, None)
        return payload
