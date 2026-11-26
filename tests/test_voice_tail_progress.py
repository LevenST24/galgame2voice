"""Once the reply text is final, a stalled voice job must not hide that fact.

The stream sends one ``progress`` frame with stage ``voice`` after the last
line, while that line's audio is still being made, so the client can open the
input box early. A reply that fails in the model sends no such frame.
"""

import asyncio
import json
import re

from httpx import ASGITransport, AsyncClient

from galgame2voice.adapters.base import parse_sse_lines
from galgame2voice.database.session import init_db
from galgame2voice.main import create_app
from galgame2voice.routers.chat import set_chat_service
from galgame2voice.services.chat_service import ChatService

VOICE = {"stage": "voice"}


def _sse(payload: dict) -> str:
    return "data: " + json.dumps(payload, ensure_ascii=False)


class _TwoLines:
    def __init__(self, *, fail_after_first: bool = False) -> None:
        self.api_key = "synthetic"
        self.fail_after_first = fail_after_first

    async def stream_chat(self, messages, model=None, **kwargs):
        async def lines():
            first = '{"lines": [{"zh": "第一句", "ja": "一つ目", "emotion": "neutral"},'
            yield _sse({"choices": [{"delta": {"content": first}}]})
            yield ""
            if self.fail_after_first:
                raise RuntimeError("model went away")
            second = ' {"zh": "第二句", "ja": "二つ目", "emotion": "neutral"}]}'
            yield _sse({"choices": [{"delta": {"content": second}}]})
            yield ""
            yield "data: [DONE]"

        async for token in parse_sse_lines(lines()):
            yield token


class _SlowLastLineTts:
    """Speaks the first line at once and takes a while over every later one."""

    def __init__(self, directory, delay: float = 1.0) -> None:
        self.audio_dir = directory
        self.delay = delay
        self.calls = 0

    @property
    def directory(self):
        return self.audio_dir

    async def synthesize_to_file(self, text, options=None, filename_prefix="chunk"):
        self.calls += 1
        if self.calls > 1:
            await asyncio.sleep(self.delay)
        path = self.audio_dir / f"{filename_prefix}.wav"
        path.write_bytes(b"")
        return f"/audio/{filename_prefix}.wav", path, 32


def _adapter(stand_in):
    async def resolve(*args, **kwargs):
        return stand_in, "synthetic-model", "deepseek"

    return resolve


def _parse(body: str) -> list[tuple[str, dict | None]]:
    events = []
    for block in re.split(r"\r?\n\r?\n", body):
        name, data = "message", None
        for line in block.splitlines():
            if line.startswith("event:"):
                name = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data = json.loads(line.split(":", 1)[1].strip())
        if data is not None:
            events.append((name, data))
    return events


async def _stream(tmp_path, stand_in, tts) -> list[tuple[str, dict | None]]:
    database = tmp_path / "voice-tail.db"
    await init_db(database)
    service = ChatService(tts_service=tts, db_path=database)
    service._get_active_llm_adapter = _adapter(stand_in)
    set_chat_service(service)
    try:
        transport = ASGITransport(app=create_app())
        async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8080", timeout=30) as client:
            response = await client.post("/api/chat/stream", json={
                "prompt": "说两句",
                "session_id": "voice-tail",
                "reply_format": "lines",
            })
        assert response.status_code == 200, response.text
        return _parse(response.text)
    finally:
        set_chat_service(None)
        await service.aclose()


def _position(events, name, predicate=lambda data: True) -> int:
    for index, (event_name, data) in enumerate(events):
        if event_name == name and predicate(data):
            return index
    return -1


async def test_voice_frame_follows_the_last_line_while_its_audio_is_still_pending(tmp_path):
    events = await _stream(tmp_path, _TwoLines(), _SlowLastLineTts(tmp_path))
    names = [name for name, _ in events]

    voice = [index for index, (name, data) in enumerate(events) if name == "progress" and data == VOICE]
    assert len(voice) == 1, "exactly one voice frame per reply: " + repr(names)
    at = voice[0]
    assert [name for name, _ in events[:at]].count("line") == 2, (
        "the frame comes only after every line is published: " + repr(names)
    )
    second_audio = _position(events, "line_audio", lambda data: data.get("line") == 1)
    assert second_audio > at, "the second line's audio must still be pending at the frame: " + repr(names)
    assert _position(events, "done") > at, names


async def test_a_model_failure_sends_no_voice_frame(tmp_path):
    events = await _stream(tmp_path, _TwoLines(fail_after_first=True), _SlowLastLineTts(tmp_path, delay=0))
    names = [name for name, _ in events]
    assert "error" in names, names
    assert VOICE not in [data for name, data in events if name == "progress"], names
