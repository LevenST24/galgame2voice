"""Reasoning chunks must keep a live reply moving without entering the dialogue.

A stand-in model streams OpenAI reasoning deltas and Anthropic thinking deltas
for longer than one progress interval, then the visible reply. The HTTP chat
route is the same one the browser calls.
"""

import asyncio
import json
import re

from httpx import ASGITransport, AsyncClient

from galgame2voice.adapters.base import parse_sse_lines
from galgame2voice.database.session import get_db, init_db
from galgame2voice.main import create_app
from galgame2voice.routers.chat import set_chat_service
from galgame2voice.services.chat_service import ChatService

# Longer than the few-second progress throttle and the 5s keep-alive, far below the 120s client idle limit.
_THINKING_GAP_SECONDS = 5.3

SECRET = "先想很久不可见"
REPLY_ZH = "想完了"
REPLY_JA = "はい"
SESSION_ID = "reasoning-progress"


def _sse(payload: dict) -> str:
    return "data: " + json.dumps(payload, ensure_ascii=False)


def _parse_sse(body: str) -> list[tuple[str, dict | None]]:
    events = []
    for block in re.split(r"\r?\n\r?\n", body):
        if not block.strip():
            continue
        if block.lstrip().startswith(":"):
            events.append((":keep-alive", None))
            continue
        name = "message"
        data = None
        for line in block.splitlines():
            if line.startswith("event:"):
                name = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data = json.loads(line.split(":", 1)[1].strip())
        events.append((name, data))
    return events


class _ReasoningThenReply:
    """Streams real provider chunks through the production SSE parser."""

    def __init__(self) -> None:
        self.api_key = "synthetic"
        self.reasoning_effort = "high"
        self.chunk_count = 0
        self.spoken: list[str] = []

    async def stream_chat(self, messages, model=None, **kwargs):
        async def lines():
            kinds = ("reasoning_content", "reasoning", "anthropic")
            for burst in range(2):
                for index in range(6):
                    self.chunk_count += 1
                    text = f"{SECRET}-{burst}-{index}"
                    kind = kinds[(burst * 6 + index) % 3]
                    if kind == "anthropic":
                        yield _sse({
                            "type": "content_block_delta",
                            "delta": {"type": "thinking_delta", "thinking": text},
                        })
                    elif kind == "reasoning":
                        yield _sse({"choices": [{"delta": {"reasoning": text}}]})
                    else:
                        yield _sse({"choices": [{"delta": {"reasoning_content": text, "content": None}}]})
                    yield ""
                if burst == 0:
                    await asyncio.sleep(_THINKING_GAP_SECONDS)
            body = json.dumps(
                {"lines": [{"zh": REPLY_ZH, "ja": REPLY_JA, "emotion": "neutral"}]},
                ensure_ascii=False,
            )
            yield _sse({"choices": [{"delta": {"content": body}}]})
            yield ""
            yield "data: [DONE]"

        async for token in parse_sse_lines(lines()):
            assert SECRET not in token
            yield token


class _SilentTts:
    def __init__(self, directory) -> None:
        self.audio_dir = directory
        self.spoken: list[str] = []

    async def synthesize_to_file(self, text, options=None, filename_prefix="chunk"):
        self.spoken.append(text)
        path = self.directory / f"{filename_prefix}.wav"
        path.write_bytes(b"")
        return f"/audio/{filename_prefix}.wav", path, 32

    @property
    def directory(self):
        return self.audio_dir


async def test_reasoning_chunks_emit_throttled_progress_and_stay_out_of_the_reply(tmp_path):
    database = tmp_path / "reasoning.db"
    await init_db(database)
    stand_in = _ReasoningThenReply()
    tts = _SilentTts(tmp_path)
    service = ChatService(tts_service=tts, db_path=database)
    service._get_active_llm_adapter = _adapter(stand_in)
    set_chat_service(service)
    app = create_app()
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test", timeout=30) as client:
            response = await client.post("/api/chat/stream", json={
                "prompt": "这句会想很久",
                "session_id": SESSION_ID,
                "reply_format": "lines",
            })
        assert response.status_code == 200, response.text
        assert response.headers["X-G2V-Progress-Policy"] == "1"
        body = response.text
        assert SECRET not in body
        events = _parse_sse(body)
        progress = [data for name, data in events if name == "progress"]
        assert stand_in.chunk_count >= 8
        assert 2 <= len(progress) < stand_in.chunk_count, (
            "思考分片期间应节流发出进度，而不是每个分片一次或只有保活："
            + repr([name for name, _data in events])
        )
        assert progress == [{"stage": "thinking"}] * len(progress)
        lines = [data for name, data in events if name == "line"]
        assert lines and all(SECRET not in json.dumps(line, ensure_ascii=False) for line in lines)
        assert lines[0]["zh"] == REPLY_ZH
        assert lines[0]["ja"] == REPLY_JA
        done = [data for name, data in events if name == "done"]
        assert done and done[-1]["chinese"] == REPLY_ZH and done[-1]["japanese"] == REPLY_JA
        assert all(SECRET not in text for text in tts.spoken)
        async with get_db(database) as conn:
            rows = await (await conn.execute(
                "SELECT role, content_chinese, content_japanese, lines_json FROM messages WHERE session_id=? ORDER BY id",
                (SESSION_ID,),
            )).fetchall()
        stored = "\n".join("" if value is None else str(value) for row in rows for value in row)
        assert SECRET not in stored
        assert any(row[0] == "assistant" and row[1] == REPLY_ZH and row[2] == REPLY_JA for row in rows)
    finally:
        set_chat_service(None)
        await service.aclose()


def _adapter(stand_in):
    async def resolve(*args, **kwargs):
        return stand_in, "deepseek-reasoner", "deepseek"

    return resolve
