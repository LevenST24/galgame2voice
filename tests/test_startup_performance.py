"""
Test suite for startup performance and latency optimizations.
"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
import pytest

from galgame2voice.services.gpt_sovits_client import GptSovitsClient
from galgame2voice.services.voice_manager import VoiceManager
from galgame2voice.database.session import get_db, init_db
from galgame2voice.main import create_app


def test_startup_batch_file_syntax_and_safety():
    bat_path = Path(__file__).resolve().parent.parent / "启动.bat"
    assert bat_path.exists()
    content = bat_path.read_text(encoding="utf-8")
    assert "scripts\\run_server.py" in content


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
async def test_lifespan_preseeds_active_voice_profile(tmp_path, monkeypatch, mock_gpt_sovits):
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
        assert vm.active_profile is not None
        assert vm.active_profile.id == expected_profile.id
        assert client.current_gpt_weights == expected_profile.gpt_weights_path
        assert client.current_sovits_weights == expected_profile.sovits_weights_path


async def test_lifespan_drains_resources_when_serving_context_raises(monkeypatch):
    import galgame2voice.main as main_module

    monkeypatch.setattr(main_module, "_init_logging_and_safety", MagicMock())
    monkeypatch.setattr(main_module, "_init_directories", MagicMock())
    monkeypatch.setattr(main_module, "_init_database_and_characters", AsyncMock())
    monkeypatch.setattr(main_module, "_init_gpt_sovits_client", AsyncMock())
    monkeypatch.setattr(main_module, "_start_telegram_bg", lambda settings: None)
    async def cleanup_loop(**kwargs):
        await asyncio.Event().wait()
    monkeypatch.setattr(main_module, "_audio_cleanup_loop", cleanup_loop)

    cleanup_tasks = []
    async def shutdown(settings, cleanup_task, telegram_task):
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
