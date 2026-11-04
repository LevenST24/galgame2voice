"""
Regression & proof tests for Telegram bot in-flight chat turn ownership vs /reset race.

When a user sends a chat message while an LLM reply is generating (non-blocking in ptb),
invoking /reset (or console action_reset) clears session messages and immediately acknowledges
"已清空当前对话上下文！".
If the in-flight or queued turns are not cancelled or invalidated, their subsequent completion will:
1. Send the stale assistant reply to Telegram after the reset ack.
2. Persist an orphaned assistant reply into the cleared session in SQLite.
3. Award character affection for the abandoned turn.
4. Schedule and send a background TTS voice note for the stale reply.

This test file verifies:
- Normal chat turn succeeds without reset.
- In-flight chat turn is cleanly prevented from leaking on /reset.
- Console action_reset cleanly invalidates in-flight turn without leaks.
- Subsequent chat turn after reset works cleanly and succeeds.
- Group chat members are strictly isolated (resetting member A does not affect member B).
- Queued chat turns waiting for the session lock are invalidated upon acquiring lock after reset.
- Uncooperative LLM adapter whose delayed reply arrives after reset is completely dropped.
- In-flight voice turn paused during STT is cancelled by /reset and immediately unblocks subsequent turn.
- Uncooperative STT returning empty or error after /reset does not emit stale notifications.
- Uncooperative LLM late error after /reset does not emit stale error notification.
- TelegramBotManager.stop() cancels in-flight voice STT tasks cleanly.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from galgame2voice.database import crud
from galgame2voice.database.models import VoiceProfileCreate
from galgame2voice.database.session import get_db
from galgame2voice.telegram_bot import TelegramBotHandlers
from telegram_test_support import TELEGRAM_TEST_ADMINS

ADMIN_ID = 12345
OTHER_ADMIN_ID = 67890
GROUP_CHAT_ID = -1001234567


class RecordingBot:
    """Stand-in for telegram.Bot capturing sent messages, voices, and actions."""

    def __init__(self):
        self.sent_messages: list[dict] = []
        self.sent_voices: list[dict] = []
        self.sent_actions: list[dict] = []

    async def send_chat_action(self, chat_id, action, **kwargs):
        self.sent_actions.append({"chat_id": chat_id, "action": action})
        return True

    async def send_message(self, chat_id, text, reply_markup=None, **kwargs):
        msg = {"chat_id": chat_id, "text": text, "reply_markup": reply_markup}
        self.sent_messages.append(msg)
        return msg

    async def send_voice(self, chat_id, voice, caption=None, **kwargs):
        v = {"chat_id": chat_id, "voice": voice, "caption": caption}
        self.sent_voices.append(v)
        return v


class FakeMessage:
    def __init__(self, text=None, voice=None):
        self.text = text
        self.voice = voice
        self.replies: list[str] = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)


class FakeQuery:
    def __init__(self, data):
        self.data = data
        self.answers: list[tuple] = []
        self.edited_text = None

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))

    async def edit_message_text(self, text, reply_markup=None):
        self.edited_text = text


def make_update(chat_id, user_id, text=None, voice=None):
    return SimpleNamespace(
        effective_chat=SimpleNamespace(id=chat_id),
        effective_user=SimpleNamespace(id=user_id),
        message=FakeMessage(text=text, voice=voice),
    )


def make_callback_update(data, chat_id, user_id):
    return SimpleNamespace(
        callback_query=FakeQuery(data),
        effective_chat=SimpleNamespace(id=chat_id),
        effective_user=SimpleNamespace(id=user_id),
        message=None,
    )


def make_context(bot=None):
    return SimpleNamespace(bot=bot or RecordingBot())


class PausableAdapter:
    """LLM adapter stand-in that pauses execution on an asyncio.Event."""

    def __init__(self, pause_event: asyncio.Event, release_event: asyncio.Event, reply_text: str = "这是旧回复"):
        self.pause_event = pause_event
        self.release_event = release_event
        self.reply_text = reply_text
        self.calls: list[list] = []

    async def chat(self, messages, model=None, **kwargs):
        self.calls.append(messages)
        self.pause_event.set()
        await self.release_event.wait()
        return SimpleNamespace(
            content=f'{{"chinese": "{self.reply_text}", "japanese": "これは返事です"}}'
        )


class UncooperativeAdapter:
    """LLM adapter stand-in that ignores cancellation and delivers a delayed reply."""

    def __init__(self, pause_event: asyncio.Event, reply_text: str = "迟到且不合作的回复"):
        self.pause_event = pause_event
        self.reply_text = reply_text
        self.released = False

    async def chat(self, messages, model=None, **kwargs):
        self.pause_event.set()
        while not self.released:
            try:
                await asyncio.sleep(0.02)
            except asyncio.CancelledError:
                # Deliberately ignore cancellation to simulate late uncooperative completion
                pass
        return SimpleNamespace(
            content=f'{{"chinese": "{self.reply_text}", "japanese": "遅れた返事です"}}'
        )


class RecordingTts:
    """TTS stand-in that records synthesis calls and returns dummy audio bytes."""

    def __init__(self):
        self.synthesized_texts: list[str] = []

    async def synthesize(self, text, options=None):
        self.synthesized_texts.append(text)
        return b"RIFF\x24\x08\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00\x80>\x00\x00\x00}\x00\x00\x02\x00\x10\x00data\x00\x08\x00\x00" + b"\x00" * 100


async def setup_test_character(db_path: str) -> int:
    """Creates an active character profile for testing."""
    async with get_db(db_path) as conn:
        await crud.get_settings_raw(conn)
        profile = await crud.create_voice_profile(
            conn,
            VoiceProfileCreate(
                name="夏目(Natsume)",
                system_prompt="你是夏目。",
                gpt_weights_path="natsume.ckpt",
                sovits_weights_path="natsume.pth",
                ref_audio_path="natsume.wav",
                prompt_text="こんにちは",
                prompt_lang="ja",
                text_lang="ja",
            ),
        )
        await crud.set_active_voice_profile(conn, profile.id)
        return profile.id


def _check_no_leaks(bot: RecordingBot, rows_final: list, final_affection: int, tts: RecordingTts, leak_needle: str = "旧回复") -> None:
    violations = []
    sent_texts = [m["text"] for m in bot.sent_messages]
    if any(leak_needle in text for text in sent_texts):
        violations.append(f"A. Stale assistant text was sent to Telegram: {sent_texts}")
    if rows_final != []:
        violations.append(f"B. Orphaned assistant message persisted into cleared session: {rows_final}")
    if final_affection != 0:
        violations.append(f"C. Affection points awarded for cancelled turn: {final_affection}")
    if tts.synthesized_texts != []:
        violations.append(f"D. TTS synthesized audio for cancelled turn: {tts.synthesized_texts}")
    if bot.sent_voices != []:
        violations.append(f"E. Voice note was sent to Telegram for cancelled turn: {bot.sent_voices}")

    assert not violations, "\n".join(["In-flight turn leaked after reset:"] + violations)


@pytest.mark.asyncio
async def test_normal_chat_turn_without_reset_succeeds(temp_db_path, monkeypatch):
    """Sanity test: in the absence of /reset, normal chat turn proceeds cleanly."""
    await setup_test_character(temp_db_path)
    handlers = TelegramBotHandlers(admin_ids=TELEGRAM_TEST_ADMINS, db_path=temp_db_path)

    pause = asyncio.Event()
    release = asyncio.Event()
    release.set()  # Not paused
    adapter = PausableAdapter(pause, release, reply_text="正常回复")
    handlers.chat_service.get_active_llm_adapter = AsyncMock(return_value=(adapter, "mock-model", "mock"))
    tts = RecordingTts()
    handlers.tts_service = tts
    monkeypatch.setattr("galgame2voice.telegram_bot.handlers.convert_wav_to_ogg", AsyncMock(return_value=b"OGG_AUDIO"))

    bot = RecordingBot()
    context = make_context(bot)

    task = await handlers.handle_text_message(make_update(ADMIN_ID, ADMIN_ID, text="你好"), context)
    if task and isinstance(task, asyncio.Task):
        await task

    # Text message was sent
    assert any("正常回复" in m["text"] for m in bot.sent_messages)

    # Database has user + assistant messages
    async with get_db(temp_db_path) as conn:
        rows = await (await conn.execute("SELECT role, content_chinese FROM messages WHERE session_id = ?", (f"tg_{ADMIN_ID}",))).fetchall()
    roles = [r[0] for r in rows]
    assert roles == ["user", "assistant"]


@pytest.mark.asyncio
async def test_reset_during_inflight_chat_turn_prevents_stale_assistant_reply(temp_db_path, monkeypatch):
    """
    Reproduction proof:
    1. Authorized user sends a text chat message; LLM chat starts and pauses.
    2. While LLM is generating, user invokes /reset.
    3. /reset acknowledges '已清空当前对话上下文！' and clears DB messages.
    4. The in-flight LLM call finishes.
    Expected contract:
    - No stale assistant text message should be sent to Telegram after reset ack.
    - No orphaned assistant message should be written into the cleared session in SQLite.
    - Affection points must not be incremented for the abandoned turn.
    - No background TTS synthesis or voice note should be sent for the abandoned turn.
    """
    await setup_test_character(temp_db_path)
    handlers = TelegramBotHandlers(admin_ids=TELEGRAM_TEST_ADMINS, db_path=temp_db_path)

    pause_llm = asyncio.Event()
    release_llm = asyncio.Event()
    adapter = PausableAdapter(pause_llm, release_llm, reply_text="旧回复内容")
    handlers.chat_service.get_active_llm_adapter = AsyncMock(return_value=(adapter, "mock-model", "mock"))
    tts = RecordingTts()
    handlers.tts_service = tts
    monkeypatch.setattr("galgame2voice.telegram_bot.handlers.convert_wav_to_ogg", AsyncMock(return_value=b"OGG_AUDIO"))

    bot = RecordingBot()
    context = make_context(bot)

    chat_task = None
    voice_task = None
    try:
        # 1. Start chat turn in background (mimicking ptb block=False dispatch)
        chat_task = asyncio.create_task(
            handlers.handle_text_message(make_update(ADMIN_ID, ADMIN_ID, text="你好，请介绍一下你自己"), context)
        )

        # Wait until LLM adapter is paused inside adapter.chat
        await asyncio.wait_for(pause_llm.wait(), timeout=2.0)
        assert len(adapter.calls) == 1

        # At this point, the user message has already been written to DB
        async with get_db(temp_db_path) as conn:
            rows_mid = await (await conn.execute("SELECT role FROM messages WHERE session_id = ?", (f"tg_{ADMIN_ID}",))).fetchall()
        assert [r[0] for r in rows_mid] == ["user"]

        # 2. While LLM is still running, user sends /reset
        reset_reply = await handlers.handle_reset(make_update(ADMIN_ID, ADMIN_ID, text="/reset"), context)
        assert "已清空当前对话上下文！" in reset_reply

        # Messages in DB are now cleared by /reset
        async with get_db(temp_db_path) as conn:
            rows_after_reset = await (await conn.execute("SELECT role FROM messages WHERE session_id = ?", (f"tg_{ADMIN_ID}",))).fetchall()
        assert rows_after_reset == []

        # 3. Release the in-flight LLM adapter
        release_llm.set()
        voice_task = await asyncio.wait_for(chat_task, timeout=2.0)
        if voice_task and isinstance(voice_task, asyncio.Task):
            try:
                await asyncio.wait_for(voice_task, timeout=2.0)
            except asyncio.CancelledError:
                pass

        # Inspect SQLite database state
        async with get_db(temp_db_path) as conn:
            rows_final = await (await conn.execute(
                "SELECT role, content_chinese FROM messages WHERE session_id = ?",
                (f"tg_{ADMIN_ID}",)
            )).fetchall()
            pts_row = await (await conn.execute(
                "SELECT COALESCE(SUM(affection_score), 0) FROM character_affection WHERE user_id = ?",
                (str(ADMIN_ID),)
            )).fetchone()
            final_affection = pts_row[0] if pts_row else 0

        _check_no_leaks(bot, rows_final, final_affection, tts, leak_needle="旧回复内容")
    finally:
        release_llm.set()
        if chat_task is not None and not chat_task.done():
            chat_task.cancel()
            try:
                await asyncio.wait_for(chat_task, timeout=1.0)
            except (asyncio.CancelledError, TimeoutError, Exception):
                pass
        if voice_task is not None and isinstance(voice_task, asyncio.Task) and not voice_task.done():
            voice_task.cancel()
            try:
                await asyncio.wait_for(voice_task, timeout=1.0)
            except (asyncio.CancelledError, TimeoutError, Exception):
                pass


@pytest.mark.asyncio
async def test_console_action_reset_prevents_stale_assistant_reply(temp_db_path, monkeypatch):
    """Verifies that console button action_reset also cleanly invalidates in-flight turn without leaks."""
    await setup_test_character(temp_db_path)
    handlers = TelegramBotHandlers(admin_ids=TELEGRAM_TEST_ADMINS, db_path=temp_db_path)

    pause_llm = asyncio.Event()
    release_llm = asyncio.Event()
    adapter = PausableAdapter(pause_llm, release_llm, reply_text="控制台重置前的旧回复")
    handlers.chat_service.get_active_llm_adapter = AsyncMock(return_value=(adapter, "mock-model", "mock"))
    tts = RecordingTts()
    handlers.tts_service = tts
    monkeypatch.setattr("galgame2voice.telegram_bot.handlers.convert_wav_to_ogg", AsyncMock(return_value=b"OGG_AUDIO"))

    bot = RecordingBot()
    context = make_context(bot)

    chat_task = None
    voice_task = None
    try:
        chat_task = asyncio.create_task(
            handlers.handle_text_message(make_update(ADMIN_ID, ADMIN_ID, text="测试控制台重置"), context)
        )

        await asyncio.wait_for(pause_llm.wait(), timeout=2.0)

        # Click action_reset via interactive console callback query
        cb_update = make_callback_update("action_reset", ADMIN_ID, ADMIN_ID)
        await handlers.handle_callback_query(cb_update, context)
        assert any("清空" in ans[0] for ans in cb_update.callback_query.answers)

        # Release LLM
        release_llm.set()
        voice_task = await asyncio.wait_for(chat_task, timeout=2.0)
        if voice_task and isinstance(voice_task, asyncio.Task):
            try:
                await asyncio.wait_for(voice_task, timeout=2.0)
            except asyncio.CancelledError:
                pass

        async with get_db(temp_db_path) as conn:
            rows_final = await (await conn.execute(
                "SELECT role, content_chinese FROM messages WHERE session_id = ?",
                (f"tg_{ADMIN_ID}",)
            )).fetchall()
            pts_row = await (await conn.execute(
                "SELECT COALESCE(SUM(affection_score), 0) FROM character_affection WHERE user_id = ?",
                (str(ADMIN_ID),)
            )).fetchone()
            final_affection = pts_row[0] if pts_row else 0

        _check_no_leaks(bot, rows_final, final_affection, tts, leak_needle="控制台重置前的旧回复")
    finally:
        release_llm.set()
        if chat_task is not None and not chat_task.done():
            chat_task.cancel()
            try:
                await asyncio.wait_for(chat_task, timeout=1.0)
            except (asyncio.CancelledError, TimeoutError, Exception):
                pass
        if voice_task is not None and isinstance(voice_task, asyncio.Task) and not voice_task.done():
            voice_task.cancel()
            try:
                await asyncio.wait_for(voice_task, timeout=1.0)
            except (asyncio.CancelledError, TimeoutError, Exception):
                pass


@pytest.mark.asyncio
async def test_chat_turn_after_reset_succeeds_normally(temp_db_path, monkeypatch):
    """Verifies that after /reset has invalidated an in-flight turn, a new chat turn succeeds end-to-end."""
    await setup_test_character(temp_db_path)
    handlers = TelegramBotHandlers(admin_ids=TELEGRAM_TEST_ADMINS, db_path=temp_db_path)

    pause_llm = asyncio.Event()
    release_llm = asyncio.Event()
    adapter = PausableAdapter(pause_llm, release_llm, reply_text="旧回复内容")
    handlers.chat_service.get_active_llm_adapter = AsyncMock(return_value=(adapter, "mock-model", "mock"))
    tts = RecordingTts()
    handlers.tts_service = tts
    monkeypatch.setattr("galgame2voice.telegram_bot.handlers.convert_wav_to_ogg", AsyncMock(return_value=b"OGG_AUDIO"))

    bot = RecordingBot()
    context = make_context(bot)

    chat_task = None
    old_voice_task = None
    new_chat_task = None
    try:
        # 1. In-flight turn paused
        chat_task = asyncio.create_task(
            handlers.handle_text_message(make_update(ADMIN_ID, ADMIN_ID, text="第一句"), context)
        )
        await asyncio.wait_for(pause_llm.wait(), timeout=2.0)

        # 2. Reset session
        await handlers.handle_reset(make_update(ADMIN_ID, ADMIN_ID, text="/reset"), context)
        release_llm.set()
        old_voice_task = await asyncio.wait_for(chat_task, timeout=2.0)
        if old_voice_task and isinstance(old_voice_task, asyncio.Task):
            await old_voice_task

        # 3. Now start a brand new turn
        adapter.reply_text = "新轮次成功回复"
        new_chat_task = await handlers.handle_text_message(make_update(ADMIN_ID, ADMIN_ID, text="第二句新消息"), context)
        if new_chat_task and isinstance(new_chat_task, asyncio.Task):
            await asyncio.wait_for(new_chat_task, timeout=2.0)

        # New reply is sent
        assert any("新轮次成功回复" in m["text"] for m in bot.sent_messages)

        # DB contains exactly 1 user + 1 assistant turn
        async with get_db(temp_db_path) as conn:
            rows = await (await conn.execute(
                "SELECT role, content_chinese FROM messages WHERE session_id = ?", (f"tg_{ADMIN_ID}",)
            )).fetchall()
        assert len(rows) == 2
        assert rows[0][0] == "user" and rows[0][1] == "第二句新消息"
        assert rows[1][0] == "assistant" and "新轮次成功回复" in rows[1][1]
    finally:
        release_llm.set()
        if chat_task is not None and not chat_task.done():
            chat_task.cancel()
            try:
                await asyncio.wait_for(chat_task, timeout=1.0)
            except (asyncio.CancelledError, TimeoutError, Exception):
                pass
        if old_voice_task is not None and isinstance(old_voice_task, asyncio.Task) and not old_voice_task.done():
            old_voice_task.cancel()
            try:
                await asyncio.wait_for(old_voice_task, timeout=1.0)
            except (asyncio.CancelledError, TimeoutError, Exception):
                pass
        if new_chat_task is not None and isinstance(new_chat_task, asyncio.Task) and not new_chat_task.done():
            new_chat_task.cancel()
            try:
                await asyncio.wait_for(new_chat_task, timeout=1.0)
            except (asyncio.CancelledError, TimeoutError, Exception):
                pass


@pytest.mark.asyncio
async def test_group_chat_members_are_isolated(temp_db_path, monkeypatch):
    """
    Verifies group chat isolation:
    Member A and Member B chat concurrently in the same group chat.
    Member A resets their session; Member A's turn is invalidated.
    Member B's turn completes successfully and is not cancelled or affected.
    """
    await setup_test_character(temp_db_path)
    handlers = TelegramBotHandlers(admin_ids=[ADMIN_ID, OTHER_ADMIN_ID], db_path=temp_db_path)

    pause_a = asyncio.Event()
    release_a = asyncio.Event()
    adapter_a = PausableAdapter(pause_a, release_a, reply_text="成员A被取消的回复")

    pause_b = asyncio.Event()
    release_b = asyncio.Event()
    adapter_b = PausableAdapter(pause_b, release_b, reply_text="成员B正常成功的回复")

    async def mock_get_adapter(conn=None):
        if len(adapter_a.calls) == 0:
            return adapter_a, "mock-model", "mock"
        return adapter_b, "mock-model", "mock"

    handlers.chat_service.get_active_llm_adapter = AsyncMock(side_effect=mock_get_adapter)
    tts = RecordingTts()
    handlers.tts_service = tts
    monkeypatch.setattr("galgame2voice.telegram_bot.handlers.convert_wav_to_ogg", AsyncMock(return_value=b"OGG_AUDIO"))

    bot = RecordingBot()
    context = make_context(bot)

    task_a = None
    task_b = None
    voice_a = None
    voice_b = None
    try:
        # Start turn A
        task_a = asyncio.create_task(
            handlers.handle_text_message(make_update(GROUP_CHAT_ID, ADMIN_ID, text="我是成员A"), context)
        )
        await asyncio.wait_for(pause_a.wait(), timeout=2.0)

        # Start turn B
        task_b = asyncio.create_task(
            handlers.handle_text_message(make_update(GROUP_CHAT_ID, OTHER_ADMIN_ID, text="我是成员B"), context)
        )
        await asyncio.wait_for(pause_b.wait(), timeout=2.0)

        # Member A invokes /reset
        await handlers.handle_reset(make_update(GROUP_CHAT_ID, ADMIN_ID, text="/reset"), context)

        # Release both LLMs
        release_a.set()
        release_b.set()

        voice_a = await asyncio.wait_for(task_a, timeout=2.0)
        if voice_a and isinstance(voice_a, asyncio.Task):
            await voice_a
        voice_b = await asyncio.wait_for(task_b, timeout=2.0)
        if voice_b and isinstance(voice_b, asyncio.Task):
            await asyncio.wait_for(voice_b, timeout=2.0)

        # Member A's reply leaked nothing
        sent_texts = [m["text"] for m in bot.sent_messages]
        assert not any("成员A被取消的回复" in text for text in sent_texts)

        # Member B's reply was successfully delivered
        assert any("成员B正常成功的回复" in text for text in sent_texts)

        # DB verification: Member A's session is empty, Member B has user + assistant
        async with get_db(temp_db_path) as conn:
            rows_a = await (await conn.execute(
                "SELECT role FROM messages WHERE session_id = ?", (f"tg_{GROUP_CHAT_ID}_{ADMIN_ID}",)
            )).fetchall()
            rows_b = await (await conn.execute(
                "SELECT role FROM messages WHERE session_id = ? ORDER BY id ASC", (f"tg_{GROUP_CHAT_ID}_{OTHER_ADMIN_ID}",)
            )).fetchall()
        assert rows_a == []
        assert [r[0] for r in rows_b] == ["user", "assistant"]
    finally:
        release_a.set()
        release_b.set()
        for t in (task_a, task_b, voice_a, voice_b):
            if t is not None and isinstance(t, asyncio.Task) and not t.done():
                t.cancel()
                try:
                    await asyncio.wait_for(t, timeout=1.0)
                except (asyncio.CancelledError, TimeoutError, Exception):
                    pass


@pytest.mark.asyncio
async def test_queued_chat_turn_is_invalidated_by_reset(temp_db_path, monkeypatch):
    """
    Verifies that a queued turn (waiting for _session_lock behind an in-flight turn)
    is invalidated by /reset and aborted upon lock acquisition without executing.
    """
    await setup_test_character(temp_db_path)
    handlers = TelegramBotHandlers(admin_ids=TELEGRAM_TEST_ADMINS, db_path=temp_db_path)

    pause_llm = asyncio.Event()
    release_llm = asyncio.Event()
    adapter = PausableAdapter(pause_llm, release_llm, reply_text="旧回复内容")
    handlers.chat_service.get_active_llm_adapter = AsyncMock(return_value=(adapter, "mock-model", "mock"))
    tts = RecordingTts()
    handlers.tts_service = tts
    monkeypatch.setattr("galgame2voice.telegram_bot.handlers.convert_wav_to_ogg", AsyncMock(return_value=b"OGG_AUDIO"))

    bot = RecordingBot()
    context = make_context(bot)

    task1 = None
    task2 = None
    try:
        # 1. Message 1 starts and acquires session lock
        task1 = asyncio.create_task(
            handlers.handle_text_message(make_update(ADMIN_ID, ADMIN_ID, text="第一句"), context)
        )
        await asyncio.wait_for(pause_llm.wait(), timeout=2.0)

        # 2. Message 2 arrives while Message 1 is running; Message 2 queues on _session_lock
        task2 = asyncio.create_task(
            handlers.handle_text_message(make_update(ADMIN_ID, ADMIN_ID, text="第二句排队中"), context)
        )
        # Give event loop a cycle so task2 is waiting on session lock
        await asyncio.sleep(0.05)

        # 3. User calls /reset before Message 1 finishes
        await handlers.handle_reset(make_update(ADMIN_ID, ADMIN_ID, text="/reset"), context)

        # 4. Release Message 1
        release_llm.set()
        res1, res2 = await asyncio.wait_for(asyncio.gather(task1, task2, return_exceptions=True), timeout=2.0)
        for r in (res1, res2):
            if isinstance(r, asyncio.Task):
                await r

        # Both Message 1 and Message 2 must not leak
        async with get_db(temp_db_path) as conn:
            rows_final = await (await conn.execute(
                "SELECT role, content_chinese FROM messages WHERE session_id = ?", (f"tg_{ADMIN_ID}",)
            )).fetchall()
            pts_row = await (await conn.execute(
                "SELECT COALESCE(SUM(affection_score), 0) FROM character_affection WHERE user_id = ?",
                (str(ADMIN_ID),)
            )).fetchone()
            final_affection = pts_row[0] if pts_row else 0

        _check_no_leaks(bot, rows_final, final_affection, tts, leak_needle="回复")
    finally:
        release_llm.set()
        for t in (task1, task2):
            if t is not None and isinstance(t, asyncio.Task) and not t.done():
                t.cancel()
                try:
                    await asyncio.wait_for(t, timeout=1.0)
                except (asyncio.CancelledError, TimeoutError, Exception):
                    pass


@pytest.mark.asyncio
async def test_uncooperative_llm_delayed_reply_does_not_leak(temp_db_path, monkeypatch):
    """
    Verifies that even if an LLM adapter is uncooperative (ignores CancelledError and returns late),
    the turn invalidation checks and require_user_turn prevent any Telegram send or DB write.
    """
    await setup_test_character(temp_db_path)
    handlers = TelegramBotHandlers(admin_ids=TELEGRAM_TEST_ADMINS, db_path=temp_db_path)

    pause_llm = asyncio.Event()
    adapter = UncooperativeAdapter(pause_llm, reply_text="不合作的迟到回复")
    handlers.chat_service.get_active_llm_adapter = AsyncMock(return_value=(adapter, "mock-model", "mock"))
    tts = RecordingTts()
    handlers.tts_service = tts
    monkeypatch.setattr("galgame2voice.telegram_bot.handlers.convert_wav_to_ogg", AsyncMock(return_value=b"OGG_AUDIO"))

    bot = RecordingBot()
    context = make_context(bot)

    chat_task = None
    delayed_voice = None
    try:
        chat_task = asyncio.create_task(
            handlers.handle_text_message(make_update(ADMIN_ID, ADMIN_ID, text="测试不合作LLM"), context)
        )
        await asyncio.wait_for(pause_llm.wait(), timeout=2.0)

        # Reset while uncooperative LLM is running
        await handlers.handle_reset(make_update(ADMIN_ID, ADMIN_ID, text="/reset"), context)

        # Now let uncooperative LLM deliver its delayed content
        adapter.released = True
        delayed_voice = await asyncio.wait_for(chat_task, timeout=2.0)
        if delayed_voice and isinstance(delayed_voice, asyncio.Task):
            await delayed_voice

        # Inspect SQLite database state
        async with get_db(temp_db_path) as conn:
            rows_final = await (await conn.execute(
                "SELECT role, content_chinese FROM messages WHERE session_id = ?", (f"tg_{ADMIN_ID}",)
            )).fetchall()
            pts_row = await (await conn.execute(
                "SELECT COALESCE(SUM(affection_score), 0) FROM character_affection WHERE user_id = ?",
                (str(ADMIN_ID),)
            )).fetchone()
            final_affection = pts_row[0] if pts_row else 0

        _check_no_leaks(bot, rows_final, final_affection, tts, leak_needle="不合作的迟到回复")
    finally:
        adapter.released = True
        if chat_task is not None and not chat_task.done():
            chat_task.cancel()
            try:
                await asyncio.wait_for(chat_task, timeout=1.0)
            except (asyncio.CancelledError, TimeoutError, Exception):
                pass
        if delayed_voice is not None and isinstance(delayed_voice, asyncio.Task) and not delayed_voice.done():
            delayed_voice.cancel()
            try:
                await asyncio.wait_for(delayed_voice, timeout=1.0)
            except (asyncio.CancelledError, TimeoutError, Exception):
                pass


class PausableSttAdapter:
    """STT adapter stand-in that pauses on an asyncio.Event and can simulate uncooperative behavior."""

    def __init__(self, pause_event: asyncio.Event, release_event: asyncio.Event | None = None, return_text: str = "语音识别结果", raise_exc: Exception | None = None):
        self.pause_event = pause_event
        self.release_event = release_event
        self.return_text = return_text
        self.raise_exc = raise_exc
        self.swallow_cancel = False
        self.calls: list[bytes] = []

    async def transcribe(self, audio_bytes, filename=None, **kwargs):
        self.calls.append(audio_bytes)
        self.pause_event.set()
        if self.release_event is not None:
            if self.swallow_cancel:
                while not self.release_event.is_set():
                    try:
                        await asyncio.sleep(0.02)
                    except asyncio.CancelledError:
                        pass
            else:
                await self.release_event.wait()
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.return_text


def setup_voice_stt_mocks(monkeypatch, handlers, stt_adapter):
    """Mocks bot.get_file, convert_ogg_to_wav and get_stt_adapter for deterministic voice tests."""
    monkeypatch.setattr("galgame2voice.telegram_bot.handlers.get_stt_adapter", lambda prov: stt_adapter)
    monkeypatch.setattr("galgame2voice.telegram_bot.handlers.convert_ogg_to_wav", AsyncMock(return_value=b"WAV_BYTES"))
    fake_tg_file = SimpleNamespace(download_as_bytearray=AsyncMock(return_value=bytearray(b"OGG_BYTES")))
    return fake_tg_file


@pytest.mark.asyncio
async def test_voice_turn_stt_paused_cancelled_by_reset_and_next_text_runs_immediately(temp_db_path, monkeypatch):
    """
    Verifies that when a voice note is paused in STT:
    1. /reset cancels the ongoing voice turn and releases the session lock immediately.
    2. A subsequent text message sent immediately acquires the lock and runs without waiting for old STT.
    """
    await setup_test_character(temp_db_path)
    handlers = TelegramBotHandlers(admin_ids=TELEGRAM_TEST_ADMINS, db_path=temp_db_path)

    pause_stt = asyncio.Event()
    release_stt = asyncio.Event()
    stt_adapter = PausableSttAdapter(pause_stt, release_stt, return_text="旧语音内容")
    fake_tg_file = setup_voice_stt_mocks(monkeypatch, handlers, stt_adapter)
    tts = RecordingTts()
    handlers.tts_service = tts
    monkeypatch.setattr("galgame2voice.telegram_bot.handlers.convert_wav_to_ogg", AsyncMock(return_value=b"OGG_AUDIO"))

    # Text adapter for the subsequent turn
    adapter = PausableAdapter(asyncio.Event(), asyncio.Event(), reply_text="新文本回复")
    adapter.pause_event.set()
    adapter.release_event.set()
    handlers.chat_service.get_active_llm_adapter = AsyncMock(return_value=(adapter, "mock-model", "mock"))

    bot = RecordingBot()
    bot.get_file = AsyncMock(return_value=fake_tg_file)
    context = make_context(bot)

    voice_task = None
    text_task = None
    returned_voice_task = None
    try:
        # 1. Start voice note
        voice_update = make_update(ADMIN_ID, ADMIN_ID, voice=SimpleNamespace(file_id="voice_123", file_size=1024))
        voice_task = asyncio.create_task(handlers.handle_voice_message(voice_update, context))

        # Wait until STT is paused inside transcribe
        await asyncio.wait_for(pause_stt.wait(), timeout=2.0)

        # 2. User invokes /reset while STT is running
        reset_reply = await handlers.handle_reset(make_update(ADMIN_ID, ADMIN_ID, text="/reset"), context)
        assert "已清空当前对话上下文！" in reset_reply

        # 3. User immediately sends a new text message
        # If the voice turn was registered as active and cancelled, _session_lock was released,
        # so the new text message MUST run and complete within 0.8s without needing release_stt!
        text_update = make_update(ADMIN_ID, ADMIN_ID, text="新问题")
        text_task = asyncio.create_task(handlers.handle_text_message(text_update, context))

        # Must complete fast without hanging on the unreleased STT
        done, pending = await asyncio.wait([text_task], timeout=0.8)
        assert text_task in done, "New text turn was blocked by old STT holding session lock after /reset!"

        # Await the text task to retrieve the spawned background voice task
        returned_voice_task = await text_task
        if returned_voice_task and isinstance(returned_voice_task, asyncio.Task):
            await asyncio.wait_for(returned_voice_task, timeout=2.0)

        # Ensure fresh text did complete and correct reply
        assert any("新文本回复" in m["text"] for m in bot.sent_messages)
        assert not any("旧语音内容" in m.get("text", "") for m in bot.sent_messages)
    finally:
        # Clean up voice task and all tasks in bounded finally paths
        release_stt.set()
        if voice_task is not None:
            if not voice_task.done():
                voice_task.cancel()
            try:
                await asyncio.wait_for(voice_task, timeout=1.0)
            except (asyncio.CancelledError, TimeoutError, Exception):
                pass
        if text_task is not None:
            if not text_task.done():
                text_task.cancel()
            try:
                t_res = await asyncio.wait_for(text_task, timeout=1.0)
                if returned_voice_task is None and isinstance(t_res, asyncio.Task):
                    returned_voice_task = t_res
            except (asyncio.CancelledError, TimeoutError, Exception):
                pass
        if returned_voice_task is not None and isinstance(returned_voice_task, asyncio.Task):
            if not returned_voice_task.done():
                returned_voice_task.cancel()
            try:
                await asyncio.wait_for(returned_voice_task, timeout=1.0)
            except (asyncio.CancelledError, TimeoutError, Exception):
                pass


@pytest.mark.asyncio
async def test_voice_turn_uncooperative_stt_empty_or_error_does_not_emit_stale_reply(temp_db_path, monkeypatch):
    """
    Verifies that if an uncooperative transcriber returns empty text or raises an error after /reset,
    no stale error message ('未能从语音中识别出有效内容' / '语音处理出错') is emitted to Telegram.
    """
    await setup_test_character(temp_db_path)
    handlers = TelegramBotHandlers(admin_ids=TELEGRAM_TEST_ADMINS, db_path=temp_db_path)

    pause_stt = asyncio.Event()
    release_stt = asyncio.Event()
    # Transcriber returns empty string
    stt_adapter = PausableSttAdapter(pause_stt, release_stt, return_text="")
    stt_adapter.swallow_cancel = True
    fake_tg_file = setup_voice_stt_mocks(monkeypatch, handlers, stt_adapter)
    tts = RecordingTts()
    handlers.tts_service = tts
    monkeypatch.setattr("galgame2voice.telegram_bot.handlers.convert_wav_to_ogg", AsyncMock(return_value=b"OGG_AUDIO"))

    bot = RecordingBot()
    bot.get_file = AsyncMock(return_value=fake_tg_file)
    context = make_context(bot)

    voice_update = make_update(ADMIN_ID, ADMIN_ID, voice=SimpleNamespace(file_id="voice_err", file_size=1024))
    voice_task = None
    try:
        voice_task = asyncio.create_task(handlers.handle_voice_message(voice_update, context))
        await asyncio.wait_for(pause_stt.wait(), timeout=2.0)

        # Reset
        await handlers.handle_reset(make_update(ADMIN_ID, ADMIN_ID, text="/reset"), context)

        # Release uncooperative transcriber (delivers empty text after reset)
        release_stt.set()
        await asyncio.wait_for(voice_task, timeout=2.0)

        # Verify no stale error reply was sent to user
        assert voice_update.message.replies == [], f"Stale error reply sent after reset: {voice_update.message.replies}"
    finally:
        release_stt.set()
        if voice_task is not None and not voice_task.done():
            voice_task.cancel()
            try:
                await asyncio.wait_for(voice_task, timeout=1.0)
            except (asyncio.CancelledError, TimeoutError, Exception):
                pass


@pytest.mark.asyncio
async def test_uncooperative_llm_late_exception_does_not_emit_stale_error_notification(temp_db_path, monkeypatch):
    """
    Verifies that if an LLM adapter is cancelled by /reset but raises a late runtime exception,
    the generic error branch does NOT emit a stale '大模型生成回复失败' notification.
    Contrasted with a normal turn where a runtime exception DOES emit the notification.
    """
    await setup_test_character(temp_db_path)
    handlers = TelegramBotHandlers(admin_ids=TELEGRAM_TEST_ADMINS, db_path=temp_db_path)
    tts = RecordingTts()
    handlers.tts_service = tts
    monkeypatch.setattr("galgame2voice.telegram_bot.handlers.convert_wav_to_ogg", AsyncMock(return_value=b"OGG_AUDIO"))

    # 1. Normal turn with genuine LLM failure: MUST emit error notification
    failing_adapter = AsyncMock()
    failing_adapter.chat = AsyncMock(side_effect=RuntimeError("Genuine LLM connection failure"))
    handlers.chat_service.get_active_llm_adapter = AsyncMock(return_value=(failing_adapter, "mock-model", "mock"))

    bot_normal = RecordingBot()
    context_normal = make_context(bot_normal)
    task_normal = await handlers.handle_text_message(make_update(ADMIN_ID, ADMIN_ID, text="测试真实错误"), context_normal)
    if task_normal and isinstance(task_normal, asyncio.Task):
        await task_normal
    assert any("大模型生成回复失败" in m["text"] for m in bot_normal.sent_messages), "Genuine failure should notify user!"

    # 2. Reset during LLM, and uncooperative LLM raises late exception: MUST NOT emit notification
    pause_llm = asyncio.Event()
    release_llm = asyncio.Event()

    class LateFailingAdapter:
        async def chat(self, messages, **kwargs):
            pause_llm.set()
            while not release_llm.is_set():
                try:
                    await asyncio.sleep(0.02)
                except asyncio.CancelledError:
                    pass
            raise RuntimeError("Late connection reset after turn cancelled")

    handlers.chat_service.get_active_llm_adapter = AsyncMock(return_value=(LateFailingAdapter(), "mock-model", "mock"))
    bot_reset = RecordingBot()
    context_reset = make_context(bot_reset)

    chat_task = None
    returned_task = None
    try:
        chat_task = asyncio.create_task(
            handlers.handle_text_message(make_update(ADMIN_ID, ADMIN_ID, text="测试重置后晚到错误"), context_reset)
        )
        await asyncio.wait_for(pause_llm.wait(), timeout=2.0)

        # Reset
        await handlers.handle_reset(make_update(ADMIN_ID, ADMIN_ID, text="/reset"), context_reset)

        # Release late failing adapter
        release_llm.set()
        returned_task = await asyncio.wait_for(chat_task, timeout=2.0)
        if returned_task and isinstance(returned_task, asyncio.Task):
            await asyncio.wait_for(returned_task, timeout=2.0)

        # Verify no stale failure notification was sent
        err_msgs = [m["text"] for m in bot_reset.sent_messages if "大模型生成回复失败" in m.get("text", "")]
        assert err_msgs == [], f"Stale error notification emitted after reset: {err_msgs}"
    finally:
        release_llm.set()
        if chat_task is not None and not chat_task.done():
            chat_task.cancel()
            try:
                await asyncio.wait_for(chat_task, timeout=1.0)
            except (asyncio.CancelledError, TimeoutError, Exception):
                pass
        if returned_task is not None and isinstance(returned_task, asyncio.Task) and not returned_task.done():
            returned_task.cancel()
            try:
                await asyncio.wait_for(returned_task, timeout=1.0)
            except (asyncio.CancelledError, TimeoutError, Exception):
                pass


@pytest.mark.asyncio
async def test_bot_stop_cancels_active_stt_task(temp_db_path, monkeypatch):
    """Verifies that TelegramBotManager.stop() cancels in-flight voice STT tasks."""
    await setup_test_character(temp_db_path)
    from galgame2voice.telegram_bot.bot import TelegramBotManager
    manager = TelegramBotManager(db_path=temp_db_path)
    handlers = manager.handlers
    handlers.admin_ids = set(TELEGRAM_TEST_ADMINS)
    tts = RecordingTts()
    handlers.tts_service = tts
    monkeypatch.setattr("galgame2voice.telegram_bot.handlers.convert_wav_to_ogg", AsyncMock(return_value=b"OGG_AUDIO"))

    pause_stt = asyncio.Event()
    release_stt = asyncio.Event()
    stt_adapter = PausableSttAdapter(pause_stt, release_stt, return_text="语音内容")
    fake_tg_file = setup_voice_stt_mocks(monkeypatch, handlers, stt_adapter)

    bot = RecordingBot()
    bot.get_file = AsyncMock(return_value=fake_tg_file)
    context = make_context(bot)

    manager.is_running = True
    voice_task = None
    try:
        voice_update = make_update(ADMIN_ID, ADMIN_ID, voice=SimpleNamespace(file_id="voice_stop", file_size=1024))
        voice_task = asyncio.create_task(handlers.handle_voice_message(voice_update, context))
        await asyncio.wait_for(pause_stt.wait(), timeout=2.0)

        # Manager stop must cancel the active STT task
        await manager.stop()
        assert voice_task.done(), "Active STT task was not cancelled/completed by manager.stop()!"
    finally:
        release_stt.set()
        if voice_task is not None:
            if not voice_task.done():
                voice_task.cancel()
            try:
                await asyncio.wait_for(voice_task, timeout=1.0)
            except (asyncio.CancelledError, TimeoutError, Exception):
                pass
