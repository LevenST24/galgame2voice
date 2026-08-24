"""
Provider Registry and Adapter Factory for galgame2voice.
Maintains default configurations and model presets for 10+ major LLM and STT providers.
"""

from typing import Dict, Any, List, Optional, Type, Union, Tuple

from galgame2voice.adapters.base import BaseLLMAdapter, BaseSTTAdapter
from galgame2voice.adapters.llm import (
    OpenAICompatibleLLMAdapter,
    DeepSeekLLMAdapter,
    QwenLLMAdapter,
    GLMLLMAdapter,
    MoonshotLLMAdapter,
    SiliconFlowLLMAdapter,
    GroqLLMAdapter,
    XAILLMAdapter,
    GeminiLLMAdapter,
    AnthropicAdapter,
    CustomLLMAdapter,
)
from galgame2voice.adapters.stt import (
    OpenAICompatibleSTTAdapter,
    SiliconFlowSTTAdapter,
    QwenSTTAdapter,
)


PROVIDER_PRESETS: Dict[str, Dict[str, Any]] = {
    "gemini": {
        "id": "gemini",
        "name": "Google Gemini",
        "default_base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "default_chat_model": "gemini-3.7-flash",
        "default_stt_model": "",
        "adapter_class": GeminiLLMAdapter,
        "stt_adapter_class": None,
        "preset_models": [
            "gemini-3.7-flash",
            "gemini-3.6-flash",
            "gemini-3.5-flash",
            "gemini-3.5-flash-lite",
            "gemini-3.1-pro",
            "gemini-2.5-pro",
            "gemini-2.5-flash",
        ],
        "description": "Google Gemini 官方 OpenAI 兼容接口 (Gemini 3.7 / 3.x 系列)",
    },
    "openai": {
        "id": "openai",
        "name": "OpenAI",
        "default_base_url": "https://api.openai.com/v1",
        "default_chat_model": "gpt-5.6-sol",
        "default_stt_model": "whisper-1",
        "adapter_class": OpenAICompatibleLLMAdapter,
        "stt_adapter_class": OpenAICompatibleSTTAdapter,
        "preset_models": [
            "gpt-5.6-sol",
            "gpt-5.6-terra",
            "gpt-5.6-luna",
            "o4-mini",
            "gpt-4o",
            "gpt-4o-mini",
        ],
        "description": "OpenAI 官方 API (GPT-5.6 系列, o4-mini, Whisper)",
    },
    "deepseek": {
        "id": "deepseek",
        "name": "DeepSeek",
        "default_base_url": "https://api.deepseek.com",
        "default_chat_model": "deepseek-v4-pro",
        "default_stt_model": "",
        "adapter_class": DeepSeekLLMAdapter,
        "stt_adapter_class": None,
        "preset_models": [
            "deepseek-v4-pro",
            "deepseek-v4-flash",
            "deepseek-v4-flash-vision-exp",
            "deepseek-chat",
            "deepseek-reasoner",
        ],
        "description": "DeepSeek 官方 API (DeepSeek-V4 系列与原生思考模式)",
    },
    "anthropic": {
        "id": "anthropic",
        "name": "Anthropic Claude",
        "default_base_url": "https://api.anthropic.com/v1",
        "default_chat_model": "claude-5-sonnet-latest",
        "default_stt_model": "",
        "adapter_class": AnthropicAdapter,
        "stt_adapter_class": None,
        "preset_models": [
            "claude-5-opus-latest",
            "claude-5-sonnet-latest",
            "claude-5-fable-latest",
            "claude-haiku-4.5",
            "claude-3-7-sonnet-latest",
            "claude-3-5-sonnet-latest",
        ],
        "description": "Anthropic 官方 Claude 原生 Messages API (Claude 5 / 3.7 系列)",
    },
    "xai": {
        "id": "xai",
        "name": "xAI (Grok)",
        "default_base_url": "https://api.x.ai/v1",
        "default_chat_model": "grok-4.6",
        "default_stt_model": "",
        "adapter_class": XAILLMAdapter,
        "stt_adapter_class": None,
        "preset_models": [
            "grok-4.6",
            "grok-4.5",
            "grok-4.3",
            "grok-2-latest",
        ],
        "description": "xAI Grok 官方 API (Grok-4.6 旗舰系列)",
    },
    "glm": {
        "id": "glm",
        "name": "智谱 GLM",
        "default_base_url": "https://open.bigmodel.cn/api/paas/v4",
        "default_chat_model": "glm-5.3",
        "default_stt_model": "",
        "adapter_class": GLMLLMAdapter,
        "stt_adapter_class": None,
        "preset_models": [
            "glm-5.3",
            "glm-5.2",
            "glm-5.1",
            "glm-5-turbo",
            "glm-5-flash",
            "glm-4.7-flash",
            "glm-4-plus",
        ],
        "description": "智谱 BigModel 开放平台 GLM-5 系列旗舰与思考模型",
    },
    "qwen": {
        "id": "qwen",
        "name": "通义千问 (Qwen)",
        "default_base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "default_chat_model": "qwen3.8-max",
        "default_stt_model": "qwen-audio-asr",
        "adapter_class": QwenLLMAdapter,
        "stt_adapter_class": QwenSTTAdapter,
        "preset_models": [
            "qwen3.8-max",
            "qwen3.8-27b",
            "qwen3.6-flash",
            "qwen-max-latest",
            "qwen-plus-latest",
            "qwq-32b-preview",
            "qwen2.5-72b-instruct",
        ],
        "description": "阿里云百炼 DashScope 通义千问 Qwen3.8 / QwQ 系列",
    },
    "custom": {
        "id": "custom",
        "name": "自定义 / 本地模型 (Ollama / vLLM)",
        "default_base_url": "http://127.0.0.1:11434/v1",
        "default_chat_model": "deepseek-v4:latest",
        "default_stt_model": "",
        "adapter_class": CustomLLMAdapter,
        "stt_adapter_class": OpenAICompatibleSTTAdapter,
        "preset_models": [
            "deepseek-v4:latest",
            "deepseek-r1:70b",
            "deepseek-r1:32b",
            "deepseek-r1:14b",
            "deepseek-r1:8b",
            "qwen3.8:27b",
            "qwen2.5:72b",
            "qwen2.5:32b",
            "qwen2.5:14b",
            "glm-5.2:latest",
            "llama3.3:70b",
            "gemma2:9b",
        ],
        "description": "本地或私有部署的 OpenAI 兼容推理服务 (Ollama, vLLM, LMStudio)",
    },
}


def list_provider_presets() -> List[Dict[str, Any]]:
    """Returns a list of all built-in provider preset descriptions."""
    results = []
    for pid, p in PROVIDER_PRESETS.items():
        results.append({
            "id": p["id"],
            "name": p["name"],
            "default_base_url": p["default_base_url"],
            "default_chat_model": p["default_chat_model"],
            "default_stt_model": p["default_stt_model"],
            "preset_models": p["preset_models"],
            "description": p["description"],
        })
    return results


def get_provider_preset(provider_id: str) -> Optional[Dict[str, Any]]:
    """Retrieves preset information for a specific provider ID."""
    p = PROVIDER_PRESETS.get(provider_id.lower())
    if not p:
        return None
    return {
        "id": p["id"],
        "name": p["name"],
        "default_base_url": p["default_base_url"],
        "default_chat_model": p["default_chat_model"],
        "default_stt_model": p["default_stt_model"],
        "preset_models": p["preset_models"],
        "description": p["description"],
    }


ADAPTER_CLASS_MAP: Dict[str, Tuple[Type[BaseLLMAdapter], str]] = {
    "gemini": (GeminiLLMAdapter, "https://generativelanguage.googleapis.com/v1beta/openai"),
    "openai": (OpenAICompatibleLLMAdapter, "https://api.openai.com/v1"),
    "deepseek": (DeepSeekLLMAdapter, "https://api.deepseek.com"),
    "anthropic": (AnthropicAdapter, "https://api.anthropic.com/v1"),
    "xai": (XAILLMAdapter, "https://api.x.ai/v1"),
    "glm": (GLMLLMAdapter, "https://open.bigmodel.cn/api/paas/v4"),
    "qwen": (QwenLLMAdapter, "https://dashscope.aliyuncs.com/compatible-mode/v1"),
    "custom": (CustomLLMAdapter, "http://127.0.0.1:11434/v1"),
    "groq": (GroqLLMAdapter, "https://api.groq.com/openai/v1"),
    "siliconflow": (SiliconFlowLLMAdapter, "https://api.siliconflow.cn/v1"),
    "moonshot": (MoonshotLLMAdapter, "https://api.moonshot.cn/v1"),
}


def get_llm_adapter(
    provider_id_or_config: Union[str, Dict[str, Any], Any],
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    **kwargs: Any,
) -> BaseLLMAdapter:
    """
    Factory function instantiating appropriate LLM adapter based on provider ID or config.
    """
    provider_id = "openai"
    key = api_key or ""
    url = base_url

    # Check if a model/config object or dict was passed
    if isinstance(provider_id_or_config, str):
        provider_id = provider_id_or_config.lower()
    elif isinstance(provider_id_or_config, dict):
        provider_id = str(provider_id_or_config.get("provider_type") or provider_id_or_config.get("id") or "openai").lower()
        key = key or provider_id_or_config.get("api_key", "")
        url = url or provider_id_or_config.get("api_base_url") or provider_id_or_config.get("base_url")
        custom_headers = provider_id_or_config.get("custom_headers")
        if custom_headers and "custom_headers" not in kwargs:
            kwargs["custom_headers"] = custom_headers
    elif hasattr(provider_id_or_config, "id"):
        provider_id = str(getattr(provider_id_or_config, "provider_type", None) or getattr(provider_id_or_config, "id", "openai")).lower()
        key = key or getattr(provider_id_or_config, "api_key", "")
        url = url or getattr(provider_id_or_config, "api_base_url", None) or getattr(provider_id_or_config, "base_url", None)
        custom_headers = getattr(provider_id_or_config, "custom_headers", None)
        if custom_headers and "custom_headers" not in kwargs:
            kwargs["custom_headers"] = custom_headers

    if provider_id in ADAPTER_CLASS_MAP:
        adapter_cls, default_url = ADAPTER_CLASS_MAP[provider_id]
        target_url = url or default_url
        return adapter_cls(api_key=key, base_url=target_url, **kwargs)

    preset = PROVIDER_PRESETS.get(provider_id)
    if preset:
        adapter_cls: Type[BaseLLMAdapter] = preset.get("adapter_class", OpenAICompatibleLLMAdapter)
        target_url = url or preset["default_base_url"]
        return adapter_cls(api_key=key, base_url=target_url, **kwargs)

    # Fallback to general OpenAI-compatible adapter
    target_url = url or "https://api.openai.com/v1"
    return OpenAICompatibleLLMAdapter(api_key=key, base_url=target_url, **kwargs)


def get_stt_adapter(
    provider_id_or_config: Union[str, Dict[str, Any], Any],
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    **kwargs: Any,
) -> BaseSTTAdapter:
    """
    Factory function instantiating appropriate STT adapter based on provider ID or config.
    """
    provider_id = "openai"
    key = api_key or ""
    url = base_url

    if isinstance(provider_id_or_config, str):
        provider_id = provider_id_or_config.lower()
    elif isinstance(provider_id_or_config, dict):
        provider_id = str(provider_id_or_config.get("provider_type") or provider_id_or_config.get("id") or "openai").lower()
        key = key or provider_id_or_config.get("api_key", "")
        url = url or provider_id_or_config.get("api_base_url") or provider_id_or_config.get("base_url")
        custom_headers = provider_id_or_config.get("custom_headers")
        if custom_headers and "custom_headers" not in kwargs:
            kwargs["custom_headers"] = custom_headers
    elif hasattr(provider_id_or_config, "id"):
        provider_id = str(getattr(provider_id_or_config, "provider_type", None) or getattr(provider_id_or_config, "id", "openai")).lower()
        key = key or getattr(provider_id_or_config, "api_key", "")
        url = url or getattr(provider_id_or_config, "api_base_url", None) or getattr(provider_id_or_config, "base_url", None)
        custom_headers = getattr(provider_id_or_config, "custom_headers", None)
        if custom_headers and "custom_headers" not in kwargs:
            kwargs["custom_headers"] = custom_headers

    if provider_id == "siliconflow":
        target_url = url or "https://api.siliconflow.cn/v1"
        return SiliconFlowSTTAdapter(api_key=key, base_url=target_url, **kwargs)
    elif provider_id == "qwen":
        target_url = url or "https://dashscope.aliyuncs.com/compatible-mode/v1"
        return QwenSTTAdapter(api_key=key, base_url=target_url, **kwargs)
    elif provider_id == "groq":
        target_url = url or "https://api.groq.com/openai/v1"
        return OpenAICompatibleSTTAdapter(api_key=key, base_url=target_url, default_model="whisper-large-v3", **kwargs)

    target_url = url or "https://api.openai.com/v1"
    return OpenAICompatibleSTTAdapter(api_key=key, base_url=target_url, **kwargs)
