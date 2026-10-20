"""Pose inheritance through real reply paths and bounded stored-line lookup."""

import asyncio
import importlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from galgame2voice.database import crud
from galgame2voice.database.models import MessageCreate
from galgame2voice.database.session import get_db, init_db
from galgame2voice.services.chat_pipelines.acting_menu import get_recent_pose, sanitize_act
from galgame2voice.services.chat_pipelines.context_builder import BuiltContext
from galgame2voice.services.chat_service import ChatService
from galgame2voice.telegram_bot.handlers import TelegramBotHandlers


MENU = {
    "faces": [], "poses": ["a", "b"], "default_pose": "a",
    "look_faces": {"a": ["a-only"], "b": ["b-only"]}, "blush": False,
}


@pytest.mark.asyncio
async def test_get_recent_pose_in_memory_has_no_stored_history():
    assert await get_recent_pose(":memory:", "pose") is None


def line(act):
    return {"index": 0, "zh": "你好。", "ja": "こんにちは。", "emotion": "gentle",
            "act": act, "audio": {"status": "skipped", "chunks": []}}


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "stream", "telegram"])
@pytest.mark.parametrize("case", ["within_reply", "across_turns", "no_stored_pose", "unavailable_stored_pose"])
async def test_reply_carries_pose(tmp_path, monkeypatch, mode, case):
    database = tmp_path / "pose.db"
    await init_db(database)
    session_id = "tg_4242" if mode == "telegram" else "pose"
    stored_pose = {"across_turns": "b", "unavailable_stored_pose": "retired"}.get(case)
    if stored_pose:
        async with get_db(database) as conn:
            await crud.add_message(conn, MessageCreate(
                session_id=session_id, role="assistant", content_chinese="以前。",
                lines=[line({"pose": "a"}), line({"pose": stored_pose})],
            ))

    acts = ([{"pose": "b"}, {"face": "b-only"}] if case == "within_reply"
            else [{"face": "b-only"}, {"face": "a-only"}])
    expected = ([{"pose": "b"}, {"face": "b-only"}] if case == "within_reply"
                else [{"face": "b-only"}, {}] if case == "across_turns"
                else [{}, {"face": "a-only"}])
    raw = json.dumps({"lines": [line(act) for act in acts]}, ensure_ascii=False)
    module = importlib.import_module({
        "sync": "galgame2voice.services.chat_service",
        "stream": "galgame2voice.services.chat_pipelines.stream_coordinator",
        "telegram": "galgame2voice.telegram_bot.handlers",
    }[mode])
    lookup = AsyncMock(wraps=get_recent_pose)
    sanitize = Mock(wraps=sanitize_act)
    monkeypatch.setattr(module, "get_recent_pose", lookup)
    monkeypatch.setattr(module, "sanitize_act", sanitize)

    def before_reply():
        lookup.assert_awaited_once_with(str(database), session_id)
        sanitize.assert_not_called()

    async def chat(*args, **kwargs):
        before_reply()
        return SimpleNamespace(content=raw)

    async def tokens(*args, **kwargs):
        before_reply()
        # Publish separate lines while the provider is still streaming.
        for item in raw:
            yield item
            await asyncio.sleep(0)

    tts = SimpleNamespace(audio_dir=tmp_path, synthesize_to_file=AsyncMock(
        return_value=("/audio/test.wav", tmp_path / "test.wav", {}),
    ))
    service = ChatService(db_path=database, tts_service=tts)
    service._get_active_llm_adapter = AsyncMock(return_value=(
        SimpleNamespace(chat=chat, stream_chat=tokens), "test", "custom",
    ))
    service._extract_memory_safe = AsyncMock()
    context = BuiltContext([], menu=MENU)
    monkeypatch.setattr("galgame2voice.services.chat_service.build_chat_context", AsyncMock(return_value=context))
    try:
        if mode == "telegram":
            service.prepare_messages = AsyncMock(return_value=context)
            service.memory_service.process_user_message = AsyncMock()
            service.affection_service.handle_turn_affection = AsyncMock()
            handlers = TelegramBotHandlers(chat_service=service, db_path=str(database))
            monkeypatch.setattr(handlers, "_spawn_background_voice_task", lambda *a, **k: asyncio.create_task(asyncio.sleep(0)))
            bot = SimpleNamespace(send_message=AsyncMock(), send_chat_action=AsyncMock())
            await (await handlers.process_text_chat(4242, "问题", bot))
            assert bot.send_message.call_args.kwargs["text"] == "你好。你好。"
        elif mode == "sync":
            result = await service.chat_sync("问题", session_id=session_id, reply_format="lines")
            assert [item["act"] for item in result["lines"]] == expected
        else:
            events = [event async for event in service.stream_chat("问题", session_id=session_id, reply_format="lines")]
            assert events[-1]["event"] == "done"
            result = events[-1]["data"]
            assert [event["data"]["act"] for event in events if event["event"] == "line"] == expected

        lookup.assert_awaited_once()
        # Also check the explicit None contract: fallback alone already worked before R11.
        assert [call.kwargs["current_pose"] for call in sanitize.call_args_list] == [
            stored_pose, "b" if case == "within_reply" else stored_pose,
        ]
        async with get_db(database) as conn:
            saved = (await crud.get_recent_messages(conn, session_id))[-1]
        assert [item["act"] for item in saved.lines] == expected
        if mode != "telegram":
            assert saved.lines == result["lines"]
    finally:
        await service.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(("case", "messages", "expected"), [
    ("newest_message", [("assistant", [line({"pose": "a"})]),
                        ("assistant", [line({"pose": "b"})])], "b"),
    ("last_line", [("assistant", [line({"pose": "a"}), line({"pose": "b"}), line({})])], "b"),
    ("skip_missing_lines", [("assistant", [line({"pose": "b"})]), ("assistant", None), ("assistant", [])], "b"),
    ("skip_non_assistant", [("assistant", [line({"pose": "b"})]), ("user", [line({"pose": "a"})])], "b"),
    ("no_pose", [("assistant", [line({})])], None),
    ("bounded", [("assistant", [line({"pose": "b"})])] + [("assistant", None)] * 20, None),
])
async def test_get_recent_pose(tmp_path, monkeypatch, case, messages, expected):
    database = tmp_path / "history.db"
    await init_db(database)
    async with get_db(database) as conn:
        for role, lines in messages:
            await crud.add_message(conn, MessageCreate(session_id="pose", role=role, content_chinese=case, lines=lines))
        await crud.add_message(conn, MessageCreate(
            session_id="other", role="assistant", content_chinese="其他。", lines=[line({"pose": "other"})],
        ))
    recent = AsyncMock(wraps=crud.get_recent_messages)
    monkeypatch.setattr(crud, "get_recent_messages", recent)
    assert await get_recent_pose(database, "pose") == expected
    recent.assert_awaited_once()
    assert recent.call_args.args[1] == "pose"
    assert recent.call_args.kwargs == {"limit": 20}
