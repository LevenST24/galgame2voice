"""
Test suite for startup performance and latency optimizations.
"""

import asyncio
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
import pytest

from galgame2voice.database import crud
from galgame2voice.database.models import MessageCreate
from galgame2voice.services.gpt_sovits_client import GptSovitsClient
from galgame2voice.services.voice_manager import VoiceManager
from galgame2voice.database.session import get_db, init_db
from galgame2voice.main import create_app


def test_index_html_no_blocking_google_fonts():
    html_path = Path(__file__).resolve().parent.parent / "galgame2voice" / "static" / "index.html"
    assert html_path.exists()
    content = html_path.read_text(encoding="utf-8")
    assert "fonts.googleapis.com" not in content, "External Google Fonts block page load in offline/China environments"
    assert "fonts.gstatic.com" not in content


@pytest.mark.asyncio
async def test_switch_voice_profile_skips_redundant_weight_reloads():
    mock_server = MagicMock()
    mock_server.handle_request = AsyncMock()
    client = GptSovitsClient(server=mock_server)

    profile1 = {
        "name": "char1",
        "gpt_weights_path": "weights/gpt1.ckpt",
        "sovits_weights_path": "weights/sovits1.pth",
        "ref_audio_path": "audio/ref1.wav",
        "prompt_text": "hello",
        "prompt_lang": "ja",
    }

    resp_mock = MagicMock()
    resp_mock.status_code = 200
    mock_server.handle_request.return_value = resp_mock

    # First switch: should call set_gpt_weights, set_sovits_weights, set_refer_audio
    ok1 = await client.switch_voice_profile(profile1)
    assert ok1 is True
    assert mock_server.handle_request.call_count == 3

    # Switch again to identical profile: should skip all 3 calls!
    mock_server.handle_request.reset_mock()
    ok2 = await client.switch_voice_profile(profile1)
    assert ok2 is True
    assert mock_server.handle_request.call_count == 0, "Identical profile switch should skip all network calls"

    # Switch with force=True: should execute calls even if identical
    mock_server.handle_request.reset_mock()
    ok3 = await client.switch_voice_profile(profile1, force=True)
    assert ok3 is True
    assert mock_server.handle_request.call_count == 3, "Force=True should execute all weight loads"


@pytest.mark.asyncio
async def test_voice_manager_switch_profile_forwards_force():
    mock_client = MagicMock(spec=GptSovitsClient)
    mock_client.switch_voice_profile = AsyncMock(return_value=True)
    mock_client.lock = asyncio.Lock()
    mock_client.active_profile = None  # an instance attribute, so spec= leaves it out
    vm = VoiceManager()
    vm.client = mock_client

    profile = {
        "id": 1,
        "name": "char1",
        "gpt_weights_path": "weights/gpt1.ckpt",
        "sovits_weights_path": "weights/sovits1.pth",
        "ref_audio_path": "audio/ref1.wav",
        "prompt_text": "hello",
        "prompt_lang": "ja",
    }

    ok = await vm.switch_profile(profile, persist=False, force=True)
    assert ok is True
    mock_client.switch_voice_profile.assert_called_once_with(profile, force=True)


@pytest.mark.asyncio
async def test_lifespan_queues_default_warmup_without_claiming_models_loaded(tmp_path, monkeypatch, mock_gpt_sovits):
    from galgame2voice.config import get_settings
    from galgame2voice.database import crud
    from galgame2voice.main import lifespan
    from galgame2voice.services import gpt_sovits_client, voice_manager
    from galgame2voice.services.character_manager import CharacterManager

    # Use real Settings so every imported getter resolves the same isolated root.
    # Patching config.get_settings alone leaves main.get_settings pointing at the
    # original getter and can make this test migrate the user's real database.
    monkeypatch.setenv("GALGAME2VOICE_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("GALGAME2VOICE_LOG_TO_FILE", "0")
    monkeypatch.setenv("GALGAME2VOICE_HOST", "127.0.0.1")
    monkeypatch.setenv("GALGAME2VOICE_GPT_SOVITS_BASE_URL", "http://127.0.0.1:9880")
    get_settings.cache_clear()
    settings = get_settings()
    monkeypatch.setenv("GALGAME2VOICE_DB_PATH", str(settings.db_path))
    monkeypatch.setenv("GALGAME_DB_PATH", str(settings.db_path))
    monkeypatch.setattr(CharacterManager, "_instance", None)
    await init_db(settings.db_path)
    from galgame2voice.database.models import VoiceProfileCreate
    async with get_db(settings.db_path) as conn:
        await crud.create_voice_profile(conn, VoiceProfileCreate(
            name="Startup test voice", gpt_weights_path="test.ckpt",
            sovits_weights_path="test.pth", is_default=True,
        ))

    client = GptSovitsClient(server=mock_gpt_sovits)
    vm = VoiceManager(gpt_sovits_client_or_server=client, db_path=str(settings.db_path))
    monkeypatch.setattr(gpt_sovits_client, "_global_gpt_sovits_client", client)
    monkeypatch.setattr(voice_manager, "_global_voice_manager", vm)
    monkeypatch.setattr(vm, "warmup_current_profile", AsyncMock())
    async with get_db(settings.db_path) as conn:
        expected_profile = await crud.get_active_voice_profile(conn)
    assert expected_profile is not None, "test database must contain a seeded voice"

    async with lifespan(create_app()):
        await asyncio.sleep(0)
        vm.warmup_current_profile.assert_awaited_once()
        assert vm.active_profile is None
        assert client.current_gpt_weights is None
        assert client.current_sovits_weights is None
        async with get_db(settings.db_path) as conn:
            saved_profile = await crud.get_active_voice_profile(conn)
        assert saved_profile.id == expected_profile.id


async def test_lifespan_drains_resources_when_serving_context_raises(monkeypatch):
    import galgame2voice.main as main_module

    monkeypatch.setattr(main_module, "_init_logging_and_safety", MagicMock())
    monkeypatch.setattr(main_module, "_init_directories", MagicMock())
    monkeypatch.setattr(main_module, "_init_database_and_characters", AsyncMock())
    monkeypatch.setattr(main_module, "_init_gpt_sovits_client", AsyncMock())
    async def cleanup_loop(**kwargs):
        await asyncio.Event().wait()
    monkeypatch.setattr(main_module, "_audio_cleanup_loop", cleanup_loop)

    cleanup_tasks = []
    async def shutdown(settings, cleanup_task):
        cleanup_tasks.append(cleanup_task)
        cleanup_task.cancel()
        await asyncio.gather(cleanup_task, return_exceptions=True)
    shutdown_mock = AsyncMock(side_effect=shutdown)
    monkeypatch.setattr(main_module, "_shutdown_services", shutdown_mock)

    with pytest.raises(RuntimeError, match="serving context failed"):
        async with main_module.lifespan(MagicMock()):
            raise RuntimeError("serving context failed")
    shutdown_mock.assert_awaited_once()
    assert cleanup_tasks[0].done()


class TestServerLifespanCyclesAndCheckpointing:
    """Verifies server startup and shutdown lifespans, detecting lingering locks or regressions."""

    @pytest.mark.asyncio
    async def test_lifespan_startup_gpt_sovits_url_unbound_local_regression(self, tmp_path, monkeypatch):
        """
        Adversarial probe: Verify that lifespan startup successfully reads gpt_sovits_url
        from the DB without raising UnboundLocalError for 'get_db'.
        """
        test_db = tmp_path / "lifespan_scope_test.db"
        monkeypatch.setenv("GALGAME2VOICE_DB_PATH", str(test_db))
        await init_db(test_db)

        # Set custom gpt_sovits_url in DB settings
        async with get_db(test_db) as conn:
            await conn.execute("UPDATE settings SET gpt_sovits_url = 'http://127.0.0.1:19880' WHERE id = 1;")
            await conn.commit()

        app = create_app()

        # Capture warnings/errors during lifespan startup
        import logging
        records = []

        class LogHandler(logging.Handler):
            def emit(self, record):
                records.append(record)

        handler = LogHandler()
        logging.getLogger("galgame2voice.main").addHandler(handler)

        try:
            async with app.router.lifespan_context(app):
                pass
        finally:
            logging.getLogger("galgame2voice.main").removeHandler(handler)

        unbound_errors = [
            r.getMessage() for r in records
            if "cannot access local variable 'get_db'" in r.getMessage()
            or "UnboundLocalError" in str(r.exc_info)
        ]
        assert len(unbound_errors) == 0, (
            f"Lifespan startup suffered UnboundLocalError on get_db: {unbound_errors}"
        )

    @pytest.mark.asyncio
    async def test_lifespan_shutdown_wal_checkpoint_truncate_zero_locks(self, tmp_path, monkeypatch):
        """
        Verify that PRAGMA wal_checkpoint(TRUNCATE) executes on shutdown and leaves zero
        lingering transaction locks across multiple sequential startup/shutdown cycles.
        """
        test_db = tmp_path / "lifespan_cycles.db"
        monkeypatch.setenv("GALGAME2VOICE_DB_PATH", str(test_db))
        await init_db(test_db)

        app = create_app()

        for cycle in range(3):
            # Startup phase
            async with app.router.lifespan_context(app):
                # Write data during active session
                async with get_db(test_db) as conn:
                    for i in range(5):
                        await crud.add_message(conn, MessageCreate(
                            session_id=f"cycle-sess-{cycle}",
                            role="user",
                            content_chinese=f"Cycle {cycle} Msg {i}",
                            content_japanese=f"Cycle {cycle} Msg {i}"
                        ))
            # Shutdown phase has completed here.

            # Empirical Lock Verification: Open direct exclusive connection
            # If any transaction lock lingered, BEGIN EXCLUSIVE will raise OperationalError.
            direct_conn = sqlite3.connect(str(test_db), timeout=1.0)
            try:
                cur = direct_conn.cursor()
                cur.execute("BEGIN EXCLUSIVE;")
                cur.execute("SELECT COUNT(*) FROM messages;")
                count = cur.fetchone()[0]
                assert count == (cycle + 1) * 5
                direct_conn.commit()

                # Verify WAL checkpoint status
                cur.execute("PRAGMA wal_checkpoint(TRUNCATE);")
                res = cur.fetchall()
                # res is [(busy, log, checkpointed)]
                assert res[0][0] == 0, f"WAL checkpoint returned busy={res[0][0]}"
            finally:
                direct_conn.close()
