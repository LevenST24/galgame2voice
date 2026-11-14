"""
Moonshot / Kimi LLM Adapter for galgame2voice.
Connects to Moonshot AI Open Platform API.
"""

from typing import Any
from galgame2voice.adapters.base import ChatMessage
from galgame2voice.adapters.llm.openai_adapter import OpenAICompatibleLLMAdapter

_K3_EFFORT_MAP = {"none": "low", "low": "low", "medium": "high", "high": "high"}
_K3_FIXED_PARAMS = ("temperature", "top_p", "presence_penalty", "frequency_penalty")


class MoonshotLLMAdapter(OpenAICompatibleLLMAdapter):
    """Moonshot AI (kimi-k3.1-pro, kimi-k3.1-pro-highspeed, kimi-k3) Adapter."""

    _MAX_TOKENS_KEY = "max_completion_tokens"

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.moonshot.cn/v1",
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
        if model.strip().lower().startswith("kimi-k3"):
            effort = _K3_EFFORT_MAP.get(effort, effort)
        return super()._reasoning_fields(effort, model)

    def _build_payload(
        self, messages: list[ChatMessage], model: str, temperature: float = 1.0,
        stream: bool = False, extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = super()._build_payload(messages, model, temperature=temperature, stream=stream, extra=extra)
        if model.strip().lower().startswith("kimi-k3"):
            for key in _K3_FIXED_PARAMS:
                payload.pop(key, None)
            if "max_tokens" in payload:
                payload["max_completion_tokens"] = payload.pop("max_tokens")
        return payload
