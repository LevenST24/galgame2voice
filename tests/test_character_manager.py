"""
Unit tests for Self-Contained Character Package (自包含角色包) Architecture.
Tests:
1. CharacterManifest schema validation
2. CharacterManager auto-discovery & package loading
3. Audio reference validation & duration boundary enforcement ([3.0s, 10.0s])
4. Dynamic emotion resolution via CharacterManager & emotion_references.py
5. Database synchronization & idempotence without corrupting user settings
6. Engine FP32 default invariant & --fp16 CLI flag
7. Release packaging bundling for characters/
"""

import json
import os
import struct
import wave
from pathlib import Path
import aiosqlite
import pytest

from galgame2voice.config import get_settings
from galgame2voice.services.character_manager import (
    CharacterManager,
    CharacterManifest,
    CharacterPackage,
    EmotionConfig,
    VoiceParamsConfig,
    get_character_manager,
)
from galgame2voice.services.emotion_references import (
    resolve_emotion_reference,
    normalize_emotion,
)
import scripts.run_server as rs
import scripts.package_release as pr


def _create_synthetic_wav(path: Path, duration_sec: float, sample_rate: int = 16000) -> Path:
    """Creates a temporary valid PCM WAV file of specified duration."""
    path.parent.mkdir(parents=True, exist_ok=True)
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
# 1. Manifest Schema Validation Tests
# ============================================================================

def test_manifest_schema_valid():
    data = {
        "id": "natsume",
        "name": "四季夏目",
        "version": "1.0.0",
        "description": "幼刀附丧神，性格内向、毒舌但温柔",
        "system_prompt": "Prompt test",
        "default_voice_params": {
            "speed": 1.0,
            "temperature": 0.8,
            "top_k": 20,
            "top_p": 0.9,
        },
        "gpt_weights": "gpt.ckpt",
        "sovits_weights": "sovits.pth",
        "emotions": {
            "gentle": {
                "audio": "refs/gentle.ogg",
                "text": "おはようございます",
                "lang": "ja",
            },
            "happy": {
                "audio": "refs/happy.ogg",
                "text": "ありがとう",
                "lang": "ja",
            },
        },
    }
    manifest = CharacterManifest.model_validate(data)
    assert manifest.id == "natsume"
    assert manifest.name == "四季夏目"
    assert manifest.default_voice_params.speed == 1.0
    assert manifest.default_voice_params.temperature == 0.8
    assert len(manifest.emotions) == 2
    assert manifest.emotions["gentle"].audio == "refs/gentle.ogg"


def test_manifest_schema_missing_required():
    # Missing 'name'
    with pytest.raises(Exception):
        CharacterManifest.model_validate({"id": "test_id"})

    # Missing 'id'
    with pytest.raises(Exception):
        CharacterManifest.model_validate({"name": "Test Name"})


# ============================================================================
# 2. Character Discovery & Audio Duration Validation
# ============================================================================

def test_character_manager_discovers_natsume(tmp_path):
    mgr = CharacterManager(get_settings().characters_dir)
    discovered = mgr.discover_characters()
    assert len(discovered) >= 1
    natsume_pkg = mgr.get_character("四季夏目")
    assert natsume_pkg is not None
    assert natsume_pkg.is_valid is True
    assert natsume_pkg.id == "natsume"
    assert "gentle" in natsume_pkg.manifest.emotions
    assert "happy" in natsume_pkg.manifest.emotions
    assert "sad" in natsume_pkg.manifest.emotions
    assert "tsundere" in natsume_pkg.manifest.emotions
    assert "cool" in natsume_pkg.manifest.emotions
    assert "shy" in natsume_pkg.manifest.emotions


def test_character_manager_duration_validation(tmp_path):
    """Test that audios outside [3.0, 10.0] are rejected and flagged in validation_errors."""
    char_dir = tmp_path / "test_char"
    char_dir.mkdir(parents=True)
    refs_dir = char_dir / "refs"
    refs_dir.mkdir(parents=True)

    # Audio 1: valid 4.0s
    _create_synthetic_wav(refs_dir / "valid.wav", duration_sec=4.0)
    # Audio 2: too short 2.0s
    _create_synthetic_wav(refs_dir / "too_short.wav", duration_sec=2.0)
    # Audio 3: too long 12.0s
    _create_synthetic_wav(refs_dir / "too_long.wav", duration_sec=12.0)

    manifest_data = {
        "id": "test_duration",
        "name": "Duration Test Char",
        "version": "1.0.0",
        "emotions": {
            "gentle": {"audio": "refs/valid.wav", "text": "Valid"},
            "sad": {"audio": "refs/too_short.wav", "text": "Short"},
            "angry": {"audio": "refs/too_long.wav", "text": "Long"},
            "missing": {"audio": "refs/not_exists.wav", "text": "Missing"},
        },
    }
    (char_dir / "manifest.json").write_text(json.dumps(manifest_data), encoding="utf-8")

    mgr = CharacterManager(tmp_path)
    discovered = mgr.discover_characters()
    pkg = mgr.get_character("test_duration")

    assert pkg is not None
    assert pkg.is_valid is False
    assert any("too_short" in err for err in pkg.validation_errors)
    assert any("too_long" in err for err in pkg.validation_errors)
    assert any("not found" in err for err in pkg.validation_errors)


# ============================================================================
# 3. Dynamic Emotion Resolution Tests
# ============================================================================

def test_resolve_emotion_reference_dynamic():
    # Canonical emotion
    res = resolve_emotion_reference("四季夏目", "happy")
    assert res is not None
    assert "happy.ogg" in res["ref_audio_path"]
    assert "ありがとう" in res["prompt_text"]
    assert res["prompt_lang"] == "ja"
    assert res["emotion"] == "happy"

    # Synonym mapping
    res_tsun = resolve_emotion_reference("四季夏目", "傲娇")
    assert res_tsun is not None
    assert "tsundere.ogg" in res_tsun["ref_audio_path"] or "angry.ogg" in res_tsun["ref_audio_path"]
    assert "バカ" in res_tsun["prompt_text"]

    # English synonym
    res_cheerful = resolve_emotion_reference("natsume", "cheerful")
    assert res_cheerful is not None
    assert "happy.ogg" in res_cheerful["ref_audio_path"]

    # Unknown emotion falls back to gentle
    res_unknown = resolve_emotion_reference("四季夏目", "non_existent_emotion")
    assert res_unknown is not None
    assert "gentle.ogg" in res_unknown["ref_audio_path"]

    # None emotion falls back to gentle
    res_none = resolve_emotion_reference("四季夏目", None)
    assert res_none is not None
    assert "gentle.ogg" in res_none["ref_audio_path"]

    # Non-existent character returns None
    assert resolve_emotion_reference("Arona_Random_Char", "happy") is None


def test_character_manager_resolve_fallback(tmp_path):
    """Test fallback to gentle when specific emotion is missing."""
    char_dir = tmp_path / "fallback_char"
    char_dir.mkdir(parents=True)
    refs_dir = char_dir / "refs"
    refs_dir.mkdir(parents=True)
    _create_synthetic_wav(refs_dir / "gentle.wav", duration_sec=4.0)

    manifest_data = {
        "id": "fallback_char",
        "name": "Fallback Char",
        "emotions": {
            "gentle": {"audio": "refs/gentle.wav", "text": "Default Gentle Text"},
        },
    }
    (char_dir / "manifest.json").write_text(json.dumps(manifest_data), encoding="utf-8")

    mgr = CharacterManager(tmp_path)
    mgr.discover_characters()

    # Requesting 'happy' which is not in manifest should fall back to 'gentle'
    res = mgr.resolve_emotion_audio_path("fallback_char", "happy")
    assert res is not None
    assert "gentle.wav" in res["ref_audio_path"]
    assert res["prompt_text"] == "Default Gentle Text"


# ============================================================================
# 4. Database Synchronization & Idempotence Tests
# ============================================================================

@pytest.mark.asyncio
async def test_character_manager_sync_with_db_idempotent(tmp_path):
    db_file = tmp_path / "test_char_sync.db"
    async with aiosqlite.connect(str(db_file)) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("""
            CREATE TABLE voice_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                gpt_weights_path TEXT NOT NULL DEFAULT '',
                sovits_weights_path TEXT NOT NULL DEFAULT '',
                ref_audio_path TEXT NOT NULL DEFAULT '',
                prompt_text TEXT NOT NULL DEFAULT '',
                prompt_lang TEXT NOT NULL DEFAULT 'ja',
                text_lang TEXT NOT NULL DEFAULT 'ja',
                system_prompt TEXT NOT NULL DEFAULT '',
                is_default INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        await conn.commit()

        mgr = CharacterManager(get_settings().characters_dir)
        mgr.discover_characters()

        # 1. First sync inserts into empty DB
        synced_1 = await mgr.sync_with_db(conn)
        assert synced_1 >= 1

        cur = await conn.execute("SELECT * FROM voice_profiles;")
        rows_1 = await cur.fetchall()
        assert len(rows_1) >= 1
        natsume_row = next(r for r in rows_1 if "夏目" in r["name"])
        assert natsume_row["is_default"] == 1
        assert "gentle.ogg" in natsume_row["ref_audio_path"]

        # 2. Simulate user custom setting (change prompt_text and system_prompt)
        await conn.execute(
            "UPDATE voice_profiles SET prompt_text = 'User Custom Text', is_default = 1 WHERE id = ?;",
            (natsume_row["id"],)
        )
        await conn.commit()

        # 3. Second sync should be idempotent and NOT corrupt or overwrite user settings
        synced_2 = await mgr.sync_with_db(conn)
        assert synced_2 == 0  # No updates needed

        cur = await conn.execute("SELECT * FROM voice_profiles WHERE id = ?;", (natsume_row["id"],))
        rows_2 = await cur.fetchone()
        assert rows_2["prompt_text"] == "User Custom Text"
        assert rows_2["is_default"] == 1


# ============================================================================
# 5. FP32 Engine Invariant & --fp16 CLI Flag Tests
# ============================================================================

def test_server_cli_fp16_flag():
    # Default is FP32 (fp16 == False)
    args_default = rs.parse_args([])
    assert args_default.fp16 is False

    # Explicit --fp16 flag
    args_fp16 = rs.parse_args(["--fp16"])
    assert args_fp16.fp16 is True


def test_build_gpt_sovits_env_fp32_default(tmp_path):
    sovits_dir = tmp_path / "mock_sovits"
    sovits_dir.mkdir()

    # Enforced FP32
    env_default = rs.build_gpt_sovits_env(sovits_dir, is_half=False)
    assert env_default["is_half"] == "False"

    # User explicit FP16
    env_fp16 = rs.build_gpt_sovits_env(sovits_dir, is_half=True)
    assert env_fp16["is_half"] == "True"


# ============================================================================
# 6. Release Packaging Bundling Tests
# ============================================================================

def test_package_release_includes_character_assets(tmp_path):
    # Manifest, prompts, and audio files under characters/ MUST be included
    assert pr.should_include(Path("characters/四季夏目/manifest.json")) is True
    assert pr.should_include(Path("characters/四季夏目/system_prompt.txt")) is True
    assert pr.should_include(Path("characters/四季夏目/refs/gentle.ogg")) is True
    assert pr.should_include(Path("characters/四季夏目/refs/cool.ogg")) is True

    # Large binary weights must be excluded
    assert pr.should_include(Path("characters/四季夏目/huge_weights.ckpt")) is False
    assert pr.should_include(Path("characters/四季夏目/huge_weights.pth")) is False


# ============================================================================
# 7. Robustness, Lazy Discovery & Security Tests
# ============================================================================

def test_character_manager_lazy_discovery():
    """Verifies that CharacterManager getters automatically discover characters if not yet discovered."""
    mgr = CharacterManager()
    assert len(mgr._packages) == 0

    # Calling get_character directly triggers lazy discovery
    pkg = mgr.get_character("四季夏目")
    assert pkg is not None
    assert pkg.is_valid is True
    assert pkg.id == "natsume"

    # get_available_characters works
    available = mgr.get_available_characters()
    assert len(available) >= 1

    # get_active_character_manifest works
    manifest = mgr.get_active_character_manifest()
    assert manifest is not None
    assert manifest.id == "natsume"


def test_character_manager_path_traversal_rejection(tmp_path):
    """Verifies that character packages containing directory traversal payloads are rejected."""
    bad_dir = tmp_path / "bad_char"
    bad_dir.mkdir()
    manifest_data = {
        "id": "bad_traversal",
        "name": "Bad Traversal Char",
        "gpt_weights": "../../../sensitive.ckpt",
        "emotions": {
            "gentle": {
                "audio": "../../windows/system32/cmd.exe",
                "text": "test",
            }
        },
    }
    (bad_dir / "manifest.json").write_text(json.dumps(manifest_data), encoding="utf-8")

    mgr = CharacterManager(tmp_path)
    discovered = mgr.discover_characters()
    pkg = mgr.get_character("bad_traversal")
    assert pkg is not None
    assert pkg.is_valid is False
    assert any("traversal" in err.lower() for err in pkg.validation_errors)


@pytest.mark.asyncio
async def test_character_manager_sync_stores_portable_paths(tmp_path):
    """Verifies that character manager DB sync writes portable project-relative paths, not machine absolute."""
    db_file = tmp_path / "portable_test.db"
    async with aiosqlite.connect(str(db_file)) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("""
            CREATE TABLE voice_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                gpt_weights_path TEXT NOT NULL DEFAULT '',
                sovits_weights_path TEXT NOT NULL DEFAULT '',
                ref_audio_path TEXT NOT NULL DEFAULT '',
                prompt_text TEXT NOT NULL DEFAULT '',
                prompt_lang TEXT NOT NULL DEFAULT 'ja',
                text_lang TEXT NOT NULL DEFAULT 'ja',
                system_prompt TEXT NOT NULL DEFAULT '',
                is_default INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        await conn.commit()

        mgr = CharacterManager(get_settings().characters_dir)
        await mgr.sync_with_db(conn)

        cur = await conn.execute("SELECT * FROM voice_profiles WHERE name = '四季夏目';")
        row = await cur.fetchone()
        assert row is not None
        ref_path = row["ref_audio_path"]
        # Must be project-relative POSIX path
        assert ref_path == "characters/四季夏目/refs/gentle.ogg"
        assert not Path(ref_path).is_absolute()


def test_legacy_path_resolution_backward_compatibility():
    """Verifies that resolve_existing_audio_path backwards-compatibly resolves legacy audio references."""
    from galgame2voice.utils.path_guard import resolve_existing_audio_path

    # Old natsume reference path resolves to self-contained character package
    res_gentle = resolve_existing_audio_path("audio/references/natsume/gentle.ogg")
    assert res_gentle is not None
    assert res_gentle.is_file()

    # Old nat002_032.ogg resolves to gentle.ogg in character package
    res_nat = resolve_existing_audio_path("audio/nat002_032.ogg")
    assert res_nat is not None
    assert res_nat.is_file()

    # Package refs/ path
    res_ref = resolve_existing_audio_path("refs/gentle.ogg")
    assert res_ref is not None
    assert res_ref.is_file()


def test_build_gpt_sovits_env_default_argument(tmp_path):
    """Verifies that build_gpt_sovits_env defaults to FP32 without explicit arguments."""
    env = rs.build_gpt_sovits_env(tmp_path)
    assert env["is_half"] == "False"


# ============================================================================
# 8. Multi-Character Package Integration & Hardening Tests
# ============================================================================

def test_character_manager_discovers_all_three_characters():
    """Verifies that CharacterManager auto-discovers packages including original core characters and newly added characters."""
    mgr = CharacterManager(get_settings().characters_dir)
    discovered = mgr.discover_characters()
    assert len(discovered) >= 3, f"Expected at least 3 packages, got {len(discovered)}"

    expected_chars = {
        "natsume": "四季夏目",
        "kanna": "明月栞那",
        "kazari": "西园寺风莉",
    }
    for char_id, expected_name in expected_chars.items():
        pkg = mgr.get_character(char_id)
        assert pkg is not None, f"Character package {char_id} not found"
        assert pkg.is_valid is True, f"Character package {char_id} is invalid: {pkg.validation_errors}"
        assert pkg.name == expected_name
        assert pkg.id == char_id
        # All 7 core emotions present
        for emo in ["gentle", "happy", "angry", "sad", "shy", "tsundere", "cool"]:
            assert emo in pkg.manifest.emotions, f"Emotion {emo} missing from {char_id}"


def test_all_reference_audios_duration_boundary():
    """Verifies that all reference audios across all packages have duration in [3.0s, 10.0s]."""
    from galgame2voice.services.tts_service import TtsService
    try:
        import soundfile as sf
    except ImportError:
        sf = None

    mgr = CharacterManager(get_settings().characters_dir)
    discovered = mgr.discover_characters()
    assert len(discovered) >= 3

    audio_count = 0
    for pkg in discovered:
        for emo_name, emo_cfg in pkg.manifest.emotions.items():
            audio_path = pkg.resolve_audio_path(emo_cfg.audio)
            assert audio_path is not None, f"Audio path could not be resolved for {pkg.name} - {emo_name}"
            assert audio_path.is_file(), f"Audio file {audio_path} does not exist"

            # Check via soundfile if available
            if sf is not None:
                info = sf.info(str(audio_path))
                assert 3.0 <= info.duration <= 10.0, (
                    f"Audio {audio_path} duration {info.duration:.2f}s is out of [3.0, 10.0] range"
                )

            # Check via TtsService helper (pure-Python / mutagen / wave fallback)
            tts_dur = TtsService.get_audio_duration(audio_path)
            assert tts_dur is not None, f"TtsService could not determine duration for {audio_path}"
            assert 3.0 <= tts_dur <= 10.0, f"TtsService duration {tts_dur:.2f}s is out of [3.0, 10.0] range for {audio_path}"
            audio_count += 1

    assert audio_count >= 21, f"Expected at least 21 reference audios, tested {audio_count}"


def test_character_weights_are_real_binaries_gt_100mb():
    """Verifies that all model weights across all characters are valid binaries > 100MB (> 104,857,600 bytes)."""
    mgr = CharacterManager(get_settings().characters_dir)
    discovered = mgr.discover_characters()
    assert len(discovered) >= 3

    for pkg in discovered:
        for weight_type in ("gpt_weights", "sovits_weights"):
            weight_path_str = pkg.resolve_weight_path(weight_type)
            assert weight_path_str, f"{pkg.name} {weight_type} not resolved"
            weight_path = Path(weight_path_str)
            assert weight_path.is_file(), f"{pkg.name} {weight_type} file does not exist: {weight_path}"

            file_size = weight_path.stat().st_size
            assert file_size > 104857600, (
                f"{pkg.name} {weight_type} size {file_size} is <= 100MB (not genuine binary)"
            )

            # Confirm binary non-pointer content
            with open(weight_path, "rb") as f:
                header = f.read(16)
            assert not header.startswith(b"GPT_weights")
            assert not header.startswith(b"SoVITS_weights")


@pytest.mark.asyncio
async def test_character_manager_sync_with_db_all_characters_and_default(tmp_path):
    """Verifies that DB sync syncs all 3 characters, cleans ghosts, and sets Natsume as deterministic default."""
    db_file = tmp_path / "multi_char_sync.db"
    async with aiosqlite.connect(str(db_file)) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("""
            CREATE TABLE voice_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                gpt_weights_path TEXT NOT NULL DEFAULT '',
                sovits_weights_path TEXT NOT NULL DEFAULT '',
                ref_audio_path TEXT NOT NULL DEFAULT '',
                prompt_text TEXT NOT NULL DEFAULT '',
                prompt_lang TEXT NOT NULL DEFAULT 'ja',
                text_lang TEXT NOT NULL DEFAULT 'ja',
                system_prompt TEXT NOT NULL DEFAULT '',
                is_default INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        # Insert a stale ghost profile that should be pruned
        await conn.execute("""
            INSERT INTO voice_profiles (name, gpt_weights_path, ref_audio_path)
            VALUES ('kazari', 'characters/kazari/gpt.ckpt', 'audio/references/natsume/gentle.ogg');
        """)
        # Insert a stale Kanna profile with wrong audio and external E: weights
        await conn.execute("""
            INSERT INTO voice_profiles (name, gpt_weights_path, sovits_weights_path, ref_audio_path, prompt_text)
            VALUES ('明月栞那', 'E:\\stale\\kanna.ckpt', 'E:\\stale\\kanna.pth', 'audio/references/natsume/gentle.ogg', '');
        """)
        await conn.commit()

        mgr = CharacterManager(get_settings().characters_dir)
        synced = await mgr.sync_with_db(conn)
        assert synced >= 2

        # 1. Ghost profile kazari must be pruned
        cur = await conn.execute("SELECT * FROM voice_profiles WHERE name = 'kazari';")
        assert (await cur.fetchone()) is None

        # 2. All 3 characters must exist in DB
        for name in ["四季夏目", "明月栞那", "西园寺风莉"]:
            cur = await conn.execute("SELECT * FROM voice_profiles WHERE name = ?;", (name,))
            row = await cur.fetchone()
            assert row is not None, f"Profile {name} missing from DB"
            assert not Path(row["ref_audio_path"]).is_absolute()
            assert not Path(row["gpt_weights_path"]).is_absolute()
            assert not Path(row["sovits_weights_path"]).is_absolute()
            assert bool(row["prompt_text"].strip()), f"Empty prompt_text for {name}"

        # 3. Kanna's stale row must be healed
        cur = await conn.execute("SELECT * FROM voice_profiles WHERE name = '明月栞那';")
        kanna_row = await cur.fetchone()
        assert "natsume" not in kanna_row["ref_audio_path"]
        assert "characters/明月栞那" in kanna_row["ref_audio_path"]
        assert "characters/明月栞那" in kanna_row["gpt_weights_path"]
        assert "characters/明月栞那" in kanna_row["sovits_weights_path"]

        # 4. Natsume must be the default
        cur = await conn.execute("SELECT * FROM voice_profiles WHERE is_default = 1;")
        default_rows = await cur.fetchall()
        assert len(default_rows) == 1
        assert default_rows[0]["name"] == "四季夏目"


def test_character_manager_whitespace_queries():
    """Verifies that whitespace-only queries return None, and space-separated queries resolve properly."""
    mgr = CharacterManager(get_settings().characters_dir)

    # Whitespace-only queries must return None
    assert mgr.get_character("") is None
    assert mgr.get_character("   ") is None
    assert mgr.get_character("\t\n") is None
    assert mgr.get_character("   \t  ") is None

    # Space-separated queries must resolve
    assert mgr.get_character("明月 栞那") is not None
    assert mgr.get_character("明月 栞那").id == "kanna"
    assert mgr.get_character("西园寺 风莉") is not None
    assert mgr.get_character("西园寺 风莉").id == "kazari"
    assert mgr.get_character("四季 夏目") is not None
    assert mgr.get_character("四季 夏目").id == "natsume"
    assert mgr.get_character("  kanna  ") is not None
    assert mgr.get_character("  kanna  ").id == "kanna"


def test_character_switch_api_all_characters_and_aliases():
    """Verifies that /api/characters/switch successfully switches to all characters by name, ID, and alias."""
    from unittest.mock import patch, AsyncMock
    from fastapi.testclient import TestClient
    from galgame2voice.main import app

    client = TestClient(app)

    with patch("galgame2voice.services.voice_manager.VoiceManager.switch_profile", new_callable=AsyncMock) as mock_switch:
        mock_switch.return_value = True

        test_cases = [
            ("四季夏目", 200),
            ("natsume", 200),
            ("明月栞那", 200),
            ("kanna", 200),
            ("西园寺风莉", 200),
            ("kazari", 200),
            ("nonexistent_heroine", 404),
        ]

        for char_query, expected_status in test_cases:
            resp = client.post("/api/characters/switch", json={"character_name": char_query})
            assert resp.status_code == expected_status, (
                f"Expected status {expected_status} for '{char_query}', got {resp.status_code}: {resp.text}"
            )
            if expected_status == 200:
                data = resp.json()
                assert data["status"] == "switched"
                assert "character" in data
                assert "character_id" in data



