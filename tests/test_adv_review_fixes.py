"""
Tests for ADV review fixes:
A2: Acting menu sanitization in the Telegram bot, and outfit validation (the web paths are covered in
    test_p2b_lines_streaming.py).
A1: Telegram bot error handling pruning orphaned user message on LLM failure or empty reply.
"""

import pytest
from typing import Any, List, Dict, Optional

from galgame2voice.database.session import get_db
from galgame2voice.database import crud
from galgame2voice.routers.chat import _validate_chat_request, ChatRequest
from galgame2voice.telegram_bot import TelegramBotHandlers
from galgame2voice.telegram_bot.console_menus import resolve_session_key
from telegram_test_support import TELEGRAM_TEST_ADMINS


# ============================================================================
# Dummy Mocks for Telegram and ChatService
# ============================================================================

class DummyBotClient:
    def __init__(self):
        self.sent_messages: List[Dict[str, Any]] = []
        self.sent_actions: List[Dict[str, Any]] = []

    async def send_chat_action(self, chat_id: int, action: str, **kwargs: Any) -> bool:
        self.sent_actions.append({"chat_id": chat_id, "action": action})
        return True

    async def send_message(self, chat_id: int, text: str, reply_markup: Optional[Any] = None, **kwargs: Any) -> Dict[str, Any]:
        msg = {"chat_id": chat_id, "text": text, "reply_markup": reply_markup}
        self.sent_messages.append(msg)
        return msg


# ============================================================================
# A2 Tests: Acting Menu Sanitization & Outfit Validation
# ============================================================================

def test_chat_request_outfit_security_validation():
    """Validates that ChatRequest outfit parameter rejects path traversals, special chars, and excessive lengths."""
    # 1. Path traversal attacks
    req_traversal1 = ChatRequest(prompt="hello", outfit="../secret/costume")
    _validate_chat_request(req_traversal1)
    assert req_traversal1.outfit is None

    req_traversal2 = ChatRequest(prompt="hello", outfit="..\\secret")
    _validate_chat_request(req_traversal2)
    assert req_traversal2.outfit is None

    # 2. Slash and backslash
    req_slash = ChatRequest(prompt="hello", outfit="costume/sub")
    _validate_chat_request(req_slash)
    assert req_slash.outfit is None

    # 3. Excessive length (> 64 chars)
    req_long = ChatRequest(prompt="hello", outfit="a" * 65)
    _validate_chat_request(req_long)
    assert req_long.outfit is None

    # 4. Valid outfit identifier
    req_valid = ChatRequest(prompt="hello", outfit="summer_dress_2")
    _validate_chat_request(req_valid)
    assert req_valid.outfit == "summer_dress_2"

    # 5. Empty or whitespace outfit
    req_empty = ChatRequest(prompt="hello", outfit="   ")
    _validate_chat_request(req_empty)
    assert req_empty.outfit is None


@pytest.mark.asyncio
async def test_telegram_bot_sanitizes_lines_with_acting_menu(temp_db_path):
    """Validates that Telegram bot process_text_chat cleans invalid line act fields against acting_menu."""
    handlers = TelegramBotHandlers(admin_ids=TELEGRAM_TEST_ADMINS, db_path=temp_db_path)
    client = DummyBotClient()

    class MockLLMAdapter:
        async def chat(self, messages, **kwargs):
            # Model outputs line format containing invalid face and blush
            return type("LLMResponse", (), {
                "content": '{"lines": [{"zh": "你好！", "ja": "こんにちは", "emotion": "gentle", "act": {"face": "nonexistent_face", "blush": true, "motion": "nod"}}]}'
            })()

    class BuiltContext(list):
        menu = {
            "outfit": "default",
            "faces": ["happy"],
            "face_ids": {"happy": ["h1"]},
            "poses": [],
            "blush": False,
            "motions": ["nod"],
            "fx": [],
        }

    class MockChatService:
        def __init__(self):
            self.session_manager = type("SessionManager", (), {"clear_session": lambda s: None})()

        async def get_active_llm_adapter(self, conn=None):
            return MockLLMAdapter(), "gpt-4o", "mock_prov"

        async def prepare_messages(self, conn, session_id, text, **kwargs):
            ctx = BuiltContext([{"role": "user", "content": text}])
            return ctx

    handlers.chat_service = MockChatService()

    task = await handlers.process_text_chat(chat_id=9001, text="你好呀！", bot=client, user_id=9001)
    if task:
        await task

    # Verify message was saved to SQLite with sanitized act
    async with get_db(temp_db_path) as conn:
        msgs = await crud.get_recent_messages(conn, resolve_session_key(9001, 9001))
        # Should have user message and assistant message
        assistant_msgs = [m for m in msgs if m.role == "assistant"]
        assert len(assistant_msgs) == 1
        a_msg = assistant_msgs[0]
        assert a_msg.lines is not None
        lines = a_msg.lines
        assert len(lines) >= 1
        # nonexistent_face and blush: true should be stripped; motion: nod preserved
        assert lines[0]["act"] == {"motion": "nod"}


# ============================================================================
# A1 Tests: Telegram Bot Orphaned User Message Pruning
# ============================================================================

@pytest.mark.asyncio
async def test_telegram_bot_a1_prunes_user_message_on_llm_error(temp_db_path):
    """Validates that if LLM raises an error, Telegram bot prunes the orphaned user message from DB."""
    handlers = TelegramBotHandlers(admin_ids=TELEGRAM_TEST_ADMINS, db_path=temp_db_path)
    client = DummyBotClient()

    class FailingLLMAdapter:
        async def chat(self, messages, **kwargs):
            raise RuntimeError("Simulated upstream LLM provider crash (503 Service Unavailable)")

    class MockChatService:
        def __init__(self):
            self.session_manager = type("SessionManager", (), {"clear_session": lambda s: None})()

        async def get_active_llm_adapter(self, conn=None):
            return FailingLLMAdapter(), "gpt-4o", "mock_prov"

        async def prepare_messages(self, conn, session_id, text, **kwargs):
            return [{"role": "user", "content": text}]

    handlers.chat_service = MockChatService()

    task = await handlers.process_text_chat(chat_id=9002, text="这句会触发LLM异常", bot=client, user_id=9002)
    if task:
        await task

    # Assert error notification was sent to user
    assert len(client.sent_messages) >= 1
    assert "大模型生成回复失败" in client.sent_messages[-1]["text"]

    # Verify SQLite DB has ZERO messages for this session (orphaned user message was cleaned up)
    async with get_db(temp_db_path) as conn:
        rows = await (await conn.execute("SELECT session_id, role FROM messages")).fetchall()
    assert rows == []


@pytest.mark.asyncio
async def test_telegram_bot_a1_prunes_user_message_on_empty_reply(temp_db_path):
    """Validates that if LLM returns empty content, Telegram bot prunes the orphaned user message from DB."""
    handlers = TelegramBotHandlers(admin_ids=TELEGRAM_TEST_ADMINS, db_path=temp_db_path)
    client = DummyBotClient()

    class EmptyLLMAdapter:
        async def chat(self, messages, **kwargs):
            # Empty response
            return type("LLMResponse", (), {"content": ""})()

    class MockChatService:
        def __init__(self):
            self.session_manager = type("SessionManager", (), {"clear_session": lambda s: None})()

        async def get_active_llm_adapter(self, conn=None):
            return EmptyLLMAdapter(), "gpt-4o", "mock_prov"

        async def prepare_messages(self, conn, session_id, text, **kwargs):
            return [{"role": "user", "content": text}]

    handlers.chat_service = MockChatService()

    task = await handlers.process_text_chat(chat_id=9003, text="这句会得到空回复", bot=client, user_id=9003)
    if task:
        await task

    # Assert error notification was sent
    assert len(client.sent_messages) >= 1
    assert "大模型生成回复失败" in client.sent_messages[-1]["text"]

    # Verify SQLite DB has ZERO messages for this session
    async with get_db(temp_db_path) as conn:
        rows = await (await conn.execute("SELECT session_id, role FROM messages")).fetchall()
    assert rows == []
