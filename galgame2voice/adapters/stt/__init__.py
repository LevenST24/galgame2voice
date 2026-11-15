"""
Speech-to-Text (STT) Adapters package for galgame2voice.
Exports OpenAI Whisper, SiliconFlow SenseVoice, and DashScope Qwen ASR adapters.
"""

from galgame2voice.adapters.stt.openai_stt import OpenAICompatibleSTTAdapter
from galgame2voice.adapters.stt.siliconflow_stt import SiliconFlowSTTAdapter
from galgame2voice.adapters.stt.qwen_stt import QwenSTTAdapter

__all__ = [
    "OpenAICompatibleSTTAdapter",
    "SiliconFlowSTTAdapter",
    "QwenSTTAdapter",
]
