"""Every persona prompt carries the per-line bilingual reply format the voice pipeline depends on."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from galgame2voice.database import crud
from galgame2voice.database.models import VoiceProfileCreate
from galgame2voice.database.session import get_db, init_db
from galgame2voice.routers.chat import ChatRequest
from galgame2voice.services.chat_pipelines.line_scanner import LineScanner
from galgame2voice.services.chat_pipelines.reply_contract import render_lines_contract
from galgame2voice.services.chat_service import ChatService
from galgame2voice.services.streaming_parser import StreamingBilingualParser

PACKAGED_PROMPT = (
    "你是夏目。\n【输出契约与情绪控制】\n"
    "重要：你必须严格输出如下 JSON 格式，不要输出任何多余文字：\n"
    '{"tts": {"speed": 1.0, "temp": 0.9, "emotion": "gentle"}, '
    '"chinese": "显示给玩家的中文台词", "japanese": "对应的口语化日文台词"}'
)


def test_the_contract_example_is_a_reply_the_parser_and_scanner_accept():
    contract = render_lines_contract(None)
    example = contract.splitlines()[1]
    parser = StreamingBilingualParser()
    scanner = LineScanner()
    parser.feed_chunk(example)
    lines = scanner.feed(example)
    parser.finalize()
    result = scanner.finish()
    assert result.format == "lines"
    assert len(lines) == 1
    assert lines[0].zh == "给玩家看的中文台词"
    assert lines[0].ja == "意思相同的口语化日语台词"
    assert lines[0].emotion == "gentle"
    assert parser.tts_emotion == "gentle"


@pytest.mark.asyncio
async def test_hand_written_personas_and_legacy_packaged_prompts_receive_lines_contract(tmp_path):
    db_file = tmp_path / "contract.db"
    await init_db(db_file)
    async with get_db(str(db_file)) as conn:
        def profile(name, prompt):
            return VoiceProfileCreate(
                name=name, gpt_weights_path=f"{name}.ckpt", sovits_weights_path=f"{name}.pth",
                ref_audio_path=f"{name}.wav", prompt_text="テスト", prompt_lang="ja", text_lang="ja",
                system_prompt=prompt,
            )

        custom = await crud.create_voice_profile(conn, profile("爱丽丝", "你是爱丽丝，说话温柔。"))
        packaged = await crud.create_voice_profile(conn, profile("夏目", PACKAGED_PROMPT))
        service = ChatService(db_path=str(db_file))
        expected_contract = render_lines_contract(None)

        for prof in (custom, packaged):
            session = await crud.upsert_session(conn=conn, session_id=f"s{prof.id}", title="t", voice_profile_id=prof.id)
            for kwargs in ({}, {"reply_format": "old"}):
                messages = await service._prepare_messages(
                    conn=conn, session_id=session.id, user_prompt="你好", session=session, **kwargs
                )
                system = messages[0].content
                assert messages[0].role == "system"
                assert prof.system_prompt.split("\n")[0] in system
                assert expected_contract in system, prof.name
                assert system.count("【输出格式】") == 1, prof.name
                assert '"chinese"' not in system, prof.name


_LINES = [
    {"index": 900, "zh": '你好，"小猫"。\\路径\n下一句😀', "ja": 'こんにちは、猫です。次です。😀',
     "emotion": "shy", "act": {"face": "smile", "motion": "nod", "blush": False}},
    {"zh": "再见。", "ja": "さようなら。", "emotion": "cool", "act": {"fx": None}},
]


def _reply(lines=_LINES, *, ascii=False):
    return json.dumps({"tts": {"speed": 1.05, "temp": 0.95, "emotion": "gentle"}, "lines": lines}, ensure_ascii=ascii)


def _parse_sse_frames(raw: str) -> list[dict]:
    frames = []
    for block in raw.split("\n\n"):
        block = block.strip()
        if not block or block.startswith(":"):
            continue
        ev_type = "message"
        data_lines = []
        for line in block.splitlines():
            if line.startswith("event:"):
                ev_type = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:"):].strip())
        if data_lines:
            frames.append({"event": ev_type, "data": json.loads("\n".join(data_lines))})
    return frames


def test_request_format_defaults_and_validation():
    assert ChatRequest(prompt="hi").reply_format == "lines"
    assert ChatRequest(prompt="hi", reply_format="old").reply_format == "old"
    assert ChatRequest(prompt="hi", reply_format="lines").reply_format == "lines"
    with pytest.raises(ValidationError):
        ChatRequest(prompt="hi", reply_format="invalid")


@pytest.mark.asyncio
async def test_explicit_old_reply_format_uses_lines_contract_and_parser_without_422(tmp_path, monkeypatch):
    from httpx import ASGITransport, AsyncClient
    from galgame2voice import config
    from galgame2voice.main import create_app
    from galgame2voice.routers.chat import set_chat_service

    db_file = tmp_path / "explicit-old.db"
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    monkeypatch.setenv("GALGAME2VOICE_DB_PATH", str(db_file))
    await init_db(db_file)

    captured_messages: list[list] = []

    async def stream_tokens(messages, *args, **kwargs):
        captured_messages.append(list(messages))
        yield _reply(_LINES)

    wav_path = audio_dir / "line.wav"
    wav_path.write_bytes(b"RIFF\x24\x00\x00\x00WAVEfmt ")
    tts = SimpleNamespace(
        audio_dir=audio_dir,
        synthesize_to_file=AsyncMock(return_value=("/audio/line.wav", wav_path, {})),
    )
    service = ChatService(db_path=db_file, tts_service=tts)
    service._get_active_llm_adapter = AsyncMock(
        return_value=(SimpleNamespace(stream_chat=stream_tokens, api_key="sk-test"), "mock-model", "deepseek")
    )
    service._extract_memory_safe = AsyncMock()
    # Keep the data directory the database was seeded in. Without DPAPI (Linux, macOS) the seeded console token is
    # encrypted with the master key file in that directory, and decrypting it elsewhere raises MasterKeyError.
    data_dir = config.get_settings().data_dir
    monkeypatch.setattr(config, "get_settings", lambda: SimpleNamespace(audio_dir=audio_dir, data_dir=data_dir))
    app = create_app()
    set_chat_service(service)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8080") as client:
            resp = await client.post("/api/chat/stream", json={
                "prompt": "旧标签页发来的请求",
                "session_id": "s_old_tab",
                "reply_format": "old",
            })
            assert resp.status_code == 200, resp.text
            events = _parse_sse_frames(resp.text)
            assert len(captured_messages) == 1
            sys_prompt = captured_messages[0][0].content
            assert '"lines":' in sys_prompt
            assert '"chinese":' not in sys_prompt
            line_events = [e["data"] for e in events if e["event"] == "line"]
            assert [item["zh"] for item in line_events] == [item["zh"] for item in _LINES]
            done = next(e["data"] for e in events if e["event"] == "done")
            assert done["reply_format"] == "lines"
            assert [item["zh"] for item in done["lines"]] == [item["zh"] for item in _LINES]

            # Second turn with explicit "old" also replays assistant history in lines JSON
            resp2 = await client.post("/api/chat/stream", json={
                "prompt": "第二轮请求",
                "session_id": "s_old_tab",
                "reply_format": "old",
            })
            assert resp2.status_code == 200, resp2.text
            assistant_ctx = [m for m in captured_messages[1] if m.role == "assistant"]
            assert len(assistant_ctx) == 1
            replayed = json.loads(assistant_ctx[0].content)
            assert "lines" in replayed and "chinese" not in replayed
    finally:
        set_chat_service(None)
        await service.aclose()
