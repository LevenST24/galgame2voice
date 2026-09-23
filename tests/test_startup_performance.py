"""
Test suite for startup performance and latency optimizations.
"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
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
async def test_lifespan_preseeds_active_voice_profile(isolate_test_database):
    await init_db(isolate_test_database)

    with patch("galgame2voice.config.get_settings") as mock_settings:
        settings_inst = MagicMock()
        settings_inst.db_path = Path(isolate_test_database)
        settings_inst.data_dir = Path(isolate_test_database).parent
        settings_inst.audio_dir = Path(isolate_test_database).parent / "audio"
        settings_inst.logs_dir = Path(isolate_test_database).parent / "logs"
        settings_inst.log_level = "INFO"
        settings_inst.log_to_file = False
        settings_inst.app_name = "galgame2voice"
        settings_inst.app_version = "2.0.0"
        settings_inst.host = "127.0.0.1"
        settings_inst.port = 8080
        settings_inst.audio_cleanup_interval_seconds = 3600
        mock_settings.return_value = settings_inst

        from galgame2voice.main import lifespan
        app = create_app()
        async with lifespan(app):
            from galgame2voice.services.voice_manager import get_voice_manager
            from galgame2voice.services.gpt_sovits_client import get_gpt_sovits_client
            vm = get_voice_manager()
            client = get_gpt_sovits_client()
            assert vm.active_profile is not None
            assert client.current_gpt_weights == vm.active_profile.gpt_weights_path
            assert client.current_sovits_weights == vm.active_profile.sovits_weights_path
