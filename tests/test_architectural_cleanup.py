"""
Tests verifying architectural cleanup and pure modularity.
Ensures zero leaks, deduplication, and safeguards across services.
"""

import ast
import asyncio
import inspect
import os
import sys
import time
from pathlib import Path

import aiosqlite
import pytest

from galgame2voice.utils import prosody
from galgame2voice.utils.prosody import (
    DYNAMIC_SPEED_MIN,
    DYNAMIC_SPEED_MAX,
    DYNAMIC_TEMP_MIN,
    DYNAMIC_TEMP_MAX,
    clamp_dynamic_speed,
    clamp_dynamic_temperature,
)
import galgame2voice.services.gpt_sovits_client as client_mod
import galgame2voice.services.chat_service as chat_mod
from galgame2voice.database import crud
import scripts.run_server as rs


# ============================================================================
# 1. Reverse Import & Hardware Leak Removal in HTTP Client
# ============================================================================

def test_gpt_sovits_client_has_no_hardware_detection_leak():
    """Verifies that gpt_sovits_client has no reverse import from scripts.run_server and no is_turing."""
    # Module must not export or define is_turing_tu116_tu117_gpu
    assert not hasattr(client_mod, "is_turing_tu116_tu117_gpu")
    assert "is_turing_tu116_tu117_gpu" not in getattr(client_mod, "__all__", [])

    # Source code must not import from scripts.run_server
    client_src = inspect.getsource(client_mod)
    assert "from scripts.run_server" not in client_src
    assert "import scripts.run_server" not in client_src

    # GptSovitsClient instance must not carry hardware state
    client = client_mod.GptSovitsClient()
    assert not hasattr(client, "is_turing_gpu")


# ============================================================================
# 2. Prosody Deduplication & Accurate Clamping
# ============================================================================

def test_prosody_constants_and_ranges():
    """Verifies prosody constants and clamping accuracy."""
    assert DYNAMIC_SPEED_MIN == 0.50
    assert DYNAMIC_SPEED_MAX == 1.50
    assert DYNAMIC_TEMP_MIN == 0.60
    assert DYNAMIC_TEMP_MAX == 1.20

    # Test speed bounds
    assert clamp_dynamic_speed(0.1) == 0.50
    assert clamp_dynamic_speed(2.0) == 1.50
    assert clamp_dynamic_speed(1.0) == 1.0
    assert clamp_dynamic_speed("bad", fallback=1.1) == 1.1

    # Test temp bounds
    assert clamp_dynamic_temperature(0.1) == 0.60
    assert clamp_dynamic_temperature(2.0) == 1.20
    assert clamp_dynamic_temperature(0.9) == 0.9
    assert clamp_dynamic_temperature(None, fallback=0.8) == 0.8


def test_services_import_shared_prosody_utilities():
    """Verifies that chat_service and gpt_sovits_client reuse the exact prosody functions."""
    assert chat_mod.clamp_dynamic_speed is prosody.clamp_dynamic_speed
    assert chat_mod.clamp_dynamic_temperature is prosody.clamp_dynamic_temperature
    assert chat_mod.DYNAMIC_SPEED_MIN == prosody.DYNAMIC_SPEED_MIN
    assert chat_mod.DYNAMIC_SPEED_MAX == prosody.DYNAMIC_SPEED_MAX

    assert client_mod.clamp_dynamic_speed is prosody.clamp_dynamic_speed
    assert client_mod.clamp_dynamic_temperature is prosody.clamp_dynamic_temperature
    assert client_mod.DYNAMIC_SPEED_MIN == prosody.DYNAMIC_SPEED_MIN
    assert client_mod.DYNAMIC_SPEED_MAX == prosody.DYNAMIC_SPEED_MAX


# ============================================================================
# 3. Database Layer Purification
# ============================================================================

def test_crud_auto_heal_has_no_shutil_or_yuzusoft_strings():
    """Verifies auto_heal_voice_profiles has no shutil copying side effects and no yuzusoft string."""
    func_src = inspect.getsource(crud.auto_heal_voice_profiles)
    assert "shutil" not in func_src
    assert "copy2" not in func_src
    assert "yuzusoft" not in func_src


@pytest.mark.asyncio
async def test_crud_auto_heal_objective_validation(tmp_path):
    """Verifies objective path validation repairs missing/unportable paths and keeps valid paths."""
    db_path = tmp_path / "test_heal_purified.db"
    async with aiosqlite.connect(str(db_path)) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("""
            CREATE TABLE voice_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                ref_audio_path TEXT NOT NULL,
                is_default INTEGER NOT NULL DEFAULT 0,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        await conn.execute("""
            INSERT INTO voice_profiles (id, name, ref_audio_path) VALUES
            (1, 'Missing File', 'audio/does_not_exist.wav'),
            (2, 'Empty Path', ''),
            (3, 'Valid Bundled Audio', 'audio/references/natsume/gentle.ogg');
        """)
        await conn.commit()

        healed = await crud.auto_heal_voice_profiles(conn)
        assert healed == 2

        cur = await conn.execute("SELECT id, ref_audio_path FROM voice_profiles ORDER BY id;")
        rows = await cur.fetchall()
        assert "gentle.ogg" in rows[0]["ref_audio_path"]
        assert "gentle.ogg" in rows[1]["ref_audio_path"]
        assert rows[2]["ref_audio_path"] == "audio/references/natsume/gentle.ogg"


@pytest.mark.asyncio
async def test_crud_auto_heal_canonicalizes_absolute_project_paths(tmp_path):
    """Verifies that auto_heal canonicalizes absolute project paths into portable relative paths."""
    from galgame2voice.config import get_settings
    settings = get_settings()
    abs_gentle = (settings.project_root / "audio" / "references" / "natsume" / "gentle.ogg").resolve()

    db_path = tmp_path / "test_heal_canonicalize.db"
    async with aiosqlite.connect(str(db_path)) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("""
            CREATE TABLE voice_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                ref_audio_path TEXT NOT NULL,
                is_default INTEGER NOT NULL DEFAULT 0,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        await conn.execute("""
            INSERT INTO voice_profiles (id, name, ref_audio_path) VALUES
            (1, 'Absolute In-Project Voice', ?);
        """, (str(abs_gentle),))
        await conn.commit()

        healed = await crud.auto_heal_voice_profiles(conn)
        assert healed == 1, "Absolute path in project root should be canonicalized to relative!"

        cur = await conn.execute("SELECT ref_audio_path FROM voice_profiles WHERE id = 1;")
        row = await cur.fetchone()
        assert row["ref_audio_path"] == "audio/references/natsume/gentle.ogg"


# ============================================================================
# 4. Safeguard Ephemeral Audio Cleanup Loop
# ============================================================================

@pytest.mark.asyncio
async def test_audio_cleanup_preserves_reference_audios(tmp_path, monkeypatch):
    """Verifies background cleanup removes ephemeral files but NEVER deletes *.ogg or reference files."""
    from galgame2voice.main import _audio_cleanup_loop

    audio_dir = tmp_path / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    refs_dir = audio_dir / "references"
    refs_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = audio_dir / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    # 1. Create reference audio in audio root and references/
    ref_root = audio_dir / "nat002_032.ogg"
    ref_root.write_bytes(b"OGG_ROOT_DATA")
    ref_nested = refs_dir / "gentle.ogg"
    ref_nested.write_bytes(b"OGG_NESTED_DATA")

    # 2. Create ephemeral synthesized files
    chunk_file = audio_dir / "chunk_0_12345.wav"
    chunk_file.write_bytes(b"CHUNK_WAV_DATA")
    full_file = audio_dir / "full_abcde.wav"
    full_file.write_bytes(b"FULL_WAV_DATA")

    # Backdate all files to 2 hours ago (well past 30m cutoff)
    old_time = time.time() - 7200
    for p in (ref_root, ref_nested, chunk_file, full_file):
        os.utime(str(p), (old_time, old_time))

    # Mock DB settings to retention 30 minutes
    class MockSettings:
        audio_retention_minutes = 30

    from contextlib import asynccontextmanager
    @asynccontextmanager
    async def mock_get_db():
        yield None

    monkeypatch.setattr("galgame2voice.main.get_db", mock_get_db)
    async def mock_get_settings_raw(conn):
        return MockSettings()
    monkeypatch.setattr("galgame2voice.database.crud.get_settings_raw", mock_get_settings_raw)

    # Run one iteration of _audio_cleanup_loop
    task = asyncio.create_task(_audio_cleanup_loop(audio_dir, interval_seconds=0))
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # Ephemeral synthesized files must be unlinked
    assert not chunk_file.exists(), "Ephemeral chunk_*.wav must be cleaned up!"
    assert not full_file.exists(), "Ephemeral full_*.wav must be cleaned up!"

    # Reference audios must be protected and intact
    assert ref_root.exists(), "Root nat002_032.ogg must NEVER be unlinked by cleanup!"
    assert ref_nested.exists(), "Nested references/*.ogg must NEVER be unlinked by cleanup!"


@pytest.mark.asyncio
async def test_audio_cleanup_preserves_registered_wav_references(tmp_path, monkeypatch):
    """Verifies that custom .wav reference files registered in voice_profiles are protected from cleanup."""
    from galgame2voice.main import _audio_cleanup_loop

    audio_dir = tmp_path / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)

    # 1. Custom WAV reference registered in DB
    custom_ref_wav = audio_dir / "custom_character_voice.wav"
    custom_ref_wav.write_bytes(b"CUSTOM_REF_WAV_BYTES")

    # 2. Ephemeral synthesized WAV file
    ephemeral_wav = audio_dir / "chunk_123.wav"
    ephemeral_wav.write_bytes(b"EPHEMERAL_CHUNK_WAV_BYTES")

    # Backdate both files beyond retention
    old_time = time.time() - 7200
    os.utime(str(custom_ref_wav), (old_time, old_time))
    os.utime(str(ephemeral_wav), (old_time, old_time))

    # Mock DB connection that returns the custom ref in voice_profiles
    class MockConn:
        async def execute(self, query):
            class MockCursor:
                async def fetchall(self):
                    return [("audio/custom_character_voice.wav",)]
            return MockCursor()

    class MockSettings:
        audio_retention_minutes = 30

    from contextlib import asynccontextmanager
    @asynccontextmanager
    async def mock_get_db():
        yield MockConn()

    monkeypatch.setattr("galgame2voice.main.get_db", mock_get_db)
    async def mock_get_settings_raw(conn):
        return MockSettings()
    monkeypatch.setattr("galgame2voice.database.crud.get_settings_raw", mock_get_settings_raw)

    task = asyncio.create_task(_audio_cleanup_loop(audio_dir, interval_seconds=0))
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # Registered reference wav MUST be preserved
    assert custom_ref_wav.exists(), "Registered custom_character_voice.wav must NOT be unlinked by cleanup!"
    # Unregistered ephemeral wav MUST be unlinked
    assert not ephemeral_wav.exists(), "Unregistered ephemeral chunk_123.wav must be cleaned up!"


# ============================================================================
# 5. Clean Up Dead Stubs & Deduplicate MEMORYSTATUSEX in run_server.py
# ============================================================================

def test_patch_sovits_precision_config_is_backward_compat_shim(tmp_path):
    """Verifies patch_sovits_precision_config exists as a safe no-op for backward compatibility."""
    assert callable(rs.patch_sovits_precision_config)
    res = rs.patch_sovits_precision_config(tmp_path, force_fp32=True)
    assert res is None


def test_memorystatusex_deduplicated_in_run_server():
    """Verifies ctypes MEMORYSTATUSEX is defined at most once in run_server.py AST."""
    server_path = Path(rs.__file__)
    tree = ast.parse(server_path.read_text(encoding="utf-8"))

    class_defs = [node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]
    count = class_defs.count("MEMORYSTATUSEX")
    assert count <= 1, f"MEMORYSTATUSEX defined {count} times in run_server.py (should be deduplicated to <= 1)"

    # Test get_system_ram_gb returns valid floats
    total_ram, avail_ram = rs.get_system_ram_gb()
    assert isinstance(total_ram, float)
    assert isinstance(avail_ram, float)
    assert total_ram >= 0.0
    assert avail_ram >= 0.0
