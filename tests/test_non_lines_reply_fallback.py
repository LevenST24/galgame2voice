"""Regression tests for non-lines whole-reply fallback SSE done payloads."""

from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
import struct
from types import SimpleNamespace
from unittest.mock import AsyncMock
import wave

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest

from galgame2voice.config import get_settings
from galgame2voice.database import crud
from galgame2voice.database.models import MessageCreate, VoiceProfileCreate
from galgame2voice.database.session import get_db, init_db
from galgame2voice.routers import chat as chat_router
from galgame2voice.routers.voice import router as voice_router
from galgame2voice.services.chat_pipelines.reply_contract import format_lines_turn
from galgame2voice.services.chat_pipelines.stream_coordinator import StreamCoordinator
from galgame2voice.services.chat_service import ChatService
from galgame2voice.services.tts_cache_manager import TtsCacheManager
from galgame2voice.services.tts_scheduler import TtsScheduler
from galgame2voice.services.tts_service import TtsService
from galgame2voice.services.voice_manager import get_voice_manager


def _make_pcm_wav(duration_ms: int = 120) -> bytes:
    sample_rate = 16000
    frames = int(sample_rate * duration_ms / 1000)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(struct.pack("<" + "h" * frames, *([0] * frames)))
    return buf.getvalue()


def _make_tts_stub(tmp_path: Path):
    wav_bytes = _make_pcm_wav(120)

    async def synthesize(sentence, **kwargs):
        path = tmp_path / f"{kwargs['filename_prefix']}.wav"
        path.write_bytes(wav_bytes)
        return f"/audio/{path.name}", path, {}

    return SimpleNamespace(
        audio_dir=tmp_path,
        synthesize_to_file=AsyncMock(side_effect=synthesize),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case_id", "raw_reply"),
    [
        ("plain_bilingual", "中文：早安。\n日文：おはよう。"),
        ("legacy_json", '{"chinese": "早安。", "japanese": "おはよう。", "emotion": "happy"}'),
    ],
)
async def test_non_lines_completion_emits_null_lines_matching_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case_id: str,
    raw_reply: str,
):
    db_file = tmp_path / f"{case_id}.db"
    audio_dir = tmp_path / f"audio_{case_id}"
    audio_dir.mkdir()
    ref_wav = tmp_path / f"ref_{case_id}.wav"
    ref_wav.write_bytes(_make_pcm_wav(100))
    monkeypatch.setenv("GALGAME2VOICE_DB_PATH", str(db_file))
    monkeypatch.setenv("GALGAME2VOICE_CONSOLE_TOKEN", "test-console-token")
    get_settings.cache_clear()
    await init_db(db_file)

    session_id = f"sess_{case_id}"
    async with get_db(db_file) as conn:
        profile = await crud.create_voice_profile(
            conn,
            VoiceProfileCreate(
                name="测试音色",
                gpt_weights_path="models/m.ckpt",
                sovits_weights_path="models/m.pth",
                ref_audio_path=str(ref_wav),
                prompt_text="テスト",
                prompt_lang="ja",
                text_lang="ja",
                is_default=True,
            ),
        )
        await crud.set_active_voice_profile(conn, profile.id)
        user_msg = await crud.add_message(
            conn,
            MessageCreate(session_id=session_id, role="user", content_chinese="早安"),
        )

    async def stream_tokens(*args, **kwargs):
        for ch in (raw_reply[:6], raw_reply[6:]):
            yield ch
            await asyncio.sleep(0)

    tts_stub = _make_tts_stub(audio_dir)
    coordinator = StreamCoordinator(
        adapter=SimpleNamespace(stream_chat=stream_tokens, api_key="synthetic"),
        messages=[],
        model_name="small-local-model",
        actual_provider_id="custom",
        session_id=session_id,
        prompt="早安",
        user_msg=user_msg,
        tts_service=tts_stub,
        db_path=str(db_file),
        metrics_collector=SimpleNamespace(record_chat_turn=AsyncMock(return_value={})),
        affection_service=SimpleNamespace(handle_turn_affection=AsyncMock(return_value={"emotion": "happy"})),
        concat_wav_fn=lambda *args: True,
    )

    events = [event async for event in coordinator]
    lines = [event["data"] for event in events if event["event"] == "line"]
    assert len(lines) == 1
    assert lines[0]["index"] == 0
    assert lines[0]["zh"] == "早安。"
    assert lines[0]["ja"] == "おはよう。"
    assert lines[0]["act"] == {}
    line_audio = [event["data"] for event in events if event["event"] == "line_audio"]
    assert len(line_audio) == 1 and line_audio[0]["line"] == 0
    assert any(e["event"] == "audio_chunk" for e in events)

    done = events[-1]
    assert done["event"] == "done"
    assert done["data"]["chinese"] == "早安。"
    assert done["data"]["japanese"] == "おはよう。"
    assert done["data"]["lines"] is None

    async with get_db(db_file) as conn:
        history = await crud.get_recent_messages(conn, session_id)
        saved = history[-1]
        assert saved.role == "assistant"
        assert saved.lines is None

    # Round-trip through chat backup import using the exact lines value from done["data"]["lines"]
    # (mirrors frontend ai.js -> conversation.js -> portableSnapshot -> POST /api/chat/import).
    imported_session_id = f"s_import_{case_id}"
    portable_msg: dict = {
        "id": "m_fallback_1",
        "role": "assistant",
        "content": done["data"]["chinese"],
        "japanese": done["data"]["japanese"],
        "ts": 1710000000000,
    }
    if isinstance(done["data"]["lines"], list):
        portable_msg["lines"] = done["data"]["lines"]

    engine = SimpleNamespace(
        current_refer_audio=None,
        current_gpt_weights=None,
        current_sovits_weights=None,
        synthesize=AsyncMock(return_value=_make_pcm_wav(160)),
    )
    scheduler = TtsScheduler()
    monkeypatch.setattr("galgame2voice.services.tts_service.get_tts_scheduler", lambda: scheduler)
    cache = TtsCacheManager(cache_dir=audio_dir / "cache", db_path=str(db_file))
    real_tts = TtsService(client=engine, audio_dir=audio_dir, cache_manager=cache, db_path=str(db_file))

    app = FastAPI()
    app.include_router(chat_router.router)
    app.include_router(voice_router)
    app.dependency_overrides[get_voice_manager] = lambda: SimpleNamespace(tts_service=real_tts)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8000") as client:
            imp_resp = await client.post(
                "/api/chat/import",
                json={
                    "version": 1,
                    "activeId": imported_session_id,
                    "sessions": [
                        {
                            "id": imported_session_id,
                            "title": "导入兜底会话",
                            "createdAt": 1710000000000,
                            "updatedAt": 1710000000000,
                            "settings": {},
                            "messages": [
                                {"id": "u_1", "role": "user", "content": "早安", "ts": 1709999999000},
                                portable_msg,
                            ],
                        }
                    ],
                },
            )
            assert imp_resp.status_code == 200, imp_resp.text

            hist_resp = await client.get("/api/chat/history", params={"session_id": imported_session_id})
            assert hist_resp.status_code == 200, hist_resp.text
            imported_assistant = next(m for m in hist_resp.json()["messages"] if m["role"] == "assistant")
            assert imported_assistant["lines"] is None

            replayed = json.loads(
                format_lines_turn(
                    imported_assistant["lines"],
                    chinese=imported_assistant["content_chinese"],
                    japanese=imported_assistant["content_japanese"],
                    emotion=None,
                )
            )
            assert [item["zh"] for item in replayed["lines"]] == ["早安。"]
            assert [item["ja"] for item in replayed["lines"]] == ["おはよう。"]

            regen_resp = await client.post(
                "/api/voice/regenerate-line",
                json={
                    "session_id": imported_session_id,
                    "message_id": imported_assistant["id"],
                    "line_index": 0,
                    "text": "おはよう。",
                    "voice_profile_id": profile.id,
                },
            )
            assert regen_resp.status_code == 200, regen_resp.text
            assert regen_resp.json()["audio"]["status"] == "ready"
    finally:
        await scheduler.aclose()
        await cache.aclose()
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_non_lines_early_exit_emits_null_lines_matching_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    db_file = tmp_path / "early_non_lines.db"
    monkeypatch.setenv("GALGAME2VOICE_CONSOLE_TOKEN", "test-console-token")
    await init_db(db_file)
    async with get_db(db_file) as conn:
        user_msg = await crud.add_message(
            conn,
            MessageCreate(session_id="sess_early", role="user", content_chinese="早安"),
        )

    cancel = asyncio.Event()
    release = asyncio.Event()

    async def stream_tokens(*args, **kwargs):
        yield "中文：早安。\n日文：おはよう。"
        await release.wait()
        yield "再多说一句。"

    coordinator = StreamCoordinator(
        adapter=SimpleNamespace(stream_chat=stream_tokens, api_key="synthetic"),
        messages=[],
        model_name="small-local-model",
        actual_provider_id="custom",
        session_id="sess_early",
        prompt="早安",
        user_msg=user_msg,
        tts_service=_make_tts_stub(tmp_path),
        db_path=str(db_file),
        metrics_collector=SimpleNamespace(record_chat_turn=AsyncMock(return_value={})),
        affection_service=SimpleNamespace(handle_turn_affection=AsyncMock(return_value={"emotion": "gentle"})),
        concat_wav_fn=lambda *args: True,
        cancel_event=cancel,
    )

    stream = coordinator.stream()
    events = []
    try:
        while not any(e["event"] == "text" for e in events):
            events.append(await asyncio.wait_for(stream.__anext__(), 2))
        cancel.set()
        events.extend([e async for e in stream])
    finally:
        release.set()
        await stream.aclose()

    done = events[-1]
    assert done["event"] == "done"
    assert done["data"]["truncated"] is True
    assert done["data"]["lines"] is None

    async with get_db(db_file) as conn:
        saved = (await crud.get_recent_messages(conn, "sess_early"))[-1]
        assert saved.role == "assistant"
        assert saved.lines is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case_id", "raw_reply", "chinese", "japanese"),
    [
        (
            "legacy_json",
            '{"tts": {"speed": 1.05, "temp": 0.95, "emotion": "happy"}, '
            '"chinese": "早安。下次见。", "japanese": "おはよう。また今度。"}',
            "早安。下次见。",
            "おはよう。また今度。",
        ),
        (
            "plain_text",
            "中文：早安。下次见。\n日文：おはよう。また今度。",
            "早安。下次见。",
            "おはよう。また今度。",
        ),
        (
            "truncated_json",
            '{"tts": {"speed": 1.05, "temp": 0.95, "emotion": "happy"}, '
            '"chinese": "早安。下次见。", "japanese": "おはよう。また今度。',
            "早安。下次见。",
            "おはよう。また今度。",
        ),
    ],
)
async def test_non_lines_reply_emits_line_before_hanging_tts_returns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case_id: str,
    raw_reply: str,
    chinese: str,
    japanese: str,
):
    """The route asks for lines, but a non-lines reply must still publish its line before TTS returns."""
    db_file = tmp_path / f"{case_id}.db"
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    monkeypatch.setenv("GALGAME2VOICE_DB_PATH", str(db_file))
    monkeypatch.setenv("GALGAME2VOICE_CONSOLE_TOKEN", "test-console-token")
    get_settings.cache_clear()
    await init_db(db_file)

    captured_messages: list = []
    release = asyncio.Event()
    entered = asyncio.Event()
    returned: list[str] = []

    async def stream_tokens(messages, *args, **kwargs):
        captured_messages.append(list(messages))
        yield raw_reply

    async def synthesize(sentence, **kwargs):
        entered.set()
        if not release.is_set():
            await release.wait()
        returned.append(sentence)
        path = audio_dir / f"{kwargs['filename_prefix']}.wav"
        path.write_bytes(_make_pcm_wav(80))
        return f"/audio/{path.name}", path, {}

    service = ChatService(
        db_path=db_file,
        tts_service=SimpleNamespace(
            audio_dir=audio_dir,
            synthesize_to_file=synthesize,
        ),
    )
    service._get_active_llm_adapter = AsyncMock(
        return_value=(
            SimpleNamespace(stream_chat=stream_tokens, api_key="sk-test", provider_type="custom"),
            "mock-model",
            "custom",
        )
    )
    service._extract_memory_safe = AsyncMock()

    events: list[dict] = []
    line_seen = asyncio.Event()

    async def drive() -> None:
        async for event in service.stream_chat("你好", session_id=f"hang_{case_id}"):
            if not isinstance(event, dict) or event.get("event") == ":keep-alive":
                continue
            events.append(event)
            if event.get("event") == "line":
                line_seen.set()

    task = asyncio.create_task(drive())
    try:
        try:
            await asyncio.wait_for(line_seen.wait(), timeout=5)
        except asyncio.TimeoutError:
            release.set()
            await asyncio.wait_for(task, timeout=20)
            kinds = [event["event"] for event in events]
            raise AssertionError(
                "line event did not arrive while TTS synthesize_to_file was still blocked; "
                f"events={kinds}"
            ) from None
        assert returned == [], "line event must be emitted before synthesize_to_file returns"
        try:
            await asyncio.wait_for(entered.wait(), timeout=2)
        except asyncio.TimeoutError:
            raise AssertionError("voice synthesis was never started for the non-lines reply") from None
        assert returned == [], "synthesize_to_file returned before the line event was observed"
        release.set()
        await asyncio.wait_for(task, timeout=20)
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await service.aclose()
        get_settings.cache_clear()

    lines = [event["data"] for event in events if event["event"] == "line"]
    assert len(lines) == 1
    line = lines[0]
    assert line["index"] == 0
    assert line["zh"] == chinese
    assert line["ja"] == japanese
    assert line["act"] == {}
    assert isinstance(line["emotion"], str) and line["emotion"]
    system_prompt = captured_messages[0][0].content
    assert '"lines"' in system_prompt

    audio = [event["data"] for event in events if event["event"] == "audio_chunk"]
    assert len(audio) >= 2
    assert all(item["line"] == 0 for item in audio)
    line_audio = [event["data"] for event in events if event["event"] == "line_audio"]
    assert line_audio == [{
        "line": 0,
        "status": "ready",
        "chunks": [
            {
                "index": item["index"],
                "line": 0,
                "audio_url": item["audio_url"],
                "sentence": item["sentence"],
            }
            for item in audio
        ],
    }]
    done = events[-1]
    assert done["event"] == "done"
    assert done["data"]["lines"] is None
    assert done["data"]["reply_format"] == "unknown"
    async with get_db(db_file) as conn:
        saved = (await crud.get_recent_messages(conn, f"hang_{case_id}"))[-1]
    assert saved.role == "assistant"
    assert saved.lines is None


_LEGACY_LINES = [
    {"index": 900, "zh": '你好，"小猫"。\\路径\n下一句😀', "ja": 'こんにちは、猫です。次です。😀',
     "emotion": "shy", "act": {"face": "smile", "motion": "nod", "blush": False}},
    {"zh": "再见。", "ja": "さようなら。", "emotion": "cool", "act": {"fx": None}},
]


@pytest.mark.asyncio
async def test_historical_legacy_message_displays_replays_and_regenerates_audio(tmp_path, monkeypatch):
    from galgame2voice.services.chat_service import ChatService

    monkeypatch.setenv("GALGAME2VOICE_PROJECT_ROOT", str(tmp_path))
    get_settings.cache_clear()
    db_file = tmp_path / "legacy-history.db"
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    ref_wav = tmp_path / "ref.wav"
    ref_wav.write_bytes(_make_pcm_wav(100))
    monkeypatch.setenv("GALGAME2VOICE_DB_PATH", str(db_file))
    await init_db(db_file)

    async with get_db(db_file) as conn:
        profile = await crud.create_voice_profile(conn, VoiceProfileCreate(
            name="历史角色",
            gpt_weights_path="models/m.ckpt",
            sovits_weights_path="models/m.pth",
            ref_audio_path=str(ref_wav),
            prompt_text="テスト",
            prompt_lang="ja",
            text_lang="ja",
            is_default=True,
        ))
        await crud.set_active_voice_profile(conn, profile.id)
        await crud.add_message(conn, MessageCreate(
            session_id="s_legacy_hist",
            role="user",
            content_chinese="以前的问候",
        ))
        legacy_msg = await crud.add_message(conn, MessageCreate(
            session_id="s_legacy_hist",
            role="assistant",
            content_chinese="旧版第一句。旧版第二句。",
            content_japanese="旧版の一文目。旧版の二文目。",
            audio_url="/audio/legacy-whole.wav",
            lines=None,
        ))

    captured_messages: list[list] = []

    async def stream_tokens(messages, *args, **kwargs):
        captured_messages.append(list(messages))
        yield json.dumps(
            {"tts": {"speed": 1.05, "temp": 0.95, "emotion": "gentle"}, "lines": _LEGACY_LINES},
            ensure_ascii=False,
        )

    engine = SimpleNamespace(
        current_refer_audio=None,
        current_gpt_weights=None,
        current_sovits_weights=None,
        synthesize=AsyncMock(return_value=_make_pcm_wav(180)),
    )
    scheduler = TtsScheduler()
    monkeypatch.setattr("galgame2voice.services.tts_service.get_tts_scheduler", lambda: scheduler)
    cache = TtsCacheManager(cache_dir=audio_dir / "cache", db_path=str(db_file))
    tts_service = TtsService(client=engine, audio_dir=audio_dir, cache_manager=cache, db_path=str(db_file))
    service = ChatService(db_path=db_file, tts_service=tts_service)
    service._get_active_llm_adapter = AsyncMock(
        return_value=(SimpleNamespace(stream_chat=stream_tokens, api_key="sk-test"), "mock-model", "deepseek")
    )
    service._extract_memory_safe = AsyncMock()

    app = FastAPI()
    app.include_router(chat_router.router)
    app.include_router(voice_router)
    chat_router.set_chat_service(service)
    app.dependency_overrides[get_voice_manager] = lambda: SimpleNamespace(tts_service=tts_service)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # 1. History API returns the legacy message with lines=None and original whole-text + audio_url
            hist_resp = await client.get("/api/chat/history", params={"session_id": "s_legacy_hist"})
            assert hist_resp.status_code == 200, hist_resp.text
            hist_msgs = hist_resp.json()["messages"]
            legacy_out = next(m for m in hist_msgs if m["id"] == legacy_msg.id)
            assert legacy_out["lines"] is None
            assert legacy_out["content_chinese"] == "旧版第一句。旧版第二句。"
            assert legacy_out["content_japanese"] == "旧版の一文目。旧版の二文目。"
            assert legacy_out["audio_url"] == "/audio/legacy-whole.wav"

            # 2. Default /api/chat/stream (no reply_format) replays legacy turn in lines format without rewriting DB row
            stream_resp = await client.post("/api/chat/stream", json={
                "prompt": "继续聊天",
                "session_id": "s_legacy_hist",
            })
            assert stream_resp.status_code == 200, stream_resp.text
            assistant_ctx = [m for m in captured_messages[0] if m.role == "assistant"]
            assert len(assistant_ctx) == 1
            replayed = json.loads(assistant_ctx[0].content)
            assert [item["zh"] for item in replayed["lines"]] == ["旧版第一句。", "旧版第二句。"]
            assert [item["ja"] for item in replayed["lines"]] == ["旧版の一文目。", "旧版の二文目。"]

            async with get_db(db_file) as conn:
                row_still_null = await (
                    await conn.execute("SELECT lines_json FROM messages WHERE id=?", (legacy_msg.id,))
                ).fetchone()
                assert row_still_null["lines_json"] is None

            # 3. Line audio regeneration on line 0 of the historical legacy message still works
            regen_resp = await client.post("/api/voice/regenerate-line", json={
                "session_id": "s_legacy_hist",
                "message_id": legacy_msg.id,
                "line_index": 0,
                "text": "旧版の一文目。旧版の二文目。",
                "voice_profile_id": profile.id,
            })
            assert regen_resp.status_code == 200, regen_resp.text
            regen_data = regen_resp.json()
            assert regen_data["persisted"] is True
            assert regen_data["audio"]["status"] == "ready"
            assert regen_data["audio"]["chunks"][0]["audio_url"].startswith("/audio/repair_")
    finally:
        chat_router.set_chat_service(None)
        await service.aclose()
        await scheduler.aclose()
        await cache.aclose()
        get_settings.cache_clear()
