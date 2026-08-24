"""
DashScope / Qwen Audio STT Adapter for galgame2voice.
Supports Alibaba Cloud DashScope ASR / Audio transcription.
"""

from typing import Any, Optional
from galgame2voice.adapters.stt.openai_stt import OpenAICompatibleSTTAdapter


class QwenSTTAdapter(OpenAICompatibleSTTAdapter):
    """
    DashScope ASR / Qwen Audio STT adapter.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1",
        client_override: Optional[Any] = None,
        default_model: str = "qwen-audio-asr",
        **kwargs: Any,
    ):
        super().__init__(
            api_key=api_key,
            base_url=base_url,
            client_override=client_override,
            default_model=default_model,
            **kwargs,
        )
