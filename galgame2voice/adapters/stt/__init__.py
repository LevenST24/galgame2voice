"""
Speech-to-Text (STT) Adapters package for galgame2voice.
Exports OpenAI Whisper, SiliconFlow SenseVoice, and DashScope Qwen ASR adapters.
"""

from galgame2voice.adapters.stt.openai_stt import OpenAICompatibleSTTAdapter, OpenAISTTAdapter
from galgame2voice.adapters.stt.siliconflow_stt import SiliconFlowSTTAdapter, SenseVoiceSTTAdapter
from galgame2voice.adapters.stt.qwen_stt import QwenSTTAdapter

__all__ = [
    "OpenAICompatibleSTTAdapter",
    "OpenAISTTAdapter",
    "SiliconFlowSTTAdapter",
    "SenseVoiceSTTAdapter",
    "QwenSTTAdapter",
]
