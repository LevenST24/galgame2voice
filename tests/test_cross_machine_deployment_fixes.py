"""
Unit tests for cross-machine deployment robustness and audio synthesis fixes:
1. UTF-8 BOM safe path reading for sovits_dir.txt and config files.
2. Audio duration check & automatic fallback when reference audio is < 3.0s or > 10.0s or missing.
3. Database auto-healing of missing or legacy hardcoded dev machine paths.
4. NVIDIA Turing TU116/TU117 GPU compatibility and FP32 precision patching.
"""

import io
import struct
import wave
from pathlib import Path
import pytest
import aiosqlite

from galgame2voice.config import get_settings
from galgame2voice.database import crud
from galgame2voice.services.tts_service import TtsService
from galgame2voice.services.emotion_references import resolve_emotion_reference, NATSUME_EMOTION_REFERENCES
from scripts.run_server import (
    _check_sovits_dir,
    build_gpt_sovits_env,
    check_python_environment,
    run_hardware_diagnostics,
)


# ============================================================================
# Helpers
# ============================================================================

def create_synthetic_wav(path: Path, duration_sec: float, sample_rate: int = 16000) -> Path:
    """Creates a temporary PCM WAV file of specified duration."""
    n_frames = int(sample_rate * duration_sec)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        frames = bytearray()
        for i in range(n_frames):
            val = int(2000 * (1 if (i // 80) % 2 == 0 else -1))
            frames.extend(struct.pack("<h", val))
        w.writeframes(frames)
    return path


# ============================================================================
# 1. UTF-8 BOM-Safe Reading
# ============================================================================

def test_bom_safe_sovits_cache_reading(tmp_path, monkeypatch):
    """Verifies that reading sovits_dir.txt with UTF-8 BOM (PowerShell export) is handled cleanly."""
    settings = get_settings()
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    cache_file = data_dir / "sovits_dir.txt"

    # Create mock GPT-SoVITS directory
    mock_sovits = tmp_path / "GPT-SoVITS-mock"
    mock_sovits.mkdir(parents=True, exist_ok=True)
    (mock_sovits / "api_v2.py").write_text("# mock api", encoding="utf-8")

    # Write cache file with UTF-8 BOM (\xef\xbb\xbf)
    bom_content = "\ufeff" + str(mock_sovits)
    cache_file.write_bytes(bom_content.encode("utf-8"))

    # Verify that raw read without sig has BOM
    raw = cache_file.read_text(encoding="utf-8")
    assert raw.startswith("\ufeff")

    # Verify utf-8-sig cleanly strips BOM
    stripped = cache_file.read_text(encoding="utf-8-sig").strip()
    assert not stripped.startswith("\ufeff")
    assert Path(stripped) == mock_sovits

    # Verify _check_sovits_dir resolves it
    resolved = _check_sovits_dir(Path(stripped))
    assert resolved == mock_sovits


def test_nested_gpt_sovits_discovery(tmp_path):
    """Verifies that double-nested directories (e.g. zip extraction output) are discovered."""
    outer = tmp_path / "GPT-SoVITS-v2pro-20250604"
    inner = outer / "GPT-SoVITS-v2pro-20250604"
    inner.mkdir(parents=True, exist_ok=True)
    (inner / "api_v2.py").write_text("# mock api", encoding="utf-8")

    # Probing outer folder should find inner folder with api_v2.py
    found = _check_sovits_dir(outer)
    assert found == inner


# ============================================================================
# 2. Audio Duration Guard & Fallback
# ============================================================================

def test_bundled_reference_audios_durations():
    """Verifies that bundled reference audios meet the 3.0s~10.0s requirement."""
    settings = get_settings()
    natsume_dir = settings.project_root / "audio" / "references" / "natsume"

    # Gentle reference must be ~5.03s
    gentle_path = natsume_dir / "gentle.ogg"
    if gentle_path.is_file():
        dur = TtsService.get_audio_duration(gentle_path)
        assert dur is not None
        assert 3.0 <= dur <= 10.0
        assert round(dur, 1) == 5.0

    # cool.ogg is now verified >= 3.0s (3.23s)
    cool_path = natsume_dir / "cool.ogg"
    if cool_path.is_file():
        dur_cool = TtsService.get_audio_duration(cool_path)
        assert dur_cool is not None
        assert 3.0 <= dur_cool <= 10.0


def test_cool_emotion_mapped_to_valid_duration_audio():
    """Verifies cool emotion maps to authentic cool.ogg (>=3.0s) with valid Japanese prompt."""
    res = resolve_emotion_reference("四季夏目", "cool")
    assert res is not None
    assert "cool.ogg" in res["ref_audio_path"]
    assert "勝手に仲間" in res["prompt_text"]
    dur = TtsService.get_audio_duration(res["ref_audio_path"])
    assert dur is not None
    assert 3.0 <= dur <= 10.0


@pytest.mark.asyncio
async def test_tts_service_duration_fallback_short_audio(tmp_path):
    """Verifies that reference audio under 3.0s automatically falls back to default reference."""
    short_wav = create_synthetic_wav(tmp_path / "short_2s.wav", duration_sec=2.0)
    dur = TtsService.get_audio_duration(short_wav)
    assert dur is not None and dur < 3.0

    tts = TtsService(audio_dir=tmp_path)
    opts = {"ref_audio_path": str(short_wav), "ai_adaptive_voice": False}
    populated = await tts._populate_voice_profile_opts(opts)

    # Must fall back to default profile reference audio (not the 2.0s short_wav)
    assert populated["ref_audio_path"] != str(short_wav)
    assert "gentle.ogg" in populated["ref_audio_path"] or "nat002_032.ogg" in populated["ref_audio_path"]


@pytest.mark.asyncio
async def test_tts_service_duration_fallback_long_audio(tmp_path):
    """Verifies that reference audio over 10.0s automatically falls back to default reference."""
    long_wav = create_synthetic_wav(tmp_path / "long_12s.wav", duration_sec=12.0)
    dur = TtsService.get_audio_duration(long_wav)
    assert dur is not None and dur > 10.0

    tts = TtsService(audio_dir=tmp_path)
    opts = {"ref_audio_path": str(long_wav), "ai_adaptive_voice": False}
    populated = await tts._populate_voice_profile_opts(opts)

    # Must fall back to default profile reference audio (not the 12.0s long_wav)
    assert populated["ref_audio_path"] != str(long_wav)
    assert "gentle.ogg" in populated["ref_audio_path"] or "nat002_032.ogg" in populated["ref_audio_path"]


@pytest.mark.asyncio
async def test_tts_service_duration_accepts_valid_audio(tmp_path):
    """Verifies that valid reference audio (3~10s) is accepted without fallback."""
    valid_wav = create_synthetic_wav(tmp_path / "valid_5s.wav", duration_sec=5.0)
    dur = TtsService.get_audio_duration(valid_wav)
    assert dur is not None and 3.0 <= dur <= 10.0

    tts = TtsService(audio_dir=tmp_path)
    opts = {"ref_audio_path": str(valid_wav), "ai_adaptive_voice": False}
    populated = await tts._populate_voice_profile_opts(opts)

    # Valid audio must be preserved
    assert populated["ref_audio_path"] == str(valid_wav)


# ============================================================================
# 3. Database Auto-Healing
# ============================================================================

@pytest.mark.asyncio
async def test_database_auto_healing_legacy_and_missing_paths(tmp_path):
    """Verifies that crud.auto_heal_voice_profiles repairs broken, missing, and dev machine paths."""
    db_path = tmp_path / "test_heal.db"
    async with aiosqlite.connect(str(db_path)) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("""
            CREATE TABLE voice_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                ref_audio_path TEXT NOT NULL,
                prompt_text TEXT NOT NULL DEFAULT '',
                prompt_lang TEXT NOT NULL DEFAULT 'ja',
                text_lang TEXT NOT NULL DEFAULT 'ja',
                gpt_weights_path TEXT NOT NULL DEFAULT '',
                sovits_weights_path TEXT NOT NULL DEFAULT '',
                is_default INTEGER NOT NULL DEFAULT 0,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # Insert 4 test profiles:
        # 1. Legacy dev path with E:/yuzusoft/...
        # 2. Non-existent path
        # 3. Empty path
        # 4. Valid path to bundled audio
        await conn.execute("""
            INSERT INTO voice_profiles (id, name, ref_audio_path) VALUES
            (1, 'Dev Profile', 'E:/yuzusoft/cafeStella/sikivoice/nat002_032.ogg'),
            (2, 'Missing Profile', 'C:/non_existent_folder/missing.wav'),
            (3, 'Empty Profile', ''),
            (4, 'Valid Profile', 'audio/references/natsume/gentle.ogg');
        """)
        await conn.commit()

        healed_count = await crud.auto_heal_voice_profiles(conn)
        await conn.commit()

        # Profiles 1, 2, 3 should be healed; Profile 4 should remain
        assert healed_count == 3

        cur = await conn.execute("SELECT id, ref_audio_path FROM voice_profiles ORDER BY id;")
        rows = await cur.fetchall()
        for r in rows[:3]:
            assert "gentle.ogg" in r["ref_audio_path"] or "nat002_032.ogg" in r["ref_audio_path"]
            assert "yuzusoft" not in r["ref_audio_path"]
            assert "non_existent" not in r["ref_audio_path"]

        # Profile 4 remains unchanged
        assert rows[3]["ref_audio_path"] == "audio/references/natsume/gentle.ogg"


# ============================================================================
# 4. Process-Isolated Precision & Pre-Flight Diagnostics
# ============================================================================

def test_gpt_sovits_process_isolation_env(tmp_path):
    """Verifies that GPT-SoVITS runtime environment sets is_half explicitly (no GPU-name matching)."""
    sovits_dir = tmp_path / "mock_sovits"
    sovits_dir.mkdir()

    env_fp32 = build_gpt_sovits_env(sovits_dir, is_half=False)
    assert env_fp32["is_half"] == "False"
    assert "no_proxy" in env_fp32

    env_fp16 = build_gpt_sovits_env(sovits_dir, is_half=True)
    assert env_fp16["is_half"] == "True"


def test_preflight_diagnostics_calibration_notice(capsys, monkeypatch, tmp_path):
    """Verifies that pre-flight diagnostics advertises automatic precision calibration."""
    import scripts.run_server as rs

    monkeypatch.setattr(rs, "read_precision_cache", lambda root: None)
    monkeypatch.setattr(rs, "detect_gpu_capability", lambda: (True, "NVIDIA GeForce RTX 3080", 1))
    diag = rs.run_hardware_diagnostics()
    captured = capsys.readouterr()
    assert "[精度校准]" in captured.out
    assert diag["has_nvidia"] is True


def test_preflight_python_and_deps_check():
    """Verifies that pre-flight Python version and dependency validation functions correctly."""
    assert check_python_environment() is True


@pytest.mark.asyncio
async def test_tts_service_duration_fallback_missing_file(tmp_path):
    """Verifies that non-existent reference audio automatically falls back to default reference."""
    missing_file = tmp_path / "absolutely_missing_reference.wav"
    dur = TtsService.get_audio_duration(missing_file)
    assert dur is None

    tts = TtsService(audio_dir=tmp_path)
    opts = {"ref_audio_path": str(missing_file), "ai_adaptive_voice": False}
    populated = await tts._populate_voice_profile_opts(opts)

    # Must fall back to default profile reference audio
    assert populated["ref_audio_path"] != str(missing_file)
    assert "gentle.ogg" in populated["ref_audio_path"] or "nat002_032.ogg" in populated["ref_audio_path"]


@pytest.mark.asyncio
async def test_switch_voice_profile_resolves_relative_reference_path(monkeypatch):
    """Verifies switch_voice_profile passes resolved absolute path to GPT-SoVITS."""
    from galgame2voice.services.gpt_sovits_client import GptSovitsClient, resolve_reference_audio_path

    recorded_params = {}

    class MockServer:
        async def handle_request(self, method, path, json_data=None, params=None):
            recorded_params[path] = params or {}
            class MockResp:
                status_code = 200
                text = "ok"
            return MockResp()

    client = GptSovitsClient(server=MockServer())
    profile = {
        "name": "Natsume Test",
        "gpt_weights_path": "GPT_weights/test.ckpt",
        "sovits_weights_path": "SoVITS_weights/test.pth",
        "refer_audio_path": "audio/references/natsume/gentle.ogg",
        "refer_text": "test text",
        "refer_language": "ja",
    }

    success = await client.switch_voice_profile(profile, force=True)
    assert success is True
    assert "/set_refer_audio" in recorded_params
    sent_path = recorded_params["/set_refer_audio"]["refer_audio_path"]

    # Must be absolute path
    assert Path(sent_path).is_absolute()
    assert sent_path.endswith("gentle.ogg")


@pytest.mark.asyncio
async def test_init_schema_and_seeds_seeds_portable_path(tmp_path):
    """Verifies that init_schema_and_seeds inserts portable relative reference path."""
    db_path = tmp_path / "fresh_seed.db"
    async with aiosqlite.connect(str(db_path)) as conn:
        conn.row_factory = aiosqlite.Row
        await crud.init_schema_and_seeds(conn)

        cur = await conn.execute("SELECT ref_audio_path FROM voice_profiles WHERE id = 1;")
        row = await cur.fetchone()
        assert row is not None
        ref_path = row["ref_audio_path"]
        assert "yuzusoft" not in ref_path
        # Must be portable relative path
        assert ref_path == "audio/references/natsume/gentle.ogg"


@pytest.mark.asyncio
async def test_auto_heal_arbitrary_drive_letters(tmp_path):
    """Verifies that auto_heal_voice_profiles cleanses arbitrary machine-specific drive letters."""
    db_path = tmp_path / "drive_letters.db"
    async with aiosqlite.connect(str(db_path)) as conn:
        conn.row_factory = aiosqlite.Row
        await crud.init_schema_and_seeds(conn)

        # Inject machine-specific drive letters from foreign machines
        await conn.execute("UPDATE voice_profiles SET ref_audio_path = 'D:\\other_user\\voice.wav' WHERE id = 1;")
        await conn.commit()

        healed = await crud.auto_heal_voice_profiles(conn)
        assert healed >= 1

        cur = await conn.execute("SELECT ref_audio_path FROM voice_profiles WHERE id = 1;")
        row = await cur.fetchone()
        assert row["ref_audio_path"] == "audio/references/natsume/gentle.ogg"


