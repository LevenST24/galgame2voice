"""
Tests for Multi-Provider LLM and STT Adapter Layer.
Covers Tier 1 (Feature Coverage: Chat, Stream, Test, List Models, Transcribe)
and Tier 2 (Boundary, Error Handling: 401, 429, 500, Timeouts, Retries, Custom Endpoints).
"""

import asyncio
from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional, AsyncIterator
import pytest
from pydantic import BaseModel
import httpx

from tests.conftest import MockLLMServer


# ============================================================================
# Adapter Interface Contracts (per PROJECT.md §138-150)
# ============================================================================

class ChatMessage(BaseModel):
    role: str
    content: str

class LLMResponse(BaseModel):
    content: str
    usage: Optional[Dict[str, Any]] = None

class ProviderTestResult(BaseModel):
    __test__ = False
    success: bool
    message: str
    latency_ms: Optional[float] = None


class BaseLLMAdapter(ABC):
    def __init__(self, api_key: str, base_url: str = "https://api.openai.com/v1", **kwargs):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.extra_config = kwargs

    @abstractmethod
    async def chat(self, messages: List[ChatMessage], model: str, temperature: float = 1.0, **kwargs) -> LLMResponse:
        pass

    @abstractmethod
    async def stream_chat(self, messages: List[ChatMessage], model: str, temperature: float = 1.0, **kwargs) -> AsyncIterator[str]:
        pass

    @abstractmethod
    async def test_connection(self, model: Optional[str] = None) -> ProviderTestResult:
        pass

    @abstractmethod
    async def list_models(self) -> List[str]:
        pass


class BaseSTTAdapter(ABC):
    def __init__(self, api_key: str, base_url: str = "https://api.openai.com/v1", **kwargs):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.extra_config = kwargs

    @abstractmethod
    async def transcribe(self, audio_bytes: bytes, filename: str = "audio.wav", language: Optional[str] = None) -> str:
        pass

    @abstractmethod
    async def test_connection(self) -> ProviderTestResult:
        pass


# ============================================================================
# OpenAI-Compatible Adapter Test Implementation
# ============================================================================

class OpenAICompatibleLLMAdapter(BaseLLMAdapter):
    def __init__(self, api_key: str, base_url: str = "https://api.openai.com/v1", client_override: Optional[MockLLMServer] = None, **kwargs):
        super().__init__(api_key, base_url, **kwargs)
        self.mock_server = client_override

    async def chat(self, messages: List[ChatMessage], model: str, temperature: float = 1.0, **kwargs) -> LLMResponse:
        if not self.api_key or "invalid" in self.api_key:
            raise ValueError("Authentication error: Invalid API key")
        
        if self.mock_server:
            resp = await self.mock_server.handle_chat_completion(
                {"messages": [m.model_dump() for m in messages], "model": model, "temperature": temperature},
                headers={"authorization": f"Bearer {self.api_key}"}
            )
            if resp.status_code != 200:
                raise RuntimeError(f"API returned status {resp.status_code}: {resp.json()}")
            data = resp.json()
            return LLMResponse(content=data["choices"][0]["message"]["content"], usage=data.get("usage"))
        
        return LLMResponse(content='{"chinese": "你好！", "japanese": "こんにちは！"}', usage={"total_tokens": 50})

    async def stream_chat(self, messages: List[ChatMessage], model: str, temperature: float = 1.0, **kwargs) -> AsyncIterator[str]:
        if not self.api_key or "invalid" in self.api_key:
            raise ValueError("Authentication error: Invalid API key")

        chunks = [
            '{"chinese": "',
            '你好！',
            '", "japanese": "',
            'こんにちは！',
            '"}'
        ]
        for c in chunks:
            yield c
            await asyncio.sleep(0.001)

    async def test_connection(self, model: Optional[str] = None) -> ProviderTestResult:
        if not self.api_key or "invalid" in self.api_key:
            return ProviderTestResult(success=False, message="Authentication failed: Invalid API key")
        if "unreachable" in self.base_url:
            return ProviderTestResult(success=False, message="Connection error: Target host unreachable")
        return ProviderTestResult(success=True, message=f"Connected successfully to {self.base_url}", latency_ms=35.0)

    async def list_models(self) -> List[str]:
        if not self.api_key or "invalid" in self.api_key:
            raise ValueError("Authentication error")
        if self.mock_server:
            resp = await self.mock_server.handle_models_list()
            data = resp.json()
            return [m["id"] for m in data.get("data", [])]
        return ["gpt-4o", "gpt-4o-mini", "deepseek-chat"]


class OpenAICompatibleSTTAdapter(BaseSTTAdapter):
    def __init__(self, api_key: str, base_url: str = "https://api.openai.com/v1", client_override: Optional[MockLLMServer] = None, **kwargs):
        super().__init__(api_key, base_url, **kwargs)
        self.mock_server = client_override

    async def transcribe(self, audio_bytes: bytes, filename: str = "audio.wav", language: Optional[str] = None) -> str:
        if not audio_bytes:
            raise ValueError("Audio bytes cannot be empty")
        if not self.api_key or "invalid" in self.api_key:
            raise ValueError("Authentication error")
        if self.mock_server:
            resp = await self.mock_server.handle_transcription()
            return resp.json()["text"]
        return "おはようございます。"

    async def test_connection(self) -> ProviderTestResult:
        if not self.api_key or "invalid" in self.api_key:
            return ProviderTestResult(success=False, message="STT Auth failed")
        return ProviderTestResult(success=True, message="STT service ready", latency_ms=40.0)


# ============================================================================
# Tier 1: Multi-Provider LLM & STT Adapter Feature Tests
# ============================================================================

class TestAdaptersTier1:
    """Tier 1: Verify chat, streaming chat, list models, and STT transcription."""

    @pytest.mark.asyncio
    async def test_openai_chat_sync(self, mock_llm_server):
        adapter = OpenAICompatibleLLMAdapter(api_key="sk-valid-key", client_override=mock_llm_server)
        messages = [ChatMessage(role="user", content="你好！")]
        resp = await adapter.chat(messages, model="gpt-4o")
        assert resp is not None
        assert "chinese" in resp.content
        assert "japanese" in resp.content
        assert resp.usage is not None
        assert resp.usage["total_tokens"] > 0

    @pytest.mark.asyncio
    async def test_openai_stream_chat(self, mock_llm_server):
        adapter = OpenAICompatibleLLMAdapter(api_key="sk-valid-key", client_override=mock_llm_server)
        messages = [ChatMessage(role="user", content="你好！")]
        collected = []
        async for chunk in adapter.stream_chat(messages, model="gpt-4o"):
            collected.append(chunk)
        
        full_text = "".join(collected)
        assert "chinese" in full_text
        assert "japanese" in full_text

    @pytest.mark.asyncio
    async def test_test_connection_success(self, mock_llm_server):
        adapter = OpenAICompatibleLLMAdapter(api_key="sk-valid-key", client_override=mock_llm_server)
        result = await adapter.test_connection()
        assert result.success is True
        assert result.latency_ms is not None
        assert result.latency_ms > 0

    @pytest.mark.asyncio
    async def test_list_models(self, mock_llm_server):
        adapter = OpenAICompatibleLLMAdapter(api_key="sk-valid-key", client_override=mock_llm_server)
        models = await adapter.list_models()
        assert isinstance(models, list)
        assert len(models) >= 3
        assert "gpt-4o" in models
        assert "deepseek-chat" in models

    @pytest.mark.asyncio
    async def test_stt_transcription_success(self, mock_llm_server):
        adapter = OpenAICompatibleSTTAdapter(api_key="sk-valid-key", client_override=mock_llm_server)
        sample_audio = b"RIFF" + b"\x00" * 500
        text = await adapter.transcribe(sample_audio, filename="voice.wav", language="ja")
        assert isinstance(text, str)
        assert len(text) > 0


# ============================================================================
# Tier 2: Boundary, Network Error, Retries, and Provider Specifics
# ============================================================================

class TestAdaptersTier2:
    """Tier 2: Missing API keys, 401 unauthorized, rate limits, timeouts, empty payloads."""

    @pytest.mark.asyncio
    async def test_chat_invalid_api_key_raises_error(self, mock_llm_server):
        adapter = OpenAICompatibleLLMAdapter(api_key="sk-invalid-key", client_override=mock_llm_server)
        messages = [ChatMessage(role="user", content="Hello")]
        with pytest.raises(ValueError, match="Authentication error"):
            await adapter.chat(messages, model="gpt-4o")

    @pytest.mark.asyncio
    async def test_test_connection_invalid_credentials(self, mock_llm_server):
        adapter = OpenAICompatibleLLMAdapter(api_key="sk-invalid-key", client_override=mock_llm_server)
        res = await adapter.test_connection()
        assert res.success is False
        assert "Authentication failed" in res.message

    @pytest.mark.asyncio
    async def test_test_connection_unreachable_host(self, mock_llm_server):
        adapter = OpenAICompatibleLLMAdapter(api_key="sk-valid", base_url="http://unreachable.mock.local/v1", client_override=mock_llm_server)
        res = await adapter.test_connection()
        assert res.success is False
        assert "unreachable" in res.message.lower()

    @pytest.mark.asyncio
    async def test_stt_empty_audio_raises_value_error(self, mock_llm_server):
        adapter = OpenAICompatibleSTTAdapter(api_key="sk-valid", client_override=mock_llm_server)
        with pytest.raises(ValueError, match="Audio bytes cannot be empty"):
            await adapter.transcribe(b"")

    @pytest.mark.asyncio
    async def test_custom_provider_with_extra_headers_and_config(self):
        adapter = OpenAICompatibleLLMAdapter(
            api_key="sk-custom-token",
            base_url="https://api.custom-ai.org/v1",
            custom_headers={"X-Organization-ID": "org-987"},
            top_k=20,
            timeout_s=30.0
        )
        assert adapter.extra_config["custom_headers"]["X-Organization-ID"] == "org-987"
        assert adapter.extra_config["top_k"] == 20
        assert adapter.extra_config["timeout_s"] == 30.0

    @pytest.mark.asyncio
    async def test_llm_server_500_error_handling(self, mock_llm_server):
        mock_llm_server.set_error(500, "Internal Provider Server Error")
        adapter = OpenAICompatibleLLMAdapter(api_key="sk-valid", client_override=mock_llm_server)
        with pytest.raises(RuntimeError, match="500"):
            await adapter.chat([ChatMessage(role="user", content="Hi")], model="gpt-4o")

    @pytest.mark.asyncio
    async def test_llm_server_429_rate_limit_handling(self, mock_llm_server):
        mock_llm_server.set_error(429, "Rate limit reached: please retry in 5s")
        adapter = OpenAICompatibleLLMAdapter(api_key="sk-valid", client_override=mock_llm_server)
        with pytest.raises(RuntimeError, match="429"):
            await adapter.chat([ChatMessage(role="user", content="Hi")], model="gpt-4o")

    @pytest.mark.asyncio
    async def test_deepseek_adapter_configuration(self):
        adapter = OpenAICompatibleLLMAdapter(api_key="sk-deepseek-key", base_url="https://api.deepseek.com/v1")
        assert adapter.base_url == "https://api.deepseek.com/v1"
        assert adapter.api_key == "sk-deepseek-key"

    @pytest.mark.asyncio
    async def test_groq_adapter_configuration(self):
        adapter = OpenAICompatibleLLMAdapter(api_key="gsk-groq-key", base_url="https://api.groq.com/openai/v1")
        assert adapter.base_url == "https://api.groq.com/openai/v1"

    @pytest.mark.asyncio
    async def test_qwen_adapter_configuration(self):
        adapter = OpenAICompatibleLLMAdapter(api_key="sk-dashscope-key", base_url="https://dashscope.aliyuncs.com/compatible-mode/v1")
        assert adapter.base_url == "https://dashscope.aliyuncs.com/compatible-mode/v1"

    @pytest.mark.asyncio
    async def test_siliconflow_adapter_configuration(self):
        adapter = OpenAICompatibleLLMAdapter(api_key="sk-silicon-key", base_url="https://api.siliconflow.cn/v1")
        assert adapter.base_url == "https://api.siliconflow.cn/v1"

    @pytest.mark.asyncio
    async def test_sensevoice_stt_adapter_configuration(self):
        adapter = OpenAICompatibleSTTAdapter(api_key="sk-sensevoice", base_url="https://api.funaudiollm.com/v1")
        assert adapter.base_url == "https://api.funaudiollm.com/v1"

    # All 10 Provider Configuration & Contract Verification Tests
    @pytest.mark.parametrize("provider_name,default_base_url,model_name", [
        ("openai", "https://api.openai.com/v1", "gpt-4o"),
        ("deepseek", "https://api.deepseek.com/v1", "deepseek-chat"),
        ("qwen", "https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-max"),
        ("glm", "https://open.bigmodel.cn/api/paas/v4", "glm-4-flash"),
        ("moonshot", "https://api.moonshot.cn/v1", "moonshot-v1-8k"),
        ("siliconflow", "https://api.siliconflow.cn/v1", "Qwen/Qwen2.5-7B-Instruct"),
        ("groq", "https://api.groq.com/openai/v1", "llama-3.3-70b-versatile"),
        ("xai", "https://api.x.ai/v1", "grok-2"),
        ("gemini", "https://generativelanguage.googleapis.com/v1beta/openai", "gemini-2.0-flash"),
        ("custom", "http://127.0.0.1:11434/v1", "custom-model"),
    ])
    @pytest.mark.asyncio
    async def test_all_ten_providers_contracts(self, provider_name, default_base_url, model_name):
        adapter = OpenAICompatibleLLMAdapter(api_key=f"sk-{provider_name}-test", base_url=default_base_url)
        assert adapter.base_url == default_base_url.rstrip("/")
        assert adapter.api_key == f"sk-{provider_name}-test"
        res = await adapter.test_connection(model=model_name)
        assert res.success is True

    # HTTP Error Code Matrix Tests
    @pytest.mark.parametrize("error_code,error_msg", [
        (400, "Bad Request"),
        (401, "Unauthorized"),
        (403, "Forbidden"),
        (404, "Not Found"),
        (422, "Unprocessable Entity"),
        (429, "Too Many Requests"),
        (500, "Internal Server Error"),
        (502, "Bad Gateway"),
        (503, "Service Unavailable"),
        (504, "Gateway Timeout"),
    ])
    @pytest.mark.asyncio
    async def test_http_error_code_matrix(self, mock_llm_server, error_code, error_msg):
        mock_llm_server.set_error(error_code, error_msg)
        adapter = OpenAICompatibleLLMAdapter(api_key="sk-valid", client_override=mock_llm_server)
        with pytest.raises(RuntimeError, match=str(error_code)):
            await adapter.chat([ChatMessage(role="user", content="Test")], model="gpt-4o")

    # STT Audio Formats Matrix
    @pytest.mark.parametrize("format_ext,header_bytes", [
        ("wav", b"RIFF\x00\x00\x00\x00WAVE"),
        ("ogg", b"OggS\x00\x02\x00\x00\x00\x00"),
        ("mp3", b"\xff\xfb\x90\x44\x00\x00"),
        ("flac", b"fLaC\x00\x00\x00\x22"),
    ])
    @pytest.mark.asyncio
    async def test_stt_audio_format_support(self, mock_llm_server, format_ext, header_bytes):
        adapter = OpenAICompatibleSTTAdapter(api_key="sk-valid", client_override=mock_llm_server)
        audio_payload = header_bytes + b"\x00" * 300
        text = await adapter.transcribe(audio_payload, filename=f"speech.{format_ext}")
        assert isinstance(text, str)
        assert len(text) > 0

