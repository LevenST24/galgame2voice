"""
SiliconFlow SenseVoice / FunAudioLLM STT Adapter for galgame2voice.
Supports SenseVoiceSmall high-accuracy multilingual speech recognition.
"""

from typing import Any, Optional
from galgame2voice.adapters.stt.openai_stt import OpenAICompatibleSTTAdapter


class SiliconFlowSTTAdapter(OpenAICompatibleSTTAdapter):
    """
    SiliconFlow STT adapter utilizing SenseVoiceSmall.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.siliconflow.cn/v1",
        client_override: Optional[Any] = None,
        default_model: str = "FunAudioLLM/SenseVoiceSmall",
        **kwargs: Any,
    ):
        super().__init__(
            api_key=api_key,
            base_url=base_url,
            client_override=client_override,
            default_model=default_model,
            **kwargs,
        )


# Alias
SenseVoiceSTTAdapter = SiliconFlowSTTAdapter
