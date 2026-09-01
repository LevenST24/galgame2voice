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
        "default_chat_model": "gemini-2.5-flash",
        "default_stt_model": "",
        "adapter_class": GeminiLLMAdapter,
        "stt_adapter_class": None,
        "preset_models": [
            "gemini-2.5-flash",
            "gemini-2.5-pro",
            "gemini-2.0-flash",
            "gemini-1.5-flash",
            "gemini-1.5-pro",
        ],
        "description": "Google Gemini 官方 OpenAI 兼容接口 (Gemini 2.5 / 2.0 / 1.5 系列)",
    },
    "openai": {
        "id": "openai",
        "name": "OpenAI",
        "default_base_url": "https://api.openai.com/v1",
        "default_chat_model": "gpt-4o",
        "default_stt_model": "whisper-1",
        "adapter_class": OpenAICompatibleLLMAdapter,
        "stt_adapter_class": OpenAICompatibleSTTAdapter,
        "preset_models": [
            "gpt-4o",
            "gpt-4o-mini",
            "o3-mini",
            "gpt-4-turbo",
            "gpt-3.5-turbo",
        ],
        "description": "OpenAI 官方 API (GPT-4o 系列, o3-mini, Whisper)",
    },
    "deepseek": {
        "id": "deepseek",
        "name": "DeepSeek",
        "default_base_url": "https://api.deepseek.com",
        "default_chat_model": "deepseek-chat",
        "default_stt_model": "",
        "adapter_class": DeepSeekLLMAdapter,
        "stt_adapter_class": None,
        "preset_models": [
            "deepseek-chat",
            "deepseek-reasoner",
        ],
        "description": "DeepSeek 官方 API (deepseek-chat 与 deepseek-reasoner)",
    },
    "anthropic": {
        "id": "anthropic",
        "name": "Anthropic Claude",
        "default_base_url": "https://api.anthropic.com/v1",
        "default_chat_model": "claude-sonnet-4-20250514",
        "default_stt_model": "",
        "adapter_class": AnthropicAdapter,
        "stt_adapter_class": None,
        "preset_models": [
            "claude-sonnet-4-20250514",
            "claude-haiku-4-20250414",
            "claude-3-5-sonnet-20241022",
            "claude-3-5-haiku-20241022",
        ],
        "description": "Anthropic 官方 Claude 原生 Messages API (Claude Sonnet 4 / 3.5 系列)",
    },
    "xai": {
        "id": "xai",
        "name": "xAI (Grok)",
        "default_base_url": "https://api.x.ai/v1",
        "default_chat_model": "grok-3",
        "default_stt_model": "",
        "adapter_class": XAILLMAdapter,
        "stt_adapter_class": None,
        "preset_models": [
            "grok-3",
            "grok-3-mini",
            "grok-2-1212",
        ],
        "description": "xAI Grok 官方 API (Grok-3 系列)",
    },
    "glm": {
        "id": "glm",
        "name": "智谱 GLM",
        "default_base_url": "https://open.bigmodel.cn/api/paas/v4",
        "default_chat_model": "glm-4-plus",
        "default_stt_model": "",
        "adapter_class": GLMLLMAdapter,
        "stt_adapter_class": None,
        "preset_models": [
            "glm-4-plus",
            "glm-4-flash",
            "glm-4-long",
            "glm-4-air",
        ],
        "description": "智谱 BigModel 开放平台 GLM-4 系列旗舰模型",
    },
    "qwen": {
        "id": "qwen",
        "name": "通义千问 (Qwen)",
        "default_base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "default_chat_model": "qwen-max-latest",
        "default_stt_model": "qwen-audio-asr",
        "adapter_class": QwenLLMAdapter,
        "stt_adapter_class": QwenSTTAdapter,
        "preset_models": [
            "qwen-max-latest",
            "qwen-plus-latest",
            "qwen-turbo-latest",
            "qwen-long",
        ],
        "description": "阿里云百炼 DashScope 通义千问 Qwen Max / Plus / Turbo 系列",
    },
    "custom": {
        "id": "custom",
        "name": "自定义 / 本地模型 (Ollama / vLLM)",
        "default_base_url": "http://127.0.0.1:11434/v1",
        "default_chat_model": "deepseek-r1:latest",
        "default_stt_model": "",
        "adapter_class": CustomLLMAdapter,
        "stt_adapter_class": OpenAICompatibleSTTAdapter,
        "preset_models": [
            "deepseek-r1:latest",
            "qwen3:latest",
            "llama3.1:latest",
            "gemma3:latest",
        ],
        "description": "本地或私有部署的 OpenAI 兼容推理服务 (Ollama, vLLM, LMStudio)",
    },
    "groq": {
        "id": "groq",
        "name": "Groq",
        "default_base_url": "https://api.groq.com/openai/v1",
        "default_chat_model": "llama-3.3-70b-versatile",
        "default_stt_model": "whisper-large-v3",
        "adapter_class": GroqLLMAdapter,
        "stt_adapter_class": OpenAICompatibleSTTAdapter,
        "preset_models": [
            "llama-3.3-70b-versatile",
            "llama-3.1-8b-instant",
            "mixtral-8x7b-32768",
        ],
        "description": "Groq 超低延迟推理 (Llama 3.3 / 3.1, Whisper large-v3)",
    },
    "siliconflow": {
        "id": "siliconflow",
        "name": "SiliconFlow 硅基流动",
        "default_base_url": "https://api.siliconflow.cn/v1",
        "default_chat_model": "deepseek-ai/DeepSeek-V3",
        "default_stt_model": "FunAudioLLM/SenseVoiceSmall",
        "adapter_class": SiliconFlowLLMAdapter,
        "stt_adapter_class": SiliconFlowSTTAdapter,
        "preset_models": [
            "deepseek-ai/DeepSeek-V3",
            "Qwen/Qwen2.5-72B-Instruct",
            "THUDM/glm-4-9b-chat",
        ],
        "description": "硅基流动聚合推理平台 (DeepSeek-V3, Qwen2.5 系列)",
    },
    "moonshot": {
        "id": "moonshot",
        "name": "Moonshot 月之暗面 (Kimi)",
        "default_base_url": "https://api.moonshot.cn/v1",
        "default_chat_model": "moonshot-v1-32k",
        "default_stt_model": "",
        "adapter_class": MoonshotLLMAdapter,
        "stt_adapter_class": None,
        "preset_models": [
            "moonshot-v1-8k",
            "moonshot-v1-32k",
            "moonshot-v1-128k",
        ],
        "description": "月之暗面 Kimi 官方 API (moonshot-v1 长上下文系列)",
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


# Derived from PROVIDER_PRESETS so provider metadata and adapter routing
# can never drift apart.
ADAPTER_CLASS_MAP: Dict[str, Tuple[Type[BaseLLMAdapter], str]] = {
    pid: (p["adapter_class"], p["default_base_url"])
    for pid, p in PROVIDER_PRESETS.items()
}


def _resolve_provider_request(
    provider_id_or_config: Union[str, Dict[str, Any], Any],
    api_key: Optional[str],
    base_url: Optional[str],
    kwargs: Dict[str, Any],
) -> Tuple[str, str, Optional[str]]:
    """Normalizes provider id, api key, and base url from id/dict/object config."""
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

    return provider_id, key, url


def get_llm_adapter(
    provider_id_or_config: Union[str, Dict[str, Any], Any],
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    **kwargs: Any,
) -> BaseLLMAdapter:
    """
    Factory function instantiating appropriate LLM adapter based on provider ID or config.
    """
    provider_id, key, url = _resolve_provider_request(provider_id_or_config, api_key, base_url, kwargs)

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
    provider_id, key, url = _resolve_provider_request(provider_id_or_config, api_key, base_url, kwargs)

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
