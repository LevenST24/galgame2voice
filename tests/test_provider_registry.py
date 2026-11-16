"""Tests for decoupled LLM provider protocol, registry, and factory."""

# galgame2voice.providers cannot be the first galgame2voice import: it hits a circular import through
# galgame2voice.adapters. Importing the chat service first loads them in the app's order.
import galgame2voice.services.chat_service  # noqa: F401
from galgame2voice.providers import (
    AnthropicProvider,
    GeminiProvider,
    LLMProvider,
    OpenAIProvider,
    create_provider,
)


def test_provider_registry_and_factory():
    openai_p = create_provider("openai", api_key="sk-test")
    assert isinstance(openai_p, LLMProvider)
    assert isinstance(openai_p, OpenAIProvider)

    gemini_p = create_provider("gemini", api_key="sk-test")
    assert isinstance(gemini_p, LLMProvider)
    assert isinstance(gemini_p, GeminiProvider)

    anthropic_p = create_provider("anthropic", api_key="sk-test")
    assert isinstance(anthropic_p, LLMProvider)
    assert isinstance(anthropic_p, AnthropicProvider)
