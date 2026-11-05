"""End-to-end turn reply and line audio repair roundtrip test.

Verifies the business contract across real HTTP endpoints, SQLite storage,
and browser state transitions. On the candidate implementation, this proves
the complete green path:
  stop -> pending -> release -> ready -> unique repair POST -> persisted true
  -> history reload -> valid RIFF/WAVE audio GET.
"""

import asyncio
import io
import json
import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock
import wave

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
import httpx
import pytest
import uvicorn

from galgame2voice.config import get_settings
from galgame2voice.database import crud
from galgame2voice.database.models import MessageCreate, VoiceProfileCreate
from galgame2voice.database.session import get_db, init_db
from galgame2voice.routers import chat, voice
from galgame2voice.routers.chat import get_chat_service, set_chat_service
from galgame2voice.services.audio_artifacts import REPAIR_AUDIO_PREFIX
from galgame2voice.services.chat_service import ChatService
from galgame2voice.services.tts_cache_manager import TtsCacheManager
from galgame2voice.services.tts_scheduler import TtsScheduler
from galgame2voice.services.tts_service import TtsService
from galgame2voice.services.voice_manager import get_voice_manager, set_voice_manager


def create_pcm_wav(amplitude: int = 100, duration_seconds: float = 4.0) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(8000)
        frames = int(8000 * duration_seconds)
        wav_file.writeframes(amplitude.to_bytes(2, "little", signed=True) * frames)
    return output.getvalue()


@pytest.mark.asyncio
async def test_turn_reply_repair_roundtrip_business_contract(tmp_path, monkeypatch):
    db_file = tmp_path / "roundtrip.db"
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("GALGAME2VOICE_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("GALGAME2VOICE_DB_PATH", str(db_file))
    get_settings.cache_clear()

    await init_db(str(db_file))

    # 1. Pre-seed SQLite with voice profile and 202 synthetic messages
    ref_wav = tmp_path / "ref.wav"
    ref_wav.write_bytes(create_pcm_wav(100))

    session_id = "turn_roundtrip_test"
    async with get_db(str(db_file)) as conn:
        profile = await crud.create_voice_profile(
            conn,
            VoiceProfileCreate(
                name="测试音色",
                gpt_weights_path="models/synthetic.ckpt",
                sovits_weights_path="models/synthetic.pth",
                ref_audio_path=str(ref_wav),
                prompt_text="こんにちは",
                prompt_lang="ja",
            ),
        )
        await crud.set_active_voice_profile(conn, profile.id)

        await crud.upsert_session(
            conn,
            session_id=session_id,
            title="端到端轮次会话",
            voice_profile_id=profile.id,
            settings_json=json.dumps({"voiceProfileId": profile.id}),
            update_voice_binding=True,
        )

        for i in range(1, 203):
            role = "user" if i % 2 == 1 else "assistant"
            lines = None
            if role == "assistant":
                lines = [{
                    "index": 0,
                    "zh": f"历史中文{i}。",
                    "ja": f"歴史日本語{i}。",
                    "emotion": "neutral",
                    "act": {},
                    "audio": {
                        "status": "ready",
                        "voice_profile_id": profile.id,
                        "voice_name": "测试音色",
                        "generated_at": 1000000 + i,
                        "chunks": [{"index": 0, "line": 0, "audio_url": f"/audio/hist_{i}.wav", "sentence": f"歴史日本語{i}。"}],
                    },
                }]
            await crud.add_message(
                conn,
                MessageCreate(
                    session_id=session_id,
                    role=role,
                    content_chinese=f"历史中文{i}。",
                    content_japanese=f"歴史日本語{i}。" if role == "assistant" else "",
                    lines=lines,
                ),
            )

    # Independent new connection reads baseline for historical 202 messages across all columns
    query_baseline = """
        SELECT id, session_id, role, content_chinese, content_japanese, audio_url, latency_ms, lines_json, reply_to_message_id
        FROM messages
        WHERE session_id = ?
        ORDER BY id ASC
    """
    async with get_db(str(db_file)) as conn:
        cursor = await conn.execute(query_baseline, (session_id,))
        baseline_rows = [dict(r) for r in await cursor.fetchall()]
    assert len(baseline_rows) == 202

    # 2. Nullable resource references for comprehensive finally cleanup
    sock = None
    server = None
    server_task = None
    chat_tts_scheduler = None
    chat_tts_cache = None
    chat_service = None
    manager = None
    proc = None
    node_result = None
    chat_service_installed = False
    manager_installed = False

    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    stream_hang = asyncio.Event()
    chat_tts_hang = asyncio.Event()

    async def fake_stream_tokens(*args, **kwargs):
        try:
            first_line = {
                "zh": "第一句中文回复。",
                "ja": "最初の日本語返答です。",
                "emotion": "neutral",
                "act": {},
            }
            # Output in legal lines format so line_scanner immediately parses and emits line 0
            payload = '{"lines": [' + json.dumps(first_line, ensure_ascii=False)
            yield payload + "\n"
            await asyncio.wait_for(stream_hang.wait(), timeout=10.0)
        finally:
            cleanup_started.set()
            await asyncio.wait_for(release_cleanup.wait(), timeout=10.0)

    try:
        # Owned scheduler & cache with patched consumption getters
        chat_tts_scheduler = TtsScheduler()
        chat_tts_cache = TtsCacheManager(cache_dir=cache_dir, db_path=str(db_file))

        monkeypatch.setattr("galgame2voice.services.tts_service.get_tts_scheduler", lambda: chat_tts_scheduler)
        monkeypatch.setattr("galgame2voice.services.chat_pipelines.stream_coordinator.get_tts_scheduler", lambda: chat_tts_scheduler)
        monkeypatch.setattr("galgame2voice.services.tts_scheduler.get_tts_scheduler", lambda: chat_tts_scheduler)

        engine = SimpleNamespace(
            calls=[],
            current_refer_audio=None,
            current_gpt_weights=None,
            current_sovits_weights=None,
        )

        async def fake_engine_synthesize(text, options=None):
            options = options or {}
            # If this is chat stream generation, keep voice pending until cancelled
            if options.get("_priority", 1) > 0 and options.get("filename_prefix") != REPAIR_AUDIO_PREFIX:
                await asyncio.wait_for(chat_tts_hang.wait(), timeout=10.0)
            return create_pcm_wav(200)

        engine.synthesize = fake_engine_synthesize

        tts_service = TtsService(
            client=engine,
            audio_dir=audio_dir,
            cache_manager=chat_tts_cache,
            db_path=str(db_file),
        )

        chat_service = ChatService(db_path=str(db_file), tts_service=tts_service)
        chat_service._get_active_llm_adapter = AsyncMock(return_value=(
            SimpleNamespace(stream_chat=fake_stream_tokens, api_key="synthetic"),
            "test-model",
            "custom",
        ))
        chat_service._extract_memory_safe = AsyncMock()

        manager = SimpleNamespace(
            tts_service=tts_service,
            switch_lock=asyncio.Lock(),
            is_active_profile=lambda prof, force=False: True,
            switch_profile=AsyncMock(return_value=True),
            switching_profile_id=None,
            current_profile=profile,
            get_profile=AsyncMock(return_value=profile),
        )
        set_voice_manager(manager)
        manager_installed = True

        # 3. Assemble FastAPI application
        app = FastAPI()
        app.include_router(chat.router)
        app.include_router(voice.router)
        app.mount("/audio", StaticFiles(directory=str(audio_dir)), name="audio")

        app.dependency_overrides[get_voice_manager] = lambda: manager
        app.dependency_overrides[get_chat_service] = lambda: chat_service
        set_chat_service(chat_service)
        chat_service_installed = True

        @app.post("/__test/release-cleanup")
        async def release_cleanup_route():
            release_cleanup.set()
            return {"released": True}

        # 4. Bind socket directly and pass to uvicorn.Server.serve(sockets=[sock])
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

        config = uvicorn.Config(app=app, host="127.0.0.1", port=port, log_level="warning")
        server = uvicorn.Server(config=config)
        server_task = asyncio.create_task(server.serve(sockets=[sock]))

        for _ in range(50):
            if server.started:
                break
            await asyncio.sleep(0.05)
        assert server.started, "Uvicorn server failed to start within 2.5s"

        # 5. Launch Node driver with shielded communication
        proc = await asyncio.create_subprocess_exec(
            "node",
            "tests/helpers/turn_reply_roundtrip.mjs",
            str(port),
            session_id,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        comm_task = asyncio.create_task(proc.communicate())
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(asyncio.shield(comm_task), timeout=45.0)
        except asyncio.TimeoutError:
            proc.kill()
            stdout_bytes, stderr_bytes = await comm_task
            out_s = stdout_bytes.decode(errors="replace")
            err_s = stderr_bytes.decode(errors="replace")
            raise RuntimeError(f"Node driver timed out after 45s.\nSTDOUT:\n{out_s}\nSTDERR:\n{err_s}") from None

        stdout_text = stdout_bytes.decode("utf-8", errors="replace")
        stderr_text = stderr_bytes.decode("utf-8", errors="replace")

        for line in stdout_text.splitlines():
            if line.startswith("__ROUNDTRIP_RESULT__"):
                node_result = json.loads(line.removeprefix("__ROUNDTRIP_RESULT__"))
                break

        assert node_result is not None, f"Node driver did not emit result JSON.\nSTDERR:\n{stderr_text}\nSTDOUT:\n{stdout_text}"

        # 6. Verify SQLite direct state with a fresh connection
        async with get_db(str(db_file)) as conn:
            cur_count = await conn.execute(
                "SELECT count(*) FROM messages WHERE session_id = ?",
                (session_id,),
            )
            (total_count,) = await cur_count.fetchone()
            assert total_count == 204, f"Expected 204 messages in DB, got {total_count}"

            cur_msgs = await conn.execute(query_baseline, (session_id,))
            all_messages = [dict(r) for r in await cur_msgs.fetchall()]

        assert len(all_messages) == 204

        # Historical messages 1..202: row-by-row exact match against baseline across all columns
        for i in range(202):
            assert all_messages[i] == baseline_rows[i], (
                f"Historical message {i + 1} changed from baseline:\n"
                f"Expected: {baseline_rows[i]}\nActual: {all_messages[i]}"
            )

        user_row = all_messages[202]
        assistant_row = all_messages[203]

        assert user_row["id"] == 203
        assert user_row["role"] == "user"
        assert user_row["content_chinese"] == "测试提问"

        assert assistant_row["id"] == 204
        assert assistant_row["role"] == "assistant"
        assert assistant_row["reply_to_message_id"] == 203

        db_lines = json.loads(assistant_row["lines_json"] or "[]")
        assert len(db_lines) >= 1
        db_audio_url = db_lines[0]["audio"]["chunks"][0]["audio_url"]
        assert db_audio_url.startswith("/audio/repair_")

        # Verify synthesized WAV file on disk (1 file corresponding to db_audio_url)
        audio_filename = db_audio_url.removeprefix("/audio/")
        audio_filepath = audio_dir / audio_filename
        assert audio_filepath.exists(), f"Audio file {audio_filepath} does not exist on disk"
        assert audio_filepath.stat().st_size > 44, "Audio file is too small to be a valid WAV"

        # 7. Green release chain assertions
        assert node_result["oldNullPostBranchTaken"] is False, "Candidate incorrectly executed old null POST fallback"
        assert node_result["repairPostCount"] == 1, f"Expected exactly 1 repair POST, got {node_result['repairPostCount']}"
        assert node_result["releaseCleanupCalled"] is True, "release-cleanup was not triggered"

        events_order = node_result["eventsOrder"]
        assert "stop" in events_order
        assert "pending" in events_order
        assert "release" in events_order
        assert "ready" in events_order
        assert "repair_post" in events_order
        assert "persisted_true" in events_order

        idx_stop = events_order.index("stop")
        idx_pending = events_order.index("pending")
        idx_release = events_order.index("release")
        idx_ready = events_order.index("ready")
        idx_post = events_order.index("repair_post")
        idx_persisted = events_order.index("persisted_true")
        assert idx_stop < idx_pending < idx_release < idx_ready < idx_post < idx_persisted, (
            f"Release chain sequence mismatch: {events_order}"
        )

        turn_events = node_result["turnReplyEvents"]
        pending_event = next((e for e in turn_events if e.get("status") == "pending"), None)
        ready_event = next((e for e in turn_events if e.get("status") == "ready"), None)
        assert pending_event is not None, f"No pending status event found in {turn_events}"
        assert ready_event is not None, f"No ready status event found in {turn_events}"

        assert pending_event["session_id"] == session_id
        assert pending_event["user_message_id"] == 203
        assert ready_event["session_id"] == session_id
        assert ready_event["user_message_id"] == 203

        # 8. Same reply & refresh assertions
        assert node_result["initialLoadedCount"] == 100
        assert len(node_result["historyCalls"]) == 2, f"Expected 2 history calls, got {node_result['historyCalls']}"
        assert all("limit=100" in url for url in node_result["historyCalls"]), "History calls did not include limit=100"

        # 4-way ID equality (reloadedServerMessageId must directly be positive int, without fallback)
        ready_id = ready_event["message_id"]
        post_id = node_result["repairRequestMessageId"]
        sqlite_id = assistant_row["id"]
        reloaded_id = node_result["reloadedServerMessageId"]

        assert isinstance(reloaded_id, int) and reloaded_id > 0, (
            f"reloadedServerMessageId must be a positive integer without fallback, got: {reloaded_id} (diagnostic: {node_result.get('diagnosticMsgId')})"
        )
        assert ready_id == post_id == sqlite_id == reloaded_id == 204, (
            f"ID mismatch: ready={ready_id}, post={post_id}, sqlite={sqlite_id}, reloaded={reloaded_id}"
        )

        # 3 URL + 1 disk file equality
        repair_resp_url = node_result["repairAudioUrl"]
        reloaded_url = node_result["reloadedAudioUrl"]
        assert repair_resp_url == reloaded_url == db_audio_url, (
            f"Audio URL mismatch: resp={repair_resp_url}, reloaded={reloaded_url}, db={db_audio_url}"
        )
        assert not node_result["audioLostAfterReload"]

        # Real HTTP GET on audio URL with RIFF/WAVE header validation
        assert node_result["audioGetStatus"] == 200
        assert node_result["audioIsRiffWave"] is True

        async with httpx.AsyncClient() as http_client:
            wav_resp = await http_client.get(f"http://127.0.0.1:{port}{db_audio_url}")
            assert wav_resp.status_code == 200
            assert wav_resp.content.startswith(b"RIFF")
            assert b"WAVE" in wav_resp.content[:12]

    finally:
        cleanup_errors = []
        release_cleanup.set()
        stream_hang.set()
        chat_tts_hang.set()

        if proc and proc.returncode is None:
            try:
                proc.kill()
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except Exception as e:
                cleanup_errors.append(f"proc: {e}")

        if server:
            server.should_exit = True
        if server_task:
            try:
                await asyncio.wait_for(server_task, timeout=3.0)
            except Exception as e:
                cleanup_errors.append(f"server_task: {e}")

        if sock:
            try:
                sock.close()
            except Exception as e:
                cleanup_errors.append(f"sock: {e}")

        if chat_tts_scheduler:
            try:
                await chat_tts_scheduler.aclose()
            except Exception as e:
                cleanup_errors.append(f"chat_tts_scheduler: {e}")

        if chat_tts_cache:
            try:
                await chat_tts_cache.aclose()
            except Exception as e:
                cleanup_errors.append(f"chat_tts_cache: {e}")

        if chat_service:
            try:
                await chat_service.aclose()
            except Exception as e:
                cleanup_errors.append(f"chat_service: {e}")

        if chat_service_installed:
            try:
                set_chat_service(None)
            except Exception as e:
                cleanup_errors.append(f"chat_service_reset: {e}")

        if manager_installed:
            try:
                set_voice_manager(None)
            except Exception as e:
                cleanup_errors.append(f"manager_reset: {e}")

        if cleanup_errors:
            raise RuntimeError(f"Cleanup errors occurred: {cleanup_errors}")
