"""Backend contract tests for single-source provider presets and URL-based test model resolution."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
from httpx import ASGITransport, AsyncClient
import pytest

from galgame2voice.adapters import registry
from galgame2voice.adapters.llm import openai_adapter
from galgame2voice.adapters.llm.openai_adapter import OpenAICompatibleLLMAdapter
from galgame2voice.adapters.registry import PROVIDER_PRESETS, list_provider_presets
from galgame2voice.main import create_app
from scripts import package_release

CONTRACT_PATH = Path(__file__).resolve().parent / "contracts" / "provider_presets_v1.json"


def load_contract() -> dict[str, Any]:
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))


def test_provider_presets_contract_and_release_packaging():
    contract = load_contract()
    assert contract["schema_version"] == "provider_presets_v1"
    assert contract["presets"] == list_provider_presets()
    assert package_release.should_include(Path("tests/contracts/provider_presets_v1.json")) is True


def test_url_default_test_models_derive_from_provider_presets():
    non_custom_presets = [p for p in PROVIDER_PRESETS.values() if p["id"] != "custom"]
    assert non_custom_presets

    expected_by_id = {p["id"]: p["default_chat_model"] for p in non_custom_presets}
    actual_by_id = {
        p["id"]: OpenAICompatibleLLMAdapter(
            api_key="sk-test",
            base_url=p["default_base_url"],
        )._resolve_test_model(None)
        for p in non_custom_presets
    }
    assert actual_by_id == expected_by_id

    custom_domain_paths = (
        ("https://generativelanguage.googleapis.com/v1beta/openai", PROVIDER_PRESETS["gemini"]["default_chat_model"]),
        ("https://dashscope-intl.aliyuncs.com/compatible-mode/v1", PROVIDER_PRESETS["qwen"]["default_chat_model"]),
        ("https://api.moonshot.cn/v1", PROVIDER_PRESETS["moonshot"]["default_chat_model"]),
        ("api.moonshot.cn/v1", PROVIDER_PRESETS["moonshot"]["default_chat_model"]),
    )
    for url, expected_model in custom_domain_paths:
        adapter = OpenAICompatibleLLMAdapter(api_key="sk-test", base_url=url)
        assert adapter._resolve_test_model(None) == expected_model

    assert not hasattr(openai_adapter, "_URL_DEFAULT_MODELS")

    # Explicit model and non-generic default_model keep priority; generic fallback yields to URL preset.
    explicit_adapter = OpenAICompatibleLLMAdapter(
        api_key="sk-test",
        base_url="https://api.moonshot.cn/v1",
        default_model="moonshot-v1-8k",
    )
    assert explicit_adapter._resolve_test_model("moonshot-v1-128k") == "moonshot-v1-128k"
    assert explicit_adapter._resolve_test_model(None) == "moonshot-v1-8k"

    generic_default_adapter = OpenAICompatibleLLMAdapter(
        api_key="sk-test",
        base_url="https://api.moonshot.cn/v1",
        default_model="gpt-6-luna",
    )
    assert generic_default_adapter._resolve_test_model(None) == PROVIDER_PRESETS["moonshot"]["default_chat_model"]

    # Unrelated hosts, lookalike domains, path substrings, and multi-tenant suffixes return None / fallback.
    resolve_by_url = getattr(registry, "resolve_preset_default_model_by_url", None)
    assert callable(resolve_by_url)
    for non_preset_url in (
        None,
        "",
        "https://",
        "http://127.0.0.1:11434/v1",
        "https://evil-openai.com/v1",
        "https://proxy.example.org/openai.com/v1",
        "https://my-gateway.workers.dev/v1",
    ):
        assert resolve_by_url(non_preset_url) is None

    unknown_adapter = OpenAICompatibleLLMAdapter(
        api_key="sk-test",
        base_url="https://proxy.example.org/openai.com/v1",
    )
    assert unknown_adapter._resolve_test_model(None) == "gpt-6-luna"


@pytest.mark.asyncio
async def test_provider_test_endpoint_probes_preset_model_for_unregistered_custom_id():
    """POST /api/providers/test with an unregistered id and Moonshot URL probes moonshot's default model."""
    app = create_app()
    transport = ASGITransport(app=app)
    probed_models: list[str] = []

    orig_get = httpx.AsyncClient.get
    orig_post = httpx.AsyncClient.post

    async def fake_get(self_client, url, *args, **kwargs):
        if "api.moonshot.cn" in str(url):
            return httpx.Response(404, text="Not Found", request=httpx.Request("GET", str(url)))
        return await orig_get(self_client, url, *args, **kwargs)

    async def fake_post(self_client, url, *args, **kwargs):
        if "api.moonshot.cn" in str(url):
            payload = kwargs.get("json") or {}
            probed_models.append(payload.get("model", ""))
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "pong"}}]},
                request=httpx.Request("POST", str(url)),
            )
        return await orig_post(self_client, url, *args, **kwargs)

    async def allow_safe_url(*_args, **_kwargs):
        return None

    with (
        patch("galgame2voice.security.url_guard.validate_llm_base_url", return_value=(True, "")),
        patch("galgame2voice.security.url_guard.assert_llm_url_safe", side_effect=allow_safe_url),
        patch.object(httpx.AsyncClient, "get", fake_get),
        patch.object(httpx.AsyncClient, "post", fake_post),
    ):
        async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as client:
            resp = await client.post(
                "/api/providers/test",
                json={
                    "id": "moonshot_gateway",
                    "api_key": "sk-moonshot-test",
                    "api_base_url": "https://api.moonshot.cn/v1",
                    "chat_model": "",
                },
            )
            assert resp.status_code == 200
            assert resp.json()["success"] is True

    assert probed_models == [PROVIDER_PRESETS["moonshot"]["default_chat_model"]]
