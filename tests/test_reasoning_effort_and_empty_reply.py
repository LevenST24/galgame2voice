"""Tests for reasoning_effort configuration and empty reply error handling.

Covers:
- Database schema migration adding reasoning_effort idempotently without version bump.
- Provider model validation and CRUD (omitting keeps value, null clears value, invalid values 422).
- Provider API endpoints and preset defaults (Gemini defaulting to low).
- Adapter request payloads forwarding reasoning_effort and omitting null.
- Stream coordinator yielding error event with kind empty_reply and pruning DB on empty reply.
- Length truncation explanation vs default explanation.
- Client cancellation before content remaining cancelled rather than empty_reply.
- Synchronous chat and Telegram bot explaining instead of returning empty text.
- Diagnostic mapping for HTTP 400 parameter errors without masking as API key failure.
"""

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
import pytest

from galgame2voice.adapters.llm.gemini_adapter import GeminiLLMAdapter
from galgame2voice.adapters.llm.anthropic_adapter import AnthropicAdapter
from galgame2voice.adapters.llm.openai_adapter import OpenAICompatibleLLMAdapter
from galgame2voice.adapters.registry import get_llm_adapter, get_provider_preset, list_provider_presets
from galgame2voice.database import crud
from galgame2voice.database.migrations import CURRENT_SCHEMA_VERSION, _add_column_if_missing, run_schema_migrations
from galgame2voice.database.models import MessageCreate, ProviderCreate, ProviderUpdate
from galgame2voice.database.session import get_db, init_db
from galgame2voice.main import create_app
from galgame2voice.services.chat_pipelines.llm_pipeline import LlmStreamPipeline
from galgame2voice.services.chat_pipelines.stream_coordinator import StreamCoordinator
from galgame2voice.services.chat_service import ChatService
from galgame2voice.telegram_bot.handlers import TelegramBotHandlers
from galgame2voice.utils.error_diagnostics import (
    EMPTY_REPLY_DEFAULT,
    EMPTY_REPLY_STOPPED_ON_LENGTH,
    EmptyReplyError,
    diagnose_llm_error,
    chat_error_payload,
)
from tests.test_stream_reply_lines import coordinator_factory as coordinator_factory


@pytest.mark.asyncio
async def test_migration_adds_reasoning_effort_column_idempotently(tmp_path):
    """Verifies reasoning_effort column is added idempotently without bumping user_version."""
    db_file = tmp_path / "test_migration.db"
    async with aiosqlite.connect(db_file) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("CREATE TABLE providers (id TEXT PRIMARY KEY, name TEXT);")
        await conn.commit()

        # Step 1: Add column if missing
        await _add_column_if_missing(conn, "providers", "reasoning_effort", "TEXT DEFAULT NULL")
        await conn.commit()

        cursor = await conn.execute("PRAGMA table_info(providers);")
        columns = [row[1] for row in await cursor.fetchall()]
        assert "reasoning_effort" in columns

        # Step 2: Idempotency - second run must not fail
        await _add_column_if_missing(conn, "providers", "reasoning_effort", "TEXT DEFAULT NULL")
        await conn.commit()

        # Step 3: Default value is NULL on newly inserted row
        await conn.execute("INSERT INTO providers (id, name) VALUES ('p1', 'Provider 1');")
        await conn.commit()
        cursor = await conn.execute("SELECT reasoning_effort FROM providers WHERE id = 'p1';")
        row = await cursor.fetchone()
        assert row[0] is None

    # Step 4: Full schema migrations on fresh database also adds the column
    fresh_db = tmp_path / "fresh_migration.db"
    async with aiosqlite.connect(fresh_db) as conn:
        conn.row_factory = aiosqlite.Row
        await run_schema_migrations(conn)
        cursor = await conn.execute("PRAGMA table_info(providers);")
        columns = [row[1] for row in await cursor.fetchall()]
        assert "reasoning_effort" in columns

        # Idempotent re-run
        await run_schema_migrations(conn)
        assert CURRENT_SCHEMA_VERSION == 7
        assert (await (await conn.execute("PRAGMA user_version")).fetchone())[0] == 7
        # Upgrade an installed v7 database without changing its version or rows.
        await conn.execute("ALTER TABLE providers DROP COLUMN reasoning_effort")
        await conn.execute("INSERT INTO providers (id, name, api_base_url, chat_model) VALUES ('legacy', 'Existing', 'https://example.com', 'test')")
        await run_schema_migrations(conn)
        await run_schema_migrations(conn)
        row = await (await conn.execute("SELECT reasoning_effort FROM providers WHERE id='legacy'")).fetchone()
        assert row[0] is None
        assert (await (await conn.execute("PRAGMA user_version")).fetchone())[0] == 7


def test_provider_model_validation():
    """Verifies reasoning_effort accepts null, none, low, medium, high and rejects invalid values."""
    for valid_val in (None, "none", "low", "medium", "high"):
        prov = ProviderCreate(
            id=f"p_{valid_val}",
            name=f"Provider {valid_val}",
            api_base_url="https://api.test.com/v1",
            chat_model="gpt-4o",
            reasoning_effort=valid_val,
        )
        assert prov.reasoning_effort == valid_val

    for invalid_val in ("minimal", "extreme", "LOW", "max", 123):
        with pytest.raises(ValidationError):
            ProviderCreate(
                id="p_bad",
                name="Bad",
                api_base_url="https://api.test.com/v1",
                chat_model="gpt-4o",
                reasoning_effort=invalid_val,
            )

    update_valid = ProviderUpdate(reasoning_effort="high")
    assert update_valid.reasoning_effort == "high"

    with pytest.raises(ValidationError):
        ProviderUpdate(reasoning_effort="unsupported")


@pytest.mark.asyncio
async def test_provider_crud_operations(tmp_path):
    """Verifies CRUD preserves reasoning_effort on omit and clears on explicit None."""
    db_file = tmp_path / "crud.db"
    await init_db(db_file)

    async with get_db(str(db_file)) as conn:
        created = await crud.create_provider(
            conn,
            ProviderCreate(
                id="prov_test",
                name="Prov Test",
                provider_type="openai",
                api_base_url="https://api.test.com/v1",
                api_key="sk-secret123",
                chat_model="gpt-4o",
                reasoning_effort="low",
            ),
        )
        assert created.reasoning_effort == "low"

        fetched = await crud.get_provider(conn, "prov_test")
        assert fetched is not None
        assert fetched.reasoning_effort == "low"

        # Update without reasoning_effort keeps stored value
        updated_omit = await crud.update_provider(
            conn,
            "prov_test",
            ProviderUpdate(name="Prov Renamed"),
        )
        assert updated_omit is not None
        assert updated_omit.name == "Prov Renamed"
        assert updated_omit.reasoning_effort == "low"

        # Update with explicit None clears stored value to NULL
        updated_null = await crud.update_provider(
            conn,
            "prov_test",
            ProviderUpdate(reasoning_effort=None),
        )
        assert updated_null is not None
        assert updated_null.reasoning_effort is None

        # Fetch raw to verify NULL in SQLite
        raw = await crud.get_provider_raw(conn, "prov_test")
        assert raw is not None
        assert raw.reasoning_effort is None

        # Update to high
        updated_high = await crud.update_provider(
            conn,
            "prov_test",
            ProviderUpdate(reasoning_effort="high"),
        )
        assert updated_high is not None
        assert updated_high.reasoning_effort == "high"


@pytest.mark.asyncio
async def test_provider_api_endpoints():
    """Verifies provider API round trips reasoning_effort and enforces 422 on invalid values."""
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Create with reasoning_effort="medium"
        create_resp = await client.post(
            "/api/providers",
            json={
                "id": "api_test_p",
                "name": "API Test",
                "api_base_url": "https://api.test.com/v1",
                "api_key": "sk-12345678",
                "chat_model": "test-model",
                "reasoning_effort": "medium",
            },
        )
        assert create_resp.status_code == 200
        assert create_resp.json()["provider"]["reasoning_effort"] == "medium"

        # Get single provider
        get_resp = await client.get("/api/providers/api_test_p")
        assert get_resp.status_code == 200
        assert get_resp.json()["provider"]["reasoning_effort"] == "medium"

        # List providers includes it
        list_resp = await client.get("/api/providers")
        assert list_resp.status_code == 200
        provs = list_resp.json()["providers"]
        p = next((x for x in provs if x["id"] == "api_test_p"), None)
        assert p is not None
        assert p["reasoning_effort"] == "medium"

        # Update omitting reasoning_effort keeps medium
        update_omit = await client.post(
            "/api/providers",
            json={
                "id": "api_test_p",
                "name": "API Test Renamed",
                "api_base_url": "https://api.test.com/v1",
                "api_key": "sk-****5678",
                "chat_model": "test-model",
            },
        )
        assert update_omit.status_code == 200
        assert update_omit.json()["provider"]["reasoning_effort"] == "medium"

        # Update with explicit null clears to null
        update_null = await client.post(
            "/api/providers",
            json={
                "id": "api_test_p",
                "name": "API Test Renamed",
                "api_base_url": "https://api.test.com/v1",
                "api_key": "sk-****5678",
                "chat_model": "test-model",
                "reasoning_effort": None,
            },
        )
        assert update_null.status_code == 200
        assert update_null.json()["provider"]["reasoning_effort"] is None

        # Invalid value returns 422
        bad_resp = await client.post(
            "/api/providers",
            json={
                "id": "bad_p",
                "name": "Bad Provider",
                "api_base_url": "https://api.test.com/v1",
                "api_key": "sk-12345678",
                "chat_model": "test-model",
                "reasoning_effort": "minimal",
            },
        )
        assert bad_resp.status_code == 422
        bad_update = await client.put("/api/providers/api_test_p", json={"reasoning_effort": "minimal"})
        assert bad_update.status_code == 422
        assert (await client.get("/api/providers/api_test_p")).json()["provider"]["reasoning_effort"] is None

        # Explicit null on a new Gemini must override the preset's low default.
        gemini = await client.post("/api/providers", json={"id": "gemini", "reasoning_effort": None})
        assert gemini.status_code == 200
        assert gemini.json()["provider"]["reasoning_effort"] is None


def test_presets_gemini_defaults_to_low():
    """Verifies built-in Gemini preset includes default_reasoning_effort: 'low'."""
    gemini_preset = get_provider_preset("gemini")
    assert gemini_preset is not None
    assert gemini_preset.get("default_reasoning_effort") == "low"

    presets = list_provider_presets()
    item = next((p for p in presets if p["id"] == "gemini"), None)
    assert item is not None
    assert item.get("default_reasoning_effort") == "low"


def test_adapter_payload_forwards_reasoning_effort():
    """Verifies OpenAICompatibleLLMAdapter and GeminiLLMAdapter forward reasoning_effort when set, omitting when null."""
    openai_adapter = OpenAICompatibleLLMAdapter(
        api_key="synthetic",
        base_url="https://api.test.com/v1",
        model="gpt-4o",
    )
    # Provided string (kwargs or extra dict)
    payload_low = openai_adapter._build_payload(
        [{"role": "user", "content": "hi"}],
        model="gpt-4o", extra={"reasoning_effort": "low"},
    )
    assert payload_low["reasoning_effort"] == "low"

    payload_extra = openai_adapter._build_payload(
        [{"role": "user", "content": "hi"}],
        model="gpt-4o",
        extra={"reasoning_effort": "low"},
    )
    assert payload_extra["reasoning_effort"] == "low"

    # None / omitted
    payload_none = openai_adapter._build_payload(
        [{"role": "user", "content": "hi"}],
        model="test", extra={"reasoning_effort": None},
    )
    assert "reasoning_effort" not in payload_none

    gemini_adapter = GeminiLLMAdapter(
        api_key="synthetic",
        model="gemini-2.5-flash",
    )
    payload_gemini = gemini_adapter._build_payload(
        [{"role": "user", "content": "hi"}],
        model="test", extra={"reasoning_effort": "high"},
    )
    assert payload_gemini["reasoning_effort"] == "high"

    payload_gemini_null = gemini_adapter._build_payload(
        [{"role": "user", "content": "hi"}],
        model="test", extra={"reasoning_effort": None},
    )
    assert "reasoning_effort" not in payload_gemini_null


@pytest.mark.asyncio
async def test_llm_pipeline_and_coordinator_forward_reasoning_effort():
    """Verifies LlmStreamPipeline and StreamCoordinator pass reasoning_effort to adapter."""
    captured_kwargs = {}

    async def mock_stream_chat(*args, **kwargs):
        captured_kwargs.update(kwargs)
        yield "token"

    adapter = SimpleNamespace(stream_chat=mock_stream_chat, api_key="synthetic", reasoning_effort="low")
    pipe = LlmStreamPipeline(
        adapter=adapter,
        messages=[],
        model_name="test",
        reasoning_effort="low",
    )
    tokens = [t async for t in pipe.stream_tokens()]
    assert tokens == ["token"]
    assert captured_kwargs.get("reasoning_effort") == "low"

    # Coordinator resolves reasoning_effort from adapter when not passed
    coord = StreamCoordinator(
        tts_service=None, db_path="", metrics_collector=None, affection_service=None,
        adapter=adapter,
        messages=[],
        model_name="test",
        actual_provider_id="custom",
        session_id="sess_re",
        prompt="hello",
    )
    assert coord.reasoning_effort == "low"


@pytest.mark.asyncio
async def test_chat_sync_forwards_reasoning_effort(tmp_path):
    """Verifies ChatService.chat_sync forwards reasoning_effort when set and omits when None."""
    db_file = tmp_path / "sync_kwargs.db"
    await init_db(db_file)

    service = ChatService(db_path=str(db_file))
    captured_kwargs = {}

    async def mock_chat(messages, **kwargs):
        captured_kwargs.update(kwargs)
        return SimpleNamespace(content='{"chinese": "你好", "japanese": "こんにちは"}')

    adapter = SimpleNamespace(
        chat=AsyncMock(side_effect=mock_chat),
        stream_chat=AsyncMock(),
        reasoning_effort="high",
    )
    service._get_active_llm_adapter = AsyncMock(return_value=(adapter, "test_model", "custom"))
    service._synthesize_sync_audio = AsyncMock(return_value=("/audio/test.wav", 10.0, 0, 1))

    # Resolves from adapter when not explicitly passed
    await service.chat_sync("hi", session_id="sync_kw_1")
    assert captured_kwargs.get("reasoning_effort") == "high"

    # Explicitly passed overrides
    captured_kwargs.clear()
    await service.chat_sync("hi", session_id="sync_kw_2", reasoning_effort="none")
    assert captured_kwargs.get("reasoning_effort") == "none"

    # When adapter and call have None, omitted from chat kwargs
    adapter.reasoning_effort = None
    captured_kwargs.clear()
    await service.chat_sync("hi", session_id="sync_kw_3")
    assert "reasoning_effort" not in captured_kwargs

    await service.aclose()


@pytest.mark.asyncio
async def test_streaming_empty_reply_default(tmp_path):
    """Verifies that an empty reply emits an error event with kind empty_reply and prunes user message."""
    db_file = tmp_path / "stream_empty_default.db"
    await init_db(db_file)

    async with get_db(str(db_file)) as conn:
        user_msg = await crud.add_message(
            conn,
            MessageCreate(session_id="empty_default", role="user", content_chinese="你好"),
        )

    async def empty_tokens(*args, **kwargs):
        if False:
            yield ""

    coordinator = StreamCoordinator(
        adapter=SimpleNamespace(stream_chat=empty_tokens, api_key="synthetic"),
        messages=[],
        model_name="test",
        actual_provider_id="custom",
        session_id="empty_default",
        prompt="你好",
        user_msg=user_msg,
        db_path=str(db_file),
        metrics_collector=SimpleNamespace(record_chat_turn=AsyncMock(return_value={})),
        affection_service=SimpleNamespace(handle_turn_affection=AsyncMock(return_value={"emotion": "gentle"})),
        tts_service=None,
        concat_wav_fn=lambda *args: True,
    )

    events = [e async for e in coordinator]
    assert len(events) >= 1
    last_event = events[-1]
    assert last_event["event"] == "error"
    data = last_event["data"]
    assert data["kind"] == "empty_reply"
    assert data["error_code"] == "empty_reply"
    assert data["error"] == EMPTY_REPLY_DEFAULT

    # Verify assistant message was not added and orphaned user message was pruned
    async with get_db(str(db_file)) as conn:
        remaining = await crud.get_recent_messages(conn, "empty_default")
        assert len(remaining) == 0


@pytest.mark.asyncio
async def test_streaming_empty_reply_stopped_on_length(tmp_path):
    """Verifies that empty reply stopped on length uses the thinking-quota explanation."""
    db_file = tmp_path / "stream_empty_length.db"
    await init_db(db_file)

    async with get_db(str(db_file)) as conn:
        user_msg = await crud.add_message(
            conn,
            MessageCreate(session_id="empty_length", role="user", content_chinese="长问题"),
        )

    async def empty_tokens(*args, **kwargs):
        if False:
            yield ""

    adapter = SimpleNamespace(
        stream_chat=empty_tokens,
        api_key="synthetic",
        last_finish_reason="length",
    )

    coordinator = StreamCoordinator(
        adapter=adapter,
        messages=[],
        model_name="test",
        actual_provider_id="custom",
        session_id="empty_length",
        prompt="长问题",
        user_msg=user_msg,
        db_path=str(db_file),
        metrics_collector=SimpleNamespace(record_chat_turn=AsyncMock(return_value={})),
        affection_service=SimpleNamespace(handle_turn_affection=AsyncMock(return_value={"emotion": "gentle"})),
        tts_service=None,
        concat_wav_fn=lambda *args: True,
    )

    events = [e async for e in coordinator]
    assert len(events) >= 1
    last_event = events[-1]
    assert last_event["event"] == "error"
    data = last_event["data"]
    assert data["kind"] == "empty_reply"
    assert data["error"] == EMPTY_REPLY_STOPPED_ON_LENGTH

    async with get_db(str(db_file)) as conn:
        remaining = await crud.get_recent_messages(conn, "empty_length")
        assert len(remaining) == 0


@pytest.mark.asyncio
async def test_streaming_cancellation_stays_cancelled(tmp_path):
    """Verifies that user cancellation before content emits done with truncated: True, not empty_reply."""
    db_file = tmp_path / "stream_cancel.db"
    await init_db(db_file)

    async with get_db(str(db_file)) as conn:
        user_msg = await crud.add_message(
            conn,
            MessageCreate(session_id="cancel_sess", role="user", content_chinese="取消测试"),
        )

    cancel_event = asyncio.Event()

    async def cancel_tokens(*args, **kwargs):
        cancel_event.set()
        if False:
            yield ""

    coordinator = StreamCoordinator(
        adapter=SimpleNamespace(stream_chat=cancel_tokens, api_key="synthetic"),
        messages=[],
        model_name="test",
        actual_provider_id="custom",
        session_id="cancel_sess",
        prompt="取消测试",
        user_msg=user_msg,
        db_path=str(db_file),
        cancel_event=cancel_event,
        metrics_collector=SimpleNamespace(record_chat_turn=AsyncMock(return_value={})),
        affection_service=SimpleNamespace(handle_turn_affection=AsyncMock(return_value={"emotion": "gentle"})),
        tts_service=None,
        concat_wav_fn=lambda *args: True,
    )

    events = [e async for e in coordinator]
    assert len(events) >= 1
    last_event = events[-1]
    assert last_event["event"] == "done"
    assert last_event["data"]["truncated"] is True
    # Ensure no error event was yielded
    assert not any(e["event"] == "error" for e in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("finish_reason", [None, "length"])
async def test_chat_sync_empty_reply_raises_and_prunes(tmp_path, finish_reason):
    """Verifies ChatService.chat_sync raises EmptyReplyError and prunes the user message."""
    db_file = tmp_path / "sync_empty.db"
    await init_db(db_file)

    service = ChatService(db_path=str(db_file))
    mock_adapter_tuple = (
        SimpleNamespace(
            chat=AsyncMock(return_value=SimpleNamespace(content="")),
            stream_chat=AsyncMock(),
            last_finish_reason=finish_reason,
        ),
        "test_model",
        "custom",
    )
    service.get_active_llm_adapter = AsyncMock(return_value=mock_adapter_tuple)
    service._get_active_llm_adapter = service.get_active_llm_adapter

    with pytest.raises(EmptyReplyError) as exc_info:
        await service.chat_sync("问题", session_id="sync_sess")

    assert str(exc_info.value) == (EMPTY_REPLY_STOPPED_ON_LENGTH if finish_reason else EMPTY_REPLY_DEFAULT)

    # Verify user message was pruned
    async with get_db(str(db_file)) as conn:
        msgs = await crud.get_recent_messages(conn, "sync_sess")
        assert len(msgs) == 0

    await service.aclose()


@pytest.mark.asyncio
async def test_api_chat_sync_endpoint_empty_reply_returns_502():
    """Verifies POST /api/chat returns HTTP 502 with kind empty_reply on empty reply."""
    app = create_app()
    service = AsyncMock()
    service.chat_sync.side_effect = EmptyReplyError(EMPTY_REPLY_DEFAULT)

    from galgame2voice.routers.chat import set_chat_service
    set_chat_service(service)
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/api/chat", json={"prompt": "hi", "session_id": "test_sync_empty"})
            assert resp.status_code == 502
            data = resp.json()
            detail = data.get("detail", {})
            assert detail.get("kind") == "empty_reply"
            assert detail.get("error_code") == "empty_reply"
            assert EMPTY_REPLY_DEFAULT in detail.get("error", "")
    finally:
        set_chat_service(None)


@pytest.mark.asyncio
@pytest.mark.parametrize("finish_reason", [None, "length"])
@pytest.mark.parametrize("effort", [None, "low"])
async def test_telegram_empty_reply_explains(tmp_path, monkeypatch, finish_reason, effort):
    """Verifies Telegram bot handlers explain with user guidance instead of sending empty text."""
    db_file = tmp_path / "tg_empty.db"
    await init_db(db_file)

    service = ChatService(db_path=str(db_file))
    mock_adapter_tuple = (
        SimpleNamespace(
            chat=AsyncMock(return_value=SimpleNamespace(content="")),
            stream_chat=AsyncMock(),
            last_finish_reason=finish_reason,
            reasoning_effort=effort,
        ),
        "test_model",
        "custom",
    )
    service.get_active_llm_adapter = AsyncMock(return_value=mock_adapter_tuple)
    service._get_active_llm_adapter = service.get_active_llm_adapter
    service.memory_service.process_user_message = AsyncMock()
    service.prepare_messages = AsyncMock(return_value=[])
    service.affection_service.handle_turn_affection = AsyncMock()

    handlers = TelegramBotHandlers(chat_service=service, db_path=str(db_file))
    bot = SimpleNamespace(send_message=AsyncMock(), send_chat_action=AsyncMock())
    monkeypatch.setattr(handlers, "_spawn_background_voice_task", lambda *args, **kwargs: None)

    task = await handlers.process_text_chat(777, "测试问题", bot)
    if task:
        await task

    assert bot.send_message.called
    sent_text = bot.send_message.call_args.kwargs["text"]
    assert (EMPTY_REPLY_STOPPED_ON_LENGTH if finish_reason else EMPTY_REPLY_DEFAULT) in sent_text
    kwargs = mock_adapter_tuple[0].chat.call_args.kwargs
    assert kwargs.get("reasoning_effort") == effort
    assert ("reasoning_effort" in kwargs) == (effort is not None)

    await service.aclose()


def test_diagnose_llm_error_unsupported_parameter():
    """Verifies HTTP 400 parameter errors diagnose as PARAMETER_NOT_SUPPORTED and don't mask as auth errors."""
    # Gemini thinking level error
    res_gemini = diagnose_llm_error(
        "Thinking level MINIMAL is not supported for this model",
        status_code=400,
        provider_id="gemini",
    )
    assert res_gemini.error_code == "PARAMETER_NOT_SUPPORTED"
    assert "思考强度" in res_gemini.guidance
    assert "API Key" not in res_gemini.message

    # OpenAI reasoning_effort error
    res_openai = diagnose_llm_error(
        "Invalid parameter: 'reasoning_effort'",
        status_code=400,
        provider_id="openai",
    )
    assert res_openai.error_code == "PARAMETER_NOT_SUPPORTED"
    assert "思考强度" in res_openai.guidance

    # Anthropic rejects unknown top-level fields
    res_anthropic = diagnose_llm_error(
        "reasoning_effort: Extra inputs are not permitted",
        status_code=400,
        provider_id="anthropic",
    )
    assert res_anthropic.error_code == "PARAMETER_NOT_SUPPORTED"

    # Genuine API key error on Gemini is still diagnosed as AUTH_FAILED
    res_key = diagnose_llm_error(
        "API key not valid. Please pass a valid API key.",
        status_code=400,
        provider_id="gemini",
    )
    assert res_key.error_code == "GEMINI_KEY_INVALID"


@pytest.mark.asyncio
@pytest.mark.parametrize("effort", [None, "none", "low", "medium", "high"])
async def test_all_request_builders_forward_only_configured_effort(tmp_path, effort):
    adapter = SimpleNamespace(chat=AsyncMock(return_value=SimpleNamespace(content="翻訳")), reasoning_effort=effort)
    captured = []

    async def tokens(*args, **kwargs):
        captured.append(kwargs)
        yield "content"

    adapter.stream_chat = tokens
    pipeline = LlmStreamPipeline(adapter=adapter, messages=[], model_name="test", reasoning_effort=effort)
    assert [token async for token in pipeline.stream_tokens()] == ["content"]
    database = tmp_path / "builders.db"
    await init_db(database)
    service = ChatService(db_path=str(database))
    service._get_active_llm_adapter = AsyncMock(return_value=(adapter, "test", "custom"))
    try:
        await service._execute_sync_llm(adapter=adapter, messages=[], model_name="test")
        assert await service.resolve_message_japanese("中文", session_id="translation") == "翻訳"
        captured.extend(call.kwargs for call in adapter.chat.call_args_list)
        for kwargs in captured:
            if effort is None:
                assert "reasoning_effort" not in kwargs
            else:
                assert kwargs["reasoning_effort"] == effort
    finally:
        await service.aclose()

    configured = get_llm_adapter(ProviderCreate(id="gemini", name="Gemini", reasoning_effort=effort,
                                               api_base_url="https://generativelanguage.googleapis.com/v1beta/openai", chat_model="test"))
    payload = configured._build_payload([], "test")
    assert payload.get("reasoning_effort") == effort
    assert ("reasoning_effort" in payload) == (effort is not None)


@pytest.mark.parametrize("effort", [None, "low"])
def test_native_adapter_forwards_only_configured_effort(effort):
    adapter = AnthropicAdapter(api_key="synthetic", reasoning_effort=effort)
    payload = adapter._prepare_anthropic_payload([])
    assert payload.get("reasoning_effort") == effort
    assert ("reasoning_effort" in payload) == (effort is not None)
    assert "reasoning_effort" not in adapter._prepare_anthropic_payload([], reasoning_effort=None)


@pytest.mark.asyncio
@pytest.mark.parametrize(("raw", "expected"), [
    ("", EMPTY_REPLY_DEFAULT),
    ('{"chinese":"  ","japanese":""}', EMPTY_REPLY_DEFAULT),
    ('{"lines":[]}', EMPTY_REPLY_DEFAULT),
    ('{"lines":[{"zh":"未完成', EMPTY_REPLY_STOPPED_ON_LENGTH),
])
async def test_empty_parsed_stream_uses_shared_coordinator_fake(coordinator_factory, raw, expected):
    async def tokens(*args, **kwargs):
        yield raw

    coordinator, _, database = coordinator_factory(tokens=tokens)
    events = [event async for event in coordinator]
    assert events[-1] == {"event": "error", "data": chat_error_payload(EmptyReplyError(expected))}
    assert not any(event["event"] == "done" for event in events)
    coordinator.metrics_collector.record_chat_turn.assert_not_awaited()
    coordinator.affection_service.handle_turn_affection.assert_not_awaited()
    async with get_db(database) as conn:
        assert await crud.get_recent_messages(conn, "lines") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("effort", [None, "low"])
async def test_real_adapter_stream_retains_length_finish_without_text(coordinator_factory, effort):
    frame = {"choices": [{"delta": {}, "finish_reason": "length"}]}
    response = SimpleNamespace(status_code=200, text=f"data: {json.dumps(frame)}\n\ndata: [DONE]\n\n")
    client = SimpleNamespace(post=AsyncMock(return_value=response))
    adapter = GeminiLLMAdapter(api_key="synthetic", reasoning_effort=effort)
    coordinator, _, database = coordinator_factory()
    coordinator.adapter = adapter
    # Exercise the real adapter's HTTP payload and SSE decoding with no live network.
    original = adapter.stream_chat

    async def stream(*args, **kwargs):
        async for token in original(*args, **kwargs, client_override=client):
            yield token

    adapter.stream_chat = stream
    events = [event async for event in coordinator]
    assert events[-1]["data"]["error"] == EMPTY_REPLY_STOPPED_ON_LENGTH
    assert events[-1]["data"]["kind"] == "empty_reply"
    payload = client.post.call_args.kwargs["json"]
    assert payload.get("reasoning_effort") == effort
    assert ("reasoning_effort" in payload) == (effort is not None)
    async with get_db(database) as conn:
        assert await crud.get_recent_messages(conn, "lines") == []


@pytest.mark.parametrize(("text", "status", "provider"), [
    ("API key not valid for reasoning model", 400, "gemini"),
    ("model thinking-model not found", 404, "openai"),
    ("reasoning_effort unsupported: quota exceeded", 429, "openai"),
    ("invalid parameter: temperature", 400, "custom"),
    ("unknown field: unrelated", 400, "custom"),
    ("upstream says 没有返回任何内容 while server failed", 500, "custom"),
])
def test_diagnostics_do_not_reclassify_unrelated_errors(text, status, provider):
    result = diagnose_llm_error(text, status_code=status, provider_id=provider)
    assert result.error_code not in ("PARAMETER_NOT_SUPPORTED", "empty_reply")


@pytest.mark.asyncio
@pytest.mark.parametrize("effort", [None, "low"])
async def test_nonstream_adapter_and_connectivity_probe_forward_effort(effort):
    data = {"choices": [{"message": {"content": None}, "finish_reason": "length"}]}
    response = SimpleNamespace(status_code=200, json=lambda: data, text="")
    server = SimpleNamespace(handle_chat_completion=AsyncMock(return_value=response))
    adapter = GeminiLLMAdapter(api_key="synthetic", client_override=server, reasoning_effort=effort)
    result = await adapter.chat([], model="test")
    assert result.content == ""
    assert adapter.last_finish_reason == "length"
    payload = server.handle_chat_completion.call_args.args[0]
    assert payload.get("reasoning_effort") == effort
    assert ("reasoning_effort" in payload) == (effort is not None)
    client = SimpleNamespace(post=AsyncMock(return_value=response))
    await adapter._probe_chat_fallback(client, "test", {}, "gemini", time.perf_counter())
    payload = client.post.call_args.kwargs["json"]
    assert payload.get("reasoning_effort") == effort
    assert ("reasoning_effort" in payload) == (effort is not None)
