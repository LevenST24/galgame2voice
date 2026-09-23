"""
Unit tests for architectural refactoring and structural decoupling.
Verifies modularity of database migrations, audio cleaner service, and 100% backward compatibility.
"""

import tempfile
import time
from pathlib import Path
import pytest
import aiosqlite


@pytest.mark.asyncio
async def test_database_migrations_decoupling():
    """Verify migrations.py works standalone and is properly re-exported in crud.py."""
    import galgame2voice.database.migrations as migrations
    import galgame2voice.database.crud as crud
    import galgame2voice.database as db_pkg

    # Direct symbols
    assert migrations.CURRENT_SCHEMA_VERSION == 6
    assert callable(migrations.run_schema_migrations)
    assert callable(migrations.init_schema_and_seeds)
    assert callable(migrations.auto_heal_voice_profiles)

    # Re-exported from crud.py
    assert crud.CURRENT_SCHEMA_VERSION == migrations.CURRENT_SCHEMA_VERSION
    assert crud.run_schema_migrations is migrations.run_schema_migrations
    assert crud.init_schema_and_seeds is migrations.init_schema_and_seeds
    assert crud.auto_heal_voice_profiles is migrations.auto_heal_voice_profiles

    # Exported from database package
    assert "CURRENT_SCHEMA_VERSION" in db_pkg.__all__
    assert "run_schema_migrations" in db_pkg.__all__
    assert "init_schema_and_seeds" in db_pkg.__all__
    assert "auto_heal_voice_profiles" in db_pkg.__all__

    # Execute migrations on an in-memory database using migrations.init_schema_and_seeds
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await migrations.init_schema_and_seeds(conn)

        # Check version
        ver = await conn.execute("PRAGMA user_version;")
        row = await ver.fetchone()
        assert row[0] == 6

        # Check settings table
        cur = await conn.execute("SELECT active_provider_id, console_token, inference_precision FROM settings WHERE id = 1;")
        settings_row = await cur.fetchone()
        assert settings_row is not None
        assert settings_row["active_provider_id"] == "deepseek"
        assert len(settings_row["console_token"]) > 0

        # Check composite indexes exist
        idx_cur = await conn.execute("SELECT name FROM sqlite_master WHERE type = 'index';")
        idx_rows = await idx_cur.fetchall()
        idx_names = {r["name"] for r in idx_rows}
        assert "idx_messages_role_session" in idx_names
        assert "idx_token_usage_provider_tokens" in idx_names
        assert "idx_sessions_updated_at" in idx_names


@pytest.mark.asyncio
async def test_audio_cleaner_service_decoupling():
    """Verify services.audio_cleaner works standalone and is backward-compatible in main.py."""
    import galgame2voice.services.audio_cleaner as cleaner
    import galgame2voice.main as main_mod
    import galgame2voice.services as srv_pkg

    # Re-export identity check
    assert main_mod._audio_cleanup_loop is cleaner._audio_cleanup_loop
    assert main_mod._scan_and_clean is cleaner._scan_and_clean
    assert main_mod._cache_scan_and_clean is cleaner._cache_scan_and_clean
    assert main_mod.AudioCleanerService is cleaner.AudioCleanerService

    # Exported from services package
    assert "AudioCleanerService" in srv_pkg.__all__
    assert "CharacterManager" in srv_pkg.__all__
    assert "get_character_manager" in srv_pkg.__all__

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()

        # Create expired and fresh files
        old_wav = tmp_path / "chunk_old.wav"
        old_wav.write_bytes(b"RIFF" + b"\x00" * 100)
        now = time.time()
        import os
        os.utime(old_wav, (now - 3600, now - 3600))

        protected_ref = tmp_path / "ref.ogg"
        protected_ref.write_bytes(b"OggS" + b"\x00" * 100)
        os.utime(protected_ref, (now - 3600, now - 3600))

        # Test _scan_and_clean
        cleaned, unlinked = cleaner._scan_and_clean(
            audio_dir=tmp_path,
            protected_audio_names={"ref.ogg"},
            cutoff=now - 1800,
            cache_cutoff=now - 86400 * 7,
        )
        assert cleaned == 1
        assert not old_wav.exists()
        assert protected_ref.exists()

        # Test AudioCleanerService start and stop
        service = cleaner.AudioCleanerService(audio_dir=tmp_path, interval_seconds=10)
        task = service.start()
        assert task is not None
        assert not task.done()
        await service.stop()
        assert task.done()


@pytest.mark.asyncio
async def test_tts_options_and_audio_spec_decoupling():
    """Verify tts_options.py and audio_spec.py are decoupled with 100% backward compatibility."""
    import galgame2voice.services.tts_options as tts_opts
    import galgame2voice.services.gpt_sovits_client as gpt_client
    import galgame2voice.utils.audio_spec as audio_spec

    # TTS options identity and re-exports
    assert gpt_client.SLICING_METHODS is tts_opts.SLICING_METHODS
    assert gpt_client.TTS_PRESETS is tts_opts.TTS_PRESETS
    assert gpt_client.validate_user_tts_options is tts_opts.validate_user_tts_options
    assert gpt_client.resolve_tts_options is tts_opts.resolve_tts_options
    assert gpt_client.VoiceProfileWeightSpec is tts_opts.VoiceProfileWeightSpec
    assert gpt_client._extract_weight_spec is tts_opts._extract_weight_spec

    # Audio silence and waveform analysis identity
    assert gpt_client.wav_peak_amplitude is audio_spec.wav_peak_amplitude
    assert gpt_client.is_silent_audio is audio_spec.is_silent_audio
    assert gpt_client.SILENT_AUDIO_ERROR == audio_spec.SILENT_AUDIO_ERROR

    # Validation behaviour test
    valid = tts_opts.validate_user_tts_options({"speed_factor": 1.2, "temperature": 0.8})
    assert valid["speed_factor"] == 1.2

    with pytest.raises(ValueError):
        tts_opts.validate_user_tts_options({"speed_factor": 99.0})

    # Resolved options test
    resolved = tts_opts.resolve_tts_options({"speed": 1.5, "preset": "low_latency"})
    assert resolved["speed_factor"] == 1.5
    assert resolved["top_k"] == 5


@pytest.mark.asyncio
async def test_providers_router_decoupling():
    """Verify providers.py router is decoupled and mounted in config.py with identical routes."""
    import galgame2voice.routers.providers as prov_mod
    import galgame2voice.routers.config as conf_mod
    from fastapi import FastAPI
    from httpx import AsyncClient, ASGITransport

    # Model and helper re-exports
    assert conf_mod.ProviderTestRequest is prov_mod.ProviderTestRequest
    assert conf_mod.ProviderTestResponse is prov_mod.ProviderTestResponse
    assert conf_mod.TelegramTestRequest is prov_mod.TelegramTestRequest
    assert conf_mod._allow_private_endpoints is prov_mod._allow_private_endpoints
    assert conf_mod._enforce_llm_url_guard is prov_mod._enforce_llm_url_guard

    # Verify routes registered on conf_mod.router
    app = FastAPI()
    app.include_router(conf_mod.router)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Check /api/config route
        resp_config = await client.get("/api/config")
        assert resp_config.status_code in (200, 500)  # endpoint reached

        # Check /api/providers route
        resp_prov = await client.get("/api/providers")
        assert resp_prov.status_code in (200, 500)  # endpoint reached

        # Check /api/providers/presets route
        resp_presets = await client.get("/api/providers/presets")
        assert resp_presets.status_code == 200
        data = resp_presets.json()
        assert "presets" in data

