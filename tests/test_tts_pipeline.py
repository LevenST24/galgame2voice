"""Regression tests for TtsStreamPipeline and StreamCoordinator handling of vocal sentences."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest

from galgame2voice.database import crud
from galgame2voice.database.models import MessageCreate
from galgame2voice.database.session import get_db, init_db
from galgame2voice.services.chat_pipelines.stream_coordinator import StreamCoordinator
from galgame2voice.services.chat_pipelines.tts_pipeline import TtsStreamPipeline


def test_is_vocal_sentence_action_cues_and_punctuation():
    """Assert is_vocal_sentence correctly rejects stage directions with punctuation,

    while accepting actual dialogue and furigana text.
    """
    # Non-vocal: stage cues with punctuation / ellipses / dashes
    assert TtsStreamPipeline.is_vocal_sentence("（ため息）……") is False
    assert TtsStreamPipeline.is_vocal_sentence("*sighs* ...") is False
    assert TtsStreamPipeline.is_vocal_sentence("[小声] ——") is False
    assert TtsStreamPipeline.is_vocal_sentence("【沈黙】……") is False
    assert TtsStreamPipeline.is_vocal_sentence("（微笑み）……（悲しそうに）……") is False
    assert TtsStreamPipeline.is_vocal_sentence("（小声で）「……」") is False
    assert TtsStreamPipeline.is_vocal_sentence("（笑）……") is False
    assert TtsStreamPipeline.is_vocal_sentence("（汗）...") is False
    assert TtsStreamPipeline.is_vocal_sentence("（怒）!?") is False
    assert TtsStreamPipeline.is_vocal_sentence("（（ため息））……") is False
    assert TtsStreamPipeline.is_vocal_sentence("（ため息）") is False
    assert TtsStreamPipeline.is_vocal_sentence("……") is False
    assert TtsStreamPipeline.is_vocal_sentence("") is False
    assert TtsStreamPipeline.is_vocal_sentence("   ") is False
    assert TtsStreamPipeline.is_vocal_sentence(None) is False

    # Vocal: actual dialogue with stage cues, furigana, or pure text
    assert TtsStreamPipeline.is_vocal_sentence("（微笑む）おはよう……") is True
    assert TtsStreamPipeline.is_vocal_sentence("一花（いちか）") is True
    assert TtsStreamPipeline.is_vocal_sentence("（微笑んで）こんにちは！") is True
    assert TtsStreamPipeline.is_vocal_sentence("こんにちは") is True
    assert TtsStreamPipeline.is_vocal_sentence("*whispers* hello") is True
    assert TtsStreamPipeline.is_vocal_sentence("（小声で）「おはよう」") is True


async def _run_streaming_turn(
    tmp_path: Path,
    *,
    db_name: str,
    session_id: str,
    prompt: str,
    token_chunks: tuple[str, ...],
    mock_synthesize: AsyncMock,
    emotion: str,
) -> tuple[Path, list[dict]]:
    database = tmp_path / db_name
    await init_db(database)
    async with get_db(database) as conn:
        user_msg = await crud.add_message(
            conn,
            MessageCreate(session_id=session_id, role="user", content_chinese=prompt),
        )

    async def tokens(*args, **kwargs):
        for chunk in token_chunks:
            yield chunk

    tts = SimpleNamespace(
        audio_dir=tmp_path,
        synthesize_to_file=mock_synthesize,
    )

    coord = StreamCoordinator(
        adapter=SimpleNamespace(stream_chat=tokens, api_key="synthetic_key"),
        messages=[],
        model_name="test_model",
        actual_provider_id="synthetic_provider",
        session_id=session_id,
        prompt=prompt,
        user_msg=user_msg,
        tts_service=tts,
        db_path=str(database),
        metrics_collector=SimpleNamespace(record_chat_turn=AsyncMock(return_value={})),
        affection_service=SimpleNamespace(handle_turn_affection=AsyncMock(return_value={"emotion": emotion})),
        concat_wav_fn=lambda *args: True,
        cancel_event=None,
        reply_format="lines",
    )

    stream = coord.stream()
    events = []
    try:
        while True:
            try:
                ev = await asyncio.wait_for(stream.__anext__(), timeout=3.0)
                events.append(ev)
            except StopAsyncIteration:
                break
    finally:
        await asyncio.wait_for(stream.aclose(), timeout=3.0)

    return database, events


@pytest.mark.asyncio
async def test_streaming_pipeline_skips_stage_direction_with_punctuation(tmp_path: Path):
    """When a line contains only stage directions followed by punctuation (e.g. （ため息）……),

    the streaming coordinator must treat it as non-vocal, not dispatch tts_service.synthesize_to_file,
    and record status='skipped' rather than 'failed'.
    """
    mock_synthesize = AsyncMock(side_effect=ValueError("Text is empty after cleaning stage directions"))
    database, events = await _run_streaming_turn(
        tmp_path,
        db_name="test_vocal.db",
        session_id="vocal_session",
        prompt="……",
        token_chunks=('{"lines": [{"zh": "（叹气）……", "ja": "（ため息）……", "emotion": "sad", "act": {}}]}',),
        mock_synthesize=mock_synthesize,
        emotion="sad",
    )

    # 1. Synthesize must NOT be called for non-vocal stage direction with ellipsis
    assert mock_synthesize.call_count == 0, f"synthesize_to_file was unexpectedly called {mock_synthesize.call_count} times"

    # 2. line_audio SSE event must report skipped status
    line_audio_events = [e for e in events if e.get("event") == "line_audio"]
    assert len(line_audio_events) == 1
    assert line_audio_events[0]["data"]["line"] == 0
    assert line_audio_events[0]["data"]["status"] == "skipped"
    assert line_audio_events[0]["data"]["chunks"] == []

    # 3. Done event must report skipped status, not failed
    done_events = [e for e in events if e.get("event") == "done"]
    assert len(done_events) == 1
    lines = done_events[0]["data"]["lines"]
    assert len(lines) == 1
    assert lines[0]["audio"]["status"] == "skipped", f"Expected status 'skipped', got {lines[0]['audio']['status']}"

    # 4. Database message line must also be 'skipped'
    async with get_db(database) as conn:
        messages = await crud.get_recent_messages(conn, "vocal_session")
        assistant_msgs = [m for m in messages if m.role == "assistant"]
        assert len(assistant_msgs) == 1
        line0 = assistant_msgs[0].lines[0]
        assert line0["audio"]["status"] == "skipped"


@pytest.mark.asyncio
async def test_streaming_pipeline_mixed_vocal_and_non_vocal_lines(tmp_path: Path):
    """When a turn has both non-vocal stage directions and vocal dialogue,

    non-vocal lines are skipped while vocal lines are synthesized in proper sequence.
    """
    mock_synthesize = AsyncMock(return_value=("/audio/cache/line1.wav", tmp_path / "line1.wav", 512))
    database, events = await _run_streaming_turn(
        tmp_path,
        db_name="test_mixed.db",
        session_id="mixed_session",
        prompt="你好",
        token_chunks=(
            '{"lines": [',
            '{"zh": "（叹气）……", "ja": "（ため息）……", "emotion": "sad", "act": {}},',
            '{"zh": "早上好。", "ja": "（微笑む）おはよう。", "emotion": "happy", "act": {}}',
            "]}",
        ),
        mock_synthesize=mock_synthesize,
        emotion="happy",
    )

    # 1. Synthesize must be called ONLY once (for line 1: （微笑む）おはよう。)
    assert mock_synthesize.call_count == 1
    call_args = mock_synthesize.call_args[0]
    assert call_args[0] == "（微笑む）おはよう。"

    # 2. line_audio SSE events: line 0 skipped, line 1 ready
    line_audio_events = [e for e in events if e.get("event") == "line_audio"]
    assert len(line_audio_events) == 2
    assert line_audio_events[0]["data"]["line"] == 0
    assert line_audio_events[0]["data"]["status"] == "skipped"
    assert line_audio_events[1]["data"]["line"] == 1
    assert line_audio_events[1]["data"]["status"] == "ready"

    # 3. Done event lines status
    done_events = [e for e in events if e.get("event") == "done"]
    assert len(done_events) == 1
    lines = done_events[0]["data"]["lines"]
    assert len(lines) == 2
    assert lines[0]["audio"]["status"] == "skipped"
    assert lines[1]["audio"]["status"] == "ready"

    # 4. Database persisted lines status
    async with get_db(database) as conn:
        messages = await crud.get_recent_messages(conn, "mixed_session")
        assistant_msgs = [m for m in messages if m.role == "assistant"]
        assert len(assistant_msgs) == 1
        stored_lines = assistant_msgs[0].lines
        assert len(stored_lines) == 2
        assert stored_lines[0]["audio"]["status"] == "skipped"
        assert stored_lines[1]["audio"]["status"] == "ready"


def test_is_vocal_sentence_rejects_kaomoji_urls_and_laugh_w():
    """Kaomoji containing Greek omega or underscores, bare URLs, and laugh w's must not be vocal."""
    assert TtsStreamPipeline.is_vocal_sentence("(´・ω・｀)") is False
    assert TtsStreamPipeline.is_vocal_sentence("(T_T)") is False
    assert TtsStreamPipeline.is_vocal_sentence("(≧ω≦)") is False
    assert TtsStreamPipeline.is_vocal_sentence("´・ω・｀") is False
    assert TtsStreamPipeline.is_vocal_sentence("___") is False
    assert TtsStreamPipeline.is_vocal_sentence("https://example.com/guide") is False
    assert TtsStreamPipeline.is_vocal_sentence("www") is False
    assert TtsStreamPipeline.is_vocal_sentence("ｗｗｗ") is False
    assert TtsStreamPipeline.is_vocal_sentence("ごめんね (´・ω・｀)") is True


def test_is_vocal_sentence_accepts_fullwidth_alnum_zero_and_inner_voice_monologue():
    assert TtsStreamPipeline.is_vocal_sentence("ＯＫ！") is True
    assert TtsStreamPipeline.is_vocal_sentence("１２３！") is True
    assert TtsStreamPipeline.is_vocal_sentence("〇") is True
    assert TtsStreamPipeline.is_vocal_sentence("【心の声：顔が熱い……！】") is True
    assert TtsStreamPipeline.is_vocal_sentence("www～") is False
