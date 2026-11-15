"""
Zhipu GLM LLM Adapter for galgame2voice.
Connects to Zhipu BigModel Open Platform API.
"""

from typing import Any
from galgame2voice.adapters.base import ChatMessage
from galgame2voice.adapters.llm.openai_adapter import OpenAICompatibleLLMAdapter

_GLM53_EFFORT_MAP = {"none": "low", "low": "low", "medium": "high", "high": "high"}
_GLM_THINKING_OFF_PREFIXES = ("glm-4.5", "glm-4.6", "glm-4.7", "glm-5")


class GLMLLMAdapter(OpenAICompatibleLLMAdapter):
    """Zhipu AI GLM (glm-4.7-flash, glm-4.7-flashx, glm-5.3-flash, glm-5.3) Adapter."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://open.bigmodel.cn/api/paas/v4",
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
        m = model.strip().lower()
        if effort is None:
            return {}
        if m.startswith("glm-5.3"):
            return {"reasoning_effort": _GLM53_EFFORT_MAP.get(effort, effort)}
        if m.startswith("glm-5.2"):
            return super()._reasoning_fields(effort, model)
        if effort == "none" and m.startswith(_GLM_THINKING_OFF_PREFIXES):
            return {"thinking": {"type": "disabled"}}
        return {}

    def _build_payload(
        self, messages: list[ChatMessage], model: str, temperature: float = 1.0,
        stream: bool = False, extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = super()._build_payload(messages, model, temperature=temperature, stream=stream, extra=extra)
        if payload["temperature"] > 1:
            payload["temperature"] = 1.0
        return payload
