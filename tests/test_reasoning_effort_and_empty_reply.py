"""Tests for reasoning_effort configuration and empty reply error handling.

Covers:
- Database schema migration adding reasoning_effort idempotently without version bump.
- Provider model validation and CRUD (omitting keeps value, null clears value, invalid values 422).
- Provider API endpoints and preset defaults (Gemini defaulting to low).
- Adapter request payloads forwarding reasoning_effort and omitting null.
- Stream coordinator yielding error event with kind empty_reply and pruning DB on empty reply.
- Length truncation explanation vs default explanation.
- Client cancellation before content remaining cancelled rather than empty_reply.
- Diagnostic mapping for HTTP 400 parameter errors without masking as API key failure.
"""

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
import pytest

from galgame2voice.adapters.base import configured_reasoning_effort
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
from galgame2voice.utils.error_diagnostics import (
    CONTENT_FILTER_FINISH_REASONS,
    EMPTY_REPLY_CONTENT_FILTERED,
    EMPTY_REPLY_DEFAULT,
    EMPTY_REPLY_STOPPED_ON_LENGTH,
    EmptyReplyError,
    STREAM_TRUNCATED_CONTENT_FILTERED,
    STREAM_TRUNCATED_ON_LENGTH,
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
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as client:
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
async def test_all_request_builders_forward_only_configured_effort(effort):
    adapter = SimpleNamespace(reasoning_effort=effort)
    captured = []

    async def tokens(*args, **kwargs):
        captured.append(kwargs)
        yield "content"

    adapter.stream_chat = tokens
    pipeline = LlmStreamPipeline(adapter=adapter, messages=[], model_name="test", reasoning_effort=effort)
    assert [token async for token in pipeline.stream_tokens()] == ["content"]
    for kwargs in captured:
        if effort is None:
            assert "reasoning_effort" not in kwargs
        else:
            assert kwargs["reasoning_effort"] == effort

    configured = get_llm_adapter(ProviderCreate(id="gemini", name="Gemini", reasoning_effort=effort,
                                               api_base_url="https://generativelanguage.googleapis.com/v1beta/openai", chat_model="test"))
    payload = configured._build_payload([], "test")
    assert payload.get("reasoning_effort") == effort
    assert ("reasoning_effort" in payload) == (effort is not None)


@pytest.mark.parametrize("effort", [None, "low"])
def test_native_adapter_forwards_only_configured_effort(effort):
    adapter = AnthropicAdapter(api_key="synthetic", reasoning_effort=effort)
    payload = adapter._prepare_anthropic_payload([])
    assert "reasoning_effort" not in payload
    assert payload.get("output_config") == ({"effort": effort} if effort else None)
    assert "output_config" not in adapter._prepare_anthropic_payload([], reasoning_effort=None)


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
async def test_connectivity_probe_forwards_effort(effort):
    response = SimpleNamespace(status_code=200, text="")
    adapter = GeminiLLMAdapter(api_key="synthetic", reasoning_effort=effort)
    client = SimpleNamespace(post=AsyncMock(return_value=response))
    await adapter._probe_chat_fallback(client, "test", {}, "gemini", time.perf_counter())
    payload = client.post.call_args.kwargs["json"]
    assert payload.get("reasoning_effort") == effort
    assert ("reasoning_effort" in payload) == (effort is not None)


def test_configured_reasoning_effort_forwards_only_known_levels():
    """Mocks and unknown values must never reach a request as reasoning_effort."""
    assert configured_reasoning_effort(MagicMock()) is None
    assert configured_reasoning_effort(SimpleNamespace(reasoning_effort="minimal")) is None
    assert configured_reasoning_effort(SimpleNamespace()) is None
    for level in ("none", "low", "medium", "high"):
        assert configured_reasoning_effort(SimpleNamespace(reasoning_effort=level)) == level


@pytest.mark.parametrize("finish_reason", list(CONTENT_FILTER_FINISH_REASONS))
def test_validate_stream_completion_content_filter(finish_reason):
    """Verifies validate_stream_completion raises StreamTruncatedError on all content filter finish reasons."""
    from galgame2voice.adapters.base import validate_stream_completion
    from galgame2voice.utils.error_diagnostics import StreamTruncatedError

    with pytest.raises(StreamTruncatedError) as exc_info:
        validate_stream_completion(
            yielded_any=True,
            saw_terminal=True,
            finish_reason=finish_reason,
        )
    assert exc_info.value.stopped_on_length is False
    assert STREAM_TRUNCATED_CONTENT_FILTERED in str(exc_info.value)


def test_validate_stream_completion_edge_cases():
    """Verifies validate_stream_completion ignores empty stream content filters and unknown finish reasons."""
    from galgame2voice.adapters.base import validate_stream_completion
    from galgame2voice.utils.error_diagnostics import StreamIncompleteError

    # Empty stream with content_filter does not raise StreamTruncatedError (empty reply handled downstream)
    for cf_reason in CONTENT_FILTER_FINISH_REASONS:
        validate_stream_completion(yielded_any=False, saw_terminal=True, finish_reason=cf_reason)
        validate_stream_completion(yielded_any=False, saw_terminal=False, finish_reason=cf_reason)

    # Unknown clean stop reasons pass without exception
    validate_stream_completion(yielded_any=True, saw_terminal=True, finish_reason="unknown_clean_stop")

    # Abrupt end without terminal signal or finish_reason raises StreamIncompleteError
    with pytest.raises(StreamIncompleteError):
        validate_stream_completion(yielded_any=True, saw_terminal=False, finish_reason=None)


@pytest.mark.asyncio
@pytest.mark.parametrize("finish_reason", ["content_filter", "SAFETY"])
async def test_streaming_content_filter_emits_error_and_truncated_done(tmp_path, finish_reason):
    """Verifies stream_chat emits error event with content filter message and done with truncated: True."""
    from galgame2voice.adapters.base import validate_stream_completion

    db_file = tmp_path / f"stream_cf_{finish_reason}.db"
    await init_db(db_file)

    async with get_db(str(db_file)) as conn:
        user_msg = await crud.add_message(
            conn,
            MessageCreate(session_id="cf_sess", role="user", content_chinese="触发过滤"),
        )

    async def cf_stream(*args, **kwargs):
        yield '{"lines": [{"zh": "你好呀", "ja": "こんにちは"}], "unclosed": "'
        validate_stream_completion(
            yielded_any=True,
            saw_terminal=False,
            finish_reason=finish_reason,
        )

    adapter = SimpleNamespace(
        stream_chat=cf_stream,
        api_key="synthetic",
        last_finish_reason=finish_reason,
    )

    coordinator = StreamCoordinator(
        adapter=adapter,
        messages=[],
        model_name="test",
        actual_provider_id="custom",
        session_id="cf_sess",
        prompt="触发过滤",
        user_msg=user_msg,
        db_path=str(db_file),
        metrics_collector=SimpleNamespace(record_chat_turn=AsyncMock(return_value={})),
        affection_service=SimpleNamespace(handle_turn_affection=AsyncMock(return_value={"emotion": "gentle"})),
        tts_service=None,
        concat_wav_fn=lambda *args: True,
    )

    events = [e async for e in coordinator]
    error_events = [e for e in events if e.get("event") == "error"]
    done_events = [e for e in events if e.get("event") == "done"]

    assert len(error_events) == 1
    err_data = error_events[0]["data"]
    assert STREAM_TRUNCATED_CONTENT_FILTERED in err_data["error"]
    assert err_data.get("error_code") == "stream_truncated_content_filtered"
    assert err_data.get("diagnostic") == STREAM_TRUNCATED_CONTENT_FILTERED

    assert len(done_events) == 1
    assert done_events[0]["data"]["truncated"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("finish_reason", ["content_filter", "SAFETY"])
async def test_streaming_empty_reply_content_filtered(tmp_path, finish_reason):
    """Verifies that empty reply with content filter finish reason uses content-filtered explanation."""
    db_file = tmp_path / f"stream_empty_cf_{finish_reason}.db"
    await init_db(db_file)

    async with get_db(str(db_file)) as conn:
        user_msg = await crud.add_message(
            conn,
            MessageCreate(session_id="empty_cf", role="user", content_chinese="违规问题"),
        )

    async def empty_tokens(*args, **kwargs):
        if False:
            yield ""

    adapter = SimpleNamespace(
        stream_chat=empty_tokens,
        api_key="synthetic",
        last_finish_reason=finish_reason,
    )

    coordinator = StreamCoordinator(
        adapter=adapter,
        messages=[],
        model_name="test",
        actual_provider_id="custom",
        session_id="empty_cf",
        prompt="违规问题",
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
    assert data["error"] == EMPTY_REPLY_CONTENT_FILTERED

    async with get_db(str(db_file)) as conn:
        remaining = await crud.get_recent_messages(conn, "empty_cf")
        assert len(remaining) == 0


@pytest.mark.parametrize("finish_reason", ["length", "MAX_TOKENS", "max_tokens"])
def test_stream_length_truncation_text_matches_constant(finish_reason):
    from galgame2voice.adapters.base import validate_stream_completion
    from galgame2voice.utils.error_diagnostics import StreamTruncatedError

    with pytest.raises(StreamTruncatedError) as exc_info:
        validate_stream_completion(yielded_any=True, saw_terminal=True, finish_reason=finish_reason)
    assert exc_info.value.stopped_on_length is True
    assert str(exc_info.value) == STREAM_TRUNCATED_ON_LENGTH
    assert STREAM_TRUNCATED_ON_LENGTH == (
        "模型输出达到最大长度限制，回复未能完整生成。建议调大「最大生成长度」或降低「思考强度」。"
    )


def test_deepseek_payload_translates_reasoning_effort():
    deepseek = get_llm_adapter("deepseek", api_key="sk-test")
    msgs = [{"role": "user", "content": "hi"}]
    off_payload = deepseek._build_payload(msgs, model="deepseek-flash", extra={"reasoning_effort": "none"})
    assert off_payload.get("thinking") == {"type": "disabled"}
    assert "reasoning_effort" not in off_payload
    low_payload = deepseek._build_payload(msgs, model="deepseek-flash", extra={"reasoning_effort": "low"})
    assert low_payload.get("reasoning_effort") == "low"
    assert "thinking" not in low_payload
    openai = OpenAICompatibleLLMAdapter(api_key="sk-test", base_url="https://api.openai.com/v1")
    openai_off = openai._build_payload(msgs, model="gpt-4o", extra={"reasoning_effort": "none"})
    assert openai_off.get("reasoning_effort") == "none"
    assert "thinking" not in openai_off


@pytest.mark.asyncio
async def test_deepseek_connectivity_probe_translates_reasoning_effort():
    import httpx

    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        ds_off = get_llm_adapter("deepseek", api_key="sk-test", reasoning_effort="none")
        await ds_off._probe_chat_fallback(client, "deepseek-flash", {}, "deepseek", time.perf_counter())
        ds_low = get_llm_adapter("deepseek", api_key="sk-test", reasoning_effort="low")
        await ds_low._probe_chat_fallback(client, "deepseek-flash", {}, "deepseek", time.perf_counter())
        oa_off = OpenAICompatibleLLMAdapter(api_key="sk-test", reasoning_effort="none")
        await oa_off._probe_chat_fallback(client, "gpt-4o", {}, "openai", time.perf_counter())
    assert sent[0].get("thinking") == {"type": "disabled"} and "reasoning_effort" not in sent[0]
    assert sent[1].get("reasoning_effort") == "low" and "thinking" not in sent[1]
    assert sent[2].get("reasoning_effort") == "none" and "thinking" not in sent[2]


@pytest.mark.asyncio
async def test_openai_gpt6_payload_and_probe_rules():
    import httpx
    from galgame2voice.adapters.llm import OpenAILLMAdapter

    oa = get_llm_adapter("openai", api_key="sk-test")
    assert isinstance(oa, OpenAILLMAdapter)
    msgs = [{"role": "user", "content": "hi"}]
    extra = {"top_p": 1.0, "max_tokens": 1024, "logprobs": True, "top_logprobs": 2}

    luna_none = oa._build_payload(msgs, "gpt-6-luna", temperature=0.7, extra={**extra, "reasoning_effort": "none"})
    assert (luna_none["temperature"], luna_none["top_p"], luna_none["reasoning_effort"]) == (0.7, 1.0, "none")
    assert luna_none["max_completion_tokens"] == 1024 and "max_tokens" not in luna_none

    luna_low = oa._build_payload(msgs, "gpt-6-luna", temperature=0.7, extra={**extra, "reasoning_effort": "low"})
    for dropped in ("temperature", "top_p", "logprobs", "top_logprobs", "max_tokens"):
        assert dropped not in luna_low
    assert luna_low["max_completion_tokens"] == 1024

    luna_default = oa._build_payload(msgs, "gpt-6-luna", temperature=0.7, extra=extra)
    assert "temperature" not in luna_default and "top_p" not in luna_default

    sol_none = oa._build_payload(msgs, "gpt-6.1-sol", temperature=0.7, extra={**extra, "reasoning_effort": "none"})
    assert sol_none["reasoning_effort"] == "low" and "temperature" not in sol_none

    gpt4o = oa._build_payload(msgs, "gpt-4o", temperature=0.7, extra=extra)
    assert gpt4o["temperature"] == 0.7 and gpt4o["max_completion_tokens"] == 1024 and "max_tokens" not in gpt4o

    custom = get_llm_adapter("custom", api_key="sk-test")
    custom_payload = custom._build_payload(msgs, "gpt-6-luna", temperature=0.7, extra={"max_tokens": 1024})
    assert custom_payload["max_tokens"] == 1024 and "max_completion_tokens" not in custom_payload
    assert custom_payload["temperature"] == 0.7

    probed: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        probed.append(json.loads(req.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await oa._probe_chat_fallback(client, "gpt-6.1-sol", {}, "openai", time.perf_counter())
        await custom._probe_chat_fallback(client, "qwen3:latest", {}, "custom", time.perf_counter())
    assert probed[0].get("max_completion_tokens") == 1 and "max_tokens" not in probed[0]
    assert probed[1].get("max_tokens") == 1 and "max_completion_tokens" not in probed[1]
