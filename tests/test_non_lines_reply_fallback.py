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
    assert not any(e["event"] in {"line", "line_audio"} for e in events)
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
