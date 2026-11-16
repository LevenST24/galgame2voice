"""Shared backend contract tests for /api/chat/stream SSE frames and /api/chat/turn-reply."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import time
from typing import Any, AsyncIterator, List, Optional
from unittest.mock import AsyncMock

from httpx import ASGITransport, AsyncClient
import pytest

from galgame2voice.adapters.base import BaseLLMAdapter, ChatMessage, LLMResponse, TestResult
from galgame2voice.database import crud
from galgame2voice.database.models import MessageResponse, ReplyLineRecord, decode_lines
from galgame2voice.database.session import get_db, init_db
from galgame2voice.main import create_app
from galgame2voice.routers.chat import ChatRequest, get_turn_reply, set_chat_service, sse_event_formatter
from galgame2voice.services.chat_pipelines.line_scanner import FX, MOTIONS, MOVES
from galgame2voice.services.chat_pipelines.stream_coordinator import StreamCoordinator
from galgame2voice.services.chat_service import ChatService, SseKeepAlive
from galgame2voice.services.gpt_sovits_client import GptSovitsClient
from galgame2voice.services.tts_service import TtsService
from galgame2voice.utils.sse import format_sse_frame
from scripts import package_release

CONTRACT_PATH = Path(__file__).resolve().parent / "contracts" / "sse_turn_reply_v1.json"


def load_contract() -> dict[str, Any]:
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))


def parse_sse_frame(frame: str) -> dict[str, Any]:
    """Parse a serialized SSE frame back into an ``{"event": ..., "data": ...}`` dict."""
    if not isinstance(frame, str) or not frame.strip():
        raise ValueError("Empty SSE frame")
    event_type = "message"
    data_lines: list[str] = []
    for raw_line in frame.splitlines():
        if raw_line.startswith("event:"):
            event_type = raw_line[6:].strip()
        elif raw_line.startswith("data:"):
            data_lines.append(raw_line[5:].strip())
    if not data_lines:
        raise ValueError(f"SSE frame missing data field: {frame!r}")
    data_str = "\n".join(data_lines)
    return {
        "event": event_type,
        "data": json.loads(data_str),
    }


def _iter_sse_samples(node: Any, prefix: str = "") -> list[tuple[str, dict[str, Any]]]:
    found: list[tuple[str, dict[str, Any]]] = []
    if isinstance(node, dict):
        if isinstance(node.get("event"), str) and isinstance(node.get("data"), dict):
            found.append((prefix, {"event": node["event"], "data": node["data"]}))
        for key, value in node.items():
            if key in ("event", "data", "expected"):
                continue
            child_prefix = f"{prefix}.{key}" if prefix else key
            found.extend(_iter_sse_samples(value, child_prefix))
    elif isinstance(node, list):
        for idx, item in enumerate(node):
            found.extend(_iter_sse_samples(item, f"{prefix}[{idx}]"))
    return found


def test_contract_schema_version_and_release_packaging():
    contract = load_contract()
    assert contract["schema_version"] == "sse_turn_reply_v1"
    assert package_release.should_include(Path("tests/contracts/sse_turn_reply_v1.json")) is True


@pytest.mark.asyncio
async def test_sse_events_and_edge_cases_roundtrip_through_real_formatter():
    contract = load_contract()
    samples = (
        _iter_sse_samples(contract["sse_events"], "sse_events")
        + _iter_sse_samples(contract["edge_cases"], "edge_cases")
    )
    assert len(samples) >= 20

    for name, sample in samples:
        frame = format_sse_frame(sample)
        assert frame.startswith(f"event: {sample['event']}\ndata: "), f"Invalid frame prefix for {name}"
        assert frame.endswith("\n\n"), f"Frame must end with double newline for {name}"
        roundtripped = parse_sse_frame(frame)
        assert roundtripped == sample, f"Roundtrip mismatch for {name}"

    async def _event_source():
        for sample in contract["sse_events"].values():
            yield sample

    formatted_frames = [frame async for frame in sse_event_formatter(_event_source())]
    assert [parse_sse_frame(f) for f in formatted_frames] == list(contract["sse_events"].values())


def test_done_lines_and_persisted_message_validate_against_pydantic_models():
    contract = load_contract()
    done_complete_lines = contract["sse_events"]["done_complete"]["data"]["lines"]
    done_truncated_lines = contract["sse_events"]["done_truncated"]["data"]["lines"]

    for line in done_complete_lines:
        validated_line = ReplyLineRecord.model_validate(line)
        assert validated_line.model_dump(exclude_unset=True) == line

    for line in done_truncated_lines:
        validated_line = ReplyLineRecord.model_validate(line)
        assert validated_line.model_dump(exclude_unset=True) == line

    assert decode_lines(done_complete_lines) == done_complete_lines
    assert decode_lines(json.dumps(done_complete_lines, ensure_ascii=False)) == done_complete_lines
    assert decode_lines(done_truncated_lines) == done_truncated_lines

    persisted_sample = contract["persisted_assistant_message"]
    validated_msg = MessageResponse.model_validate(persisted_sample)
    assert validated_msg.model_dump() == persisted_sample


@pytest.mark.asyncio
async def test_real_stream_coordinator_emits_contract_matching_events(tmp_path):
    contract = load_contract()
    ev = contract["sse_events"]
    db_path = tmp_path / "coordinator_contract.db"
    await init_db(db_path)

    async with get_db(db_path) as conn:
        await crud.get_or_create_session(conn, "sess_contract_1")
        await conn.execute(
            "INSERT INTO messages (id, session_id, role, content_chinese) VALUES (101, 'sess_contract_1', 'user', '早安');"
        )
        await conn.commit()
        cur = await conn.execute("SELECT * FROM messages WHERE id = 101;")
        user_row = await cur.fetchone()
        user_msg = MessageResponse(**dict(user_row))

    wav_path = tmp_path / "contract_0.wav"
    wav_path.write_bytes(b"RIFF\x24\x00\x00\x00WAVEfmt ")

    async def synthesize_to_file(sentence: str, **kwargs: Any):
        if sentence == "おはよう。":
            return "/audio/cache/contract_0.wav", wav_path, {}
        raise RuntimeError("GPT-SoVITS timeout")

    tts_service = SimpleNamespace(
        audio_dir=tmp_path,
        synthesize_to_file=AsyncMock(side_effect=synthesize_to_file),
    )

    llm_payload = {
        "lines": [
            {
                "zh": "早安。",
                "ja": "おはよう。",
                "emotion": "happy",
                "act": {"face": "smile", "motion": "nod", "blush": False, "fx": None},
            },
            {
                "zh": "（轻轻挥手）",
                "ja": "",
                "emotion": "gentle",
                "act": {"motion": None, "blush": False, "fx": None},
            },
            {
                "zh": "听得到吗？",
                "ja": "聞こえる？",
                "emotion": "cool",
                "act": {"motion": None, "blush": False, "fx": "question"},
            },
        ]
    }

    async def stream_tokens(*args: Any, **kwargs: Any):
        yield json.dumps(llm_payload, ensure_ascii=False)

    acting_menu = {
        "faces": ["smile"],
        "poses": [],
        "blush": True,
        "motions": list(MOTIONS),
        "moves": list(MOVES),
        "fx": list(FX),
    }

    coordinator = StreamCoordinator(
        adapter=SimpleNamespace(stream_chat=stream_tokens, api_key="synthetic"),
        messages=[],
        model_name="contract-test-model",
        actual_provider_id="custom",
        session_id="sess_contract_1",
        prompt="早安",
        user_msg=user_msg,
        tts_service=tts_service,
        db_path=str(db_path),
        metrics_collector=SimpleNamespace(record_chat_turn=AsyncMock(return_value={})),
        affection_service=SimpleNamespace(handle_turn_affection=AsyncMock(return_value={"emotion": "happy"})),
        concat_wav_fn=lambda *args: True,
        reply_format="lines",
        acting_menu=acting_menu,
    )

    events = [event async for event in coordinator]
    assert events[0] == ev["turn_started"]

    line_audio_events = [e for e in events if e["event"] == "line_audio"]
    assert line_audio_events == [
        ev["line_audio_ready"],
        ev["line_audio_skipped"],
        ev["line_audio_failed"],
    ]

    audio_chunks = [e for e in events if e["event"] == "audio_chunk"]
    assert len(audio_chunks) == 1
    for key in ("index", "line", "sentence", "audio_url", "emotion"):
        assert audio_chunks[0]["data"][key] == ev["audio_chunk_0"]["data"][key]

    done_event = events[-1]
    assert done_event["event"] == "done"
    assert done_event["data"]["chinese"] == ev["done_complete"]["data"]["chinese"]
    assert done_event["data"]["japanese"] == ev["done_complete"]["data"]["japanese"]
    assert done_event["data"]["audio_url"] == ev["done_complete"]["data"]["audio_url"]
    assert done_event["data"]["total_audio_url"] == ev["done_complete"]["data"]["total_audio_url"]
    assert done_event["data"]["chunks"] == ev["done_complete"]["data"]["chunks"]
    assert done_event["data"]["truncated"] is False
    assert done_event["data"]["message_id"] == ev["done_complete"]["data"]["message_id"] == 102
    assert [line["audio"] for line in done_event["data"]["lines"]] == [
        line["audio"] for line in ev["done_complete"]["data"]["lines"]
    ]


@pytest.mark.asyncio
async def test_turn_reply_route_matches_all_five_contract_statuses(tmp_path, monkeypatch):
    contract = load_contract()
    expected_responses = contract["turn_reply_responses"]
    persisted_sample = contract["persisted_assistant_message"]

    db_path = tmp_path / "turn_reply_contract.db"
    await init_db(db_path)
    monkeypatch.setenv("GALGAME2VOICE_DB_PATH", str(db_path))
    set_chat_service(None)

    app = create_app()
    transport = ASGITransport(app=app)

    try:
        async with get_db(db_path) as conn:
            await crud.get_or_create_session(conn, "sess_contract_1")
            await conn.execute(
                "INSERT INTO messages (id, session_id, role, content_chinese) VALUES (101, 'sess_contract_1', 'user', '早安');"
            )
            await conn.commit()

        async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as client:
            # 1. pending: user message 101 exists, no assistant reply yet
            direct_pending = await get_turn_reply(session_id="sess_contract_1", user_message_id=101)
            assert direct_pending == expected_responses["pending"]
            resp_pending = await client.get(
                "/api/chat/turn-reply",
                params={"session_id": "sess_contract_1", "user_message_id": 101},
            )
            assert resp_pending.status_code == 200
            assert resp_pending.json() == expected_responses["pending"]

            # 2. gone: non-existent user_message_id 999
            direct_gone = await get_turn_reply(session_id="sess_contract_1", user_message_id=999)
            assert direct_gone == expected_responses["gone"]
            resp_gone = await client.get(
                "/api/chat/turn-reply",
                params={"session_id": "sess_contract_1", "user_message_id": 999},
            )
            assert resp_gone.status_code == 200
            assert resp_gone.json() == expected_responses["gone"]

            # 3. ready: single committed assistant reply (id=102) linked to user_message_id=101
            async with get_db(db_path) as conn:
                await conn.execute(
                    """
                    INSERT INTO messages (
                        id, session_id, role, content_chinese, content_japanese,
                        audio_url, latency_ms, lines_json, reply_to_message_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        persisted_sample["id"],
                        persisted_sample["session_id"],
                        persisted_sample["role"],
                        persisted_sample["content_chinese"],
                        persisted_sample["content_japanese"],
                        persisted_sample["audio_url"],
                        persisted_sample["latency_ms"],
                        json.dumps(persisted_sample["lines"], ensure_ascii=False),
                        persisted_sample["reply_to_message_id"],
                        persisted_sample["created_at"],
                    ),
                )
                await conn.commit()

            direct_ready = await get_turn_reply(session_id="sess_contract_1", user_message_id=101)
            assert direct_ready == expected_responses["ready"]
            resp_ready = await client.get(
                "/api/chat/turn-reply",
                params={"session_id": "sess_contract_1", "user_message_id": 101},
            )
            assert resp_ready.status_code == 200
            assert resp_ready.json() == expected_responses["ready"]

            resp_history = await client.get(
                "/api/chat/history",
                params={"session_id": "sess_contract_1", "limit": 100},
            )
            assert resp_history.status_code == 200
            history_messages = resp_history.json()["messages"]
            assert history_messages[-1] == persisted_sample

            # 4. ambiguous: second assistant reply linked to the same user_message_id=101
            async with get_db(db_path) as conn:
                await conn.execute(
                    """
                    INSERT INTO messages (
                        id, session_id, role, content_chinese, reply_to_message_id
                    ) VALUES (103, 'sess_contract_1', 'assistant', '分叉回复', 101);
                    """
                )
                await conn.commit()

            direct_ambiguous = await get_turn_reply(session_id="sess_contract_1", user_message_id=101)
            assert direct_ambiguous == expected_responses["ambiguous"]
            resp_ambiguous = await client.get(
                "/api/chat/turn-reply",
                params={"session_id": "sess_contract_1", "user_message_id": 101},
            )
            assert resp_ambiguous.status_code == 200
            assert resp_ambiguous.json() == expected_responses["ambiguous"]

            # 5. unavailable: database locked by exclusive transaction
            sync_conn = sqlite3.connect(str(db_path), timeout=0.0)
            try:
                sync_conn.execute("PRAGMA locking_mode = EXCLUSIVE;")
                sync_conn.execute("BEGIN EXCLUSIVE;")

                direct_unavailable = await asyncio.wait_for(
                    get_turn_reply(session_id="sess_contract_1", user_message_id=101),
                    timeout=2.0,
                )
                assert direct_unavailable == expected_responses["unavailable"]

                resp_unavailable = await asyncio.wait_for(
                    client.get(
                        "/api/chat/turn-reply",
                        params={"session_id": "sess_contract_1", "user_message_id": 101},
                    ),
                    timeout=2.0,
                )
                assert resp_unavailable.status_code == 200
                assert resp_unavailable.json() == expected_responses["unavailable"]
            finally:
                sync_conn.rollback()
                sync_conn.close()
    finally:
        set_chat_service(None)


class TestSseStreamingBackpressureAndDisconnect:
    """Tests SSE streaming pipeline cancellation, backpressure, and disconnect handling."""

    @pytest.mark.asyncio
    async def test_chat_request_stream_flag_contract(self):
        """Verify ChatRequest accepts stream boolean sent by frontend."""
        req = ChatRequest(prompt="Hello", session_id="test", stream=True)
        assert req.stream is True
        req_false = ChatRequest(prompt="Hello", session_id="test", stream=False)
        assert req_false.stream is False


class TestSseKeepAliveFormatting:
    """Verifies that sse_event_formatter properly adheres to W3C SSE comment specifications."""

    @pytest.mark.asyncio
    async def test_sse_event_formatter_keep_alive_dict(self):
        async def dummy_gen():
            yield {"event": "text", "data": {"delta_chinese": "你好"}}
            yield SseKeepAlive()
            yield {"comment": ": keep-alive\n\n"}
            yield ": keep-alive\n\n"
            yield {"event": "done", "data": {"truncated": False}}

        formatted = []
        async for chunk in sse_event_formatter(dummy_gen()):
            formatted.append(chunk)

        assert len(formatted) == 5
        assert formatted[0] == 'event: text\ndata: {"delta_chinese": "你好"}\n\n'
        assert formatted[1] == ': keep-alive\n\n'
        assert formatted[2] == ': keep-alive\n\n'
        assert formatted[3] == ': keep-alive\n\n'
        assert 'event: done' in formatted[4]


class MockPausingLLMAdapter(BaseLLMAdapter):
    """Adapter that pauses mid-stream to trigger SSE keep-alive heartbeats."""

    def __init__(self, pause_seconds: float = 0.6):
        super().__init__(api_key="sk-mock-pause", base_url="http://mock-pause")
        self.pause_seconds = pause_seconds
        self.paused_event = asyncio.Event()

    async def chat(self, messages: List[ChatMessage], model: str, **kwargs: Any) -> LLMResponse:
        return LLMResponse(content='{"chinese": "你好", "japanese": "こんにちは"}')

    async def stream_chat(self, messages: List[ChatMessage], model: str, **kwargs: Any) -> AsyncIterator[str]:
        yield '{"chinese": "你好", "japanese": "'
        self.paused_event.set()
        await asyncio.sleep(self.pause_seconds)
        yield 'こんにちは！"}'

    async def test_connection(self, model: Optional[str] = None) -> TestResult:
        return TestResult(success=True, message="OK", latency_ms=1.0)

    async def list_models(self) -> List[str]:
        return ["mock-pause-model"]


class TestR3SseKeepAlive:
    """Verifies W3C SSE keep-alive comment frames (: keep-alive\\n\\n) on silence."""

    @pytest.mark.asyncio
    async def test_r3_sse_keep_alive_on_queue_silence(self, temp_db_path, mock_gpt_sovits, monkeypatch):
        """Verifies stream_chat emits SseKeepAlive with ': keep-alive\\n\\n' on silence."""
        client = GptSovitsClient(server=mock_gpt_sovits)
        tts_service = TtsService(client=client, db_path=temp_db_path)
        chat_service = ChatService(tts_service=tts_service, db_path=temp_db_path)

        adapter = MockPausingLLMAdapter(pause_seconds=0.8)

        async def _mock_adapter_getter(conn=None, provider_id=None):
            return adapter, "mock-pause-model", "mock-prov"

        chat_service._get_active_llm_adapter = _mock_adapter_getter

        # Accelerate monotonic time during pause so queue timeout (0.5s) triggers >= 5.0s jump
        real_monotonic = time.monotonic
        time_offset = 0.0

        def accelerated_monotonic():
            return real_monotonic() + time_offset

        monkeypatch.setattr(time, "monotonic", accelerated_monotonic)

        # Task to shift virtual clock during queue silence after the first token is consumed
        async def _shift_clock():
            await adapter.paused_event.wait()
            await asyncio.sleep(0.05)
            nonlocal time_offset
            time_offset = 6.0

        asyncio.create_task(_shift_clock())

        # 1. Test stream_chat yields SseKeepAlive frame
        events = []
        async for ev in chat_service.stream_chat(prompt="测试保活心跳", session_id="ka_sess_1"):
            events.append(ev)

        keep_alive_events = [ev for ev in events if isinstance(ev, SseKeepAlive) or ev == ": keep-alive\n\n"]
        assert len(keep_alive_events) >= 1, "stream_chat must yield SseKeepAlive frame during silence"
        assert keep_alive_events[0].get("comment") == ": keep-alive\n\n"
