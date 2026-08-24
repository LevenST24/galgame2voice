"""
Tests for Milestone 2 Multi-Provider Adapters and Config Router.
Covers factory methods, registry presets, specialized LLM/STT adapter subclasses,
real-time testing, model discovery, and REST API router endpoints.
"""

import json
import pytest
from httpx import AsyncClient, ASGITransport
from pydantic import BaseModel

from galgame2voice.adapters.base import (
    ChatMessage,
    LLMResponse,
    TestResult,
    BaseLLMAdapter,
    BaseSTTAdapter,
)
from galgame2voice.adapters.registry import (
    PROVIDER_PRESETS,
    list_provider_presets,
    get_provider_preset,
    get_llm_adapter,
    get_stt_adapter,
)
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
    CustomLLMAdapter,
)
from galgame2voice.adapters.stt import (
    OpenAICompatibleSTTAdapter,
    SiliconFlowSTTAdapter,
    QwenSTTAdapter,
)
from galgame2voice.database.models import ProviderInDB, ProviderCreate
from galgame2voice.main import create_app
from tests.conftest import MockLLMServer, MockGptSovitsServer


class TestAdapterRegistryAndFactory:
    """Tests for Provider Registry presets and dynamic adapter factory instantiation."""

    def test_list_provider_presets(self):
        presets = list_provider_presets()
        assert isinstance(presets, list)
        assert len(presets) >= 8
        provider_ids = [p["id"] for p in presets]
        for expected in ["gemini", "openai", "deepseek", "anthropic", "xai", "glm", "qwen", "custom"]:
            assert expected in provider_ids

    def test_get_provider_preset_valid_and_invalid(self):
        deepseek = get_provider_preset("deepseek")
        assert deepseek is not None
        assert deepseek["id"] == "deepseek"
        assert "deepseek-chat" in deepseek["default_chat_model"]

        nonexistent = get_provider_preset("unknown_provider_xyz")
        assert nonexistent is None

    def test_get_llm_adapter_by_id_string(self):
        adapter = get_llm_adapter("deepseek", api_key="sk-test-key")
        assert isinstance(adapter, DeepSeekLLMAdapter)
        assert adapter.api_key == "sk-test-key"
        assert "deepseek" in adapter.base_url

        qwen_adapter = get_llm_adapter("qwen", api_key="sk-qwen-key")
        assert isinstance(qwen_adapter, QwenLLMAdapter)

        groq_adapter = get_llm_adapter("groq", api_key="gsk-groq-key")
        assert isinstance(groq_adapter, GroqLLMAdapter)

        custom_adapter = get_llm_adapter("custom", base_url="http://localhost:8000/v1")
        assert isinstance(custom_adapter, CustomLLMAdapter)

    def test_get_llm_adapter_by_dict_and_model(self):
        config_dict = {
            "provider_type": "glm",
            "api_key": "glm-token-123",
            "api_base_url": "https://open.bigmodel.cn/api/paas/v4",
            "custom_headers": {"X-Test": "1"},
        }
        adapter = get_llm_adapter(config_dict)
        assert isinstance(adapter, GLMLLMAdapter)
        assert adapter.api_key == "glm-token-123"
        assert adapter.extra_config["custom_headers"]["X-Test"] == "1"

        provider_obj = ProviderInDB(
            id="moonshot",
            name="Moonshot",
            api_base_url="https://api.moonshot.cn/v1",
            api_key="sk-moonshot-key",
            chat_model="moonshot-v1-8k",
        )
        adapter2 = get_llm_adapter(provider_obj)
        assert isinstance(adapter2, MoonshotLLMAdapter)
        assert adapter2.api_key == "sk-moonshot-key"

    def test_get_stt_adapter_factory(self):
        silicon = get_stt_adapter("siliconflow", api_key="sk-sf-key")
        assert isinstance(silicon, SiliconFlowSTTAdapter)

        qwen = get_stt_adapter("qwen", api_key="sk-qw-key")
        assert isinstance(qwen, QwenSTTAdapter)

        groq = get_stt_adapter("groq", api_key="gsk-groq")
        assert isinstance(groq, OpenAICompatibleSTTAdapter)
        assert groq.default_model == "whisper-large-v3"

        default_stt = get_stt_adapter("openai", api_key="sk-openai")
        assert isinstance(default_stt, OpenAICompatibleSTTAdapter)


class TestAdaptersExecutionWithMockServer:
    """Tests LLM and STT adapter execution, streaming, and error handling with MockLLMServer."""

    @pytest.mark.asyncio
    async def test_llm_chat_execution(self, mock_llm_server):
        adapter = DeepSeekLLMAdapter(api_key="sk-valid-key", client_override=mock_llm_server)
        messages = [ChatMessage(role="user", content="Hello")]
        resp = await adapter.chat(messages, model="deepseek-chat")
        assert isinstance(resp, LLMResponse)
        assert "chinese" in resp.content
        assert resp.usage is not None

    @pytest.mark.asyncio
    async def test_llm_stream_chat_tokens(self, mock_llm_server):
        adapter = QwenLLMAdapter(api_key="sk-valid-key", client_override=mock_llm_server)
        messages = [ChatMessage(role="user", content="Generate")]
        tokens = []
        async for chunk in adapter.stream_chat(messages, model="qwen-max"):
            tokens.append(chunk)
        full_text = "".join(tokens)
        assert len(full_text) > 0
        assert "chinese" in full_text

    @pytest.mark.asyncio
    async def test_llm_test_connection_success_and_failure(self, mock_llm_server):
        adapter = GLMLLMAdapter(api_key="sk-valid-key", client_override=mock_llm_server)
        res = await adapter.test_connection(model="glm-4-flash")
        assert res.success is True
        assert res.latency_ms is not None

        bad_adapter = GLMLLMAdapter(api_key="sk-invalid", client_override=mock_llm_server)
        res_bad = await bad_adapter.test_connection()
        assert res_bad.success is False

    @pytest.mark.asyncio
    async def test_llm_list_models_mock(self, mock_llm_server):
        adapter = SiliconFlowLLMAdapter(api_key="sk-valid-key", client_override=mock_llm_server)
        models = await adapter.list_models()
        assert "deepseek-chat" in models
        assert "gpt-4o" in models

    @pytest.mark.asyncio
    async def test_stt_transcription_and_test(self, mock_llm_server):
        adapter = SiliconFlowSTTAdapter(api_key="sk-valid-key", client_override=mock_llm_server)
        text = await adapter.transcribe(b"RIFF" + b"\x00" * 200, filename="audio.wav", language="ja")
        assert "おはよう" in text

        res = await adapter.test_connection()
        assert res.success is True


class TestConfigAndProviderRouterEndpoints:
    """Tests FastAPI config and provider management REST endpoints."""

    @pytest.mark.asyncio
    async def test_config_endpoints_get_and_post(self):
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # 1. GET /api/config
            resp = await client.get("/api/config")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "ok"
            assert "settings" in data

            # 2. POST /api/config
            post_resp = await client.post("/api/config", json={
                "settings": {
                    "temperature": 0.85,
                    "top_k": 20,
                    "max_history_messages": 15,
                }
            })
            assert post_resp.status_code == 200
            assert post_resp.json()["status"] == "success"

    @pytest.mark.asyncio
    async def test_provider_management_endpoints(self):
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # 1. GET /api/providers
            resp = await client.get("/api/providers")
            assert resp.status_code == 200
            data = resp.json()
            assert "providers" in data
            assert "presets" in data

            # 2. GET /api/providers/presets
            presets_resp = await client.get("/api/providers/presets")
            assert presets_resp.status_code == 200
            assert len(presets_resp.json()["presets"]) >= 8

            # 3. POST /api/providers (Create new provider)
            create_resp = await client.post("/api/providers", json={
                "id": "my_custom_provider",
                "name": "My Custom AI",
                "api_base_url": "http://127.0.0.1:11434/v1",
                "api_key": "sk-custom-secret-key-12345",
                "chat_model": "llama3:8b",
                "is_active": False,
            })
            assert create_resp.status_code == 200
            created_provider = create_resp.json()["provider"]
            assert created_provider["id"] == "my_custom_provider"
            assert "****" in created_provider["api_key"]

            # 4. GET /api/providers/my_custom_provider
            get_resp = await client.get("/api/providers/my_custom_provider")
            assert get_resp.status_code == 200
            assert get_resp.json()["provider"]["name"] == "My Custom AI"

            # 5. POST /api/providers/test
            test_resp = await client.post("/api/providers/test", json={
                "provider_type": "deepseek",
                "api_key": "sk-test-valid",
                "base_url": "https://api.deepseek.com/v1",
                "model": "deepseek-chat"
            })
            assert test_resp.status_code == 200
            assert "success" in test_resp.json()

            # 6. GET /api/providers/{provider_id}/models
            models_resp = await client.get("/api/providers/my_custom_provider/models")
            assert models_resp.status_code == 200
            assert "models" in models_resp.json()

            # 7. DELETE /api/providers/my_custom_provider
            del_resp = await client.delete("/api/providers/my_custom_provider")
            assert del_resp.status_code == 200
            assert del_resp.json()["status"] == "deleted"
