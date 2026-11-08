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
from galgame2voice.utils.path_guard import to_project_relative_path
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


def _create_synthetic_wav(
    path: Path,
    duration_sec: float,
    sample_rate: int = 16000,
    amplitude: int = 2000,
) -> Path:
    """Creates a temporary valid PCM WAV file of specified duration."""
    path.parent.mkdir(parents=True, exist_ok=True)
    n_frames = int(sample_rate * duration_sec)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        frames = bytearray()
        for i in range(n_frames):
            val = int(amplitude * (1 if (i // 80) % 2 == 0 else -1))
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

@pytest.mark.requires_character_assets
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
    mgr.discover_characters()
    pkg = mgr.get_character("test_duration")

    assert pkg is not None
    assert pkg.is_valid is False
    assert any("too_short" in err for err in pkg.validation_errors)
    assert any("too_long" in err for err in pkg.validation_errors)
    assert any("不存在" in err for err in pkg.validation_errors)


# ============================================================================
# 3. Dynamic Emotion Resolution Tests
# ============================================================================

@pytest.mark.requires_character_assets
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
    assert "tsundere.ogg" in res_tsun["ref_audio_path"]
    assert "angry.ogg" not in res_tsun["ref_audio_path"]
    assert "バカ" in res_tsun["prompt_text"]

    # Angry mapping (independent, never tsundere)
    res_angry = resolve_emotion_reference("四季夏目", "angry")
    assert res_angry is not None
    assert "angry.ogg" in res_angry["ref_audio_path"]
    assert "tsundere.ogg" not in res_angry["ref_audio_path"]

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

@pytest.mark.requires_character_assets
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

def test_package_release_excludes_local_character_assets(tmp_path):
    # Character packages are installed separately, including manifests and prompts.
    assert pr.should_include(Path("characters/四季夏目/manifest.json")) is False
    assert pr.should_include(Path("characters/四季夏目/system_prompt.txt")) is False
    assert pr.should_include(Path("characters/四季夏目/refs/gentle.ogg")) is False
    assert pr.should_include(Path("characters/四季夏目/refs/cool.ogg")) is False

    # Large binary weights and user archives must be excluded
    assert pr.should_include(Path("characters/四季夏目/huge_weights.ckpt")) is False
    assert pr.should_include(Path("characters/四季夏目/huge_weights.pth")) is False
    assert pr.should_include(Path("characters/四季夏目.zip")) is False
    assert pr.should_include(Path("characters/栞那.7z")) is False


# ============================================================================
# 7. Robustness, Lazy Discovery & Security Tests
# ============================================================================

@pytest.mark.requires_character_assets
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
    mgr.discover_characters()
    pkg = mgr.get_character("bad_traversal")
    assert pkg is not None
    assert pkg.is_valid is False
    assert any("路径穿越" in err for err in pkg.validation_errors)


@pytest.mark.requires_character_assets
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


@pytest.mark.requires_character_assets
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

@pytest.mark.requires_character_assets
def test_character_manager_discovers_all_three_characters():
    """Alias for backward compatibility with existing test runners."""
    test_character_manager_discovers_all_eight_characters()


@pytest.mark.requires_character_assets
def test_character_manager_discovers_all_eight_characters():
    """
    Verifies that CharacterManager auto-discovers all 8 character packages:
    四季夏目, 明月栞那, 西园寺风莉, 三司绫濑, 二条院羽月, 在原七海, 常陆茉子, 丛雨.
    Asserts:
    1. All 8 characters are discovered and is_valid is True.
    2. Each character has all 7 standard emotions (gentle, happy, sad, tsundere, angry, shy, cool).
    3. Inside each character package, all 7 emotion audio files have distinct MD5 hashes.
    4. Anti-hijacking: resolving 'angry' does NOT return tsundere.ogg, and resolving 'tsundere' does NOT return angry.ogg.
    """
    import hashlib
    mgr = CharacterManager(get_settings().characters_dir)
    discovered = mgr.discover_characters()
    assert len(discovered) >= 13, f"Expected at least 13 packages, got {len(discovered)}"

    expected_chars = {
        "natsume": "四季夏目",
        "kanna": "明月栞那",
        "kazari": "西园寺风莉",
        "ayase": "三司绫濑",
        "hazuki": "二条院羽月",
        "nanami": "在原七海",
        "mako": "常陆茉子",
        "murasame": "丛雨",
        "noa": "白雪乃爱",
        "amane": "谷风天音",
        "kurumi": "小云雀来海",
        "kaguya": "星河辉耶",
        "orie": "高楯欧丽叶",
    }
    for char_id, expected_name in expected_chars.items():
        pkg = mgr.get_character(char_id)
        assert pkg is not None, f"Character package {char_id} not found"
        assert pkg.is_valid is True, f"Character package {char_id} is invalid: {pkg.validation_errors}"
        assert pkg.name == expected_name
        assert pkg.id == char_id

        # All 7 core emotions present and distinct MD5 hashes
        seen_md5s = {}
        for emo in ["gentle", "happy", "angry", "sad", "shy", "tsundere", "cool"]:
            assert emo in pkg.manifest.emotions, f"Emotion {emo} missing from {char_id}"
            audio_p = pkg.resolve_audio_path(pkg.manifest.emotions[emo].audio)
            assert audio_p is not None and audio_p.is_file(), f"Audio for {char_id}/{emo} does not exist"
            h = hashlib.md5(audio_p.read_bytes()).hexdigest()
            assert h not in seen_md5s, (
                f"Duplicate MD5 in character '{char_id}': emotion '{emo}' shares identical MD5 ({h[:8]}) with '{seen_md5s[h]}'"
            )
            seen_md5s[h] = emo

        # Anti-hijacking verification: angry must never return tsundere, tsundere must never return angry
        res_angry = mgr.resolve_emotion_audio_path(char_id, "angry")
        assert res_angry is not None, f"Failed to resolve angry for {char_id}"
        assert "angry.ogg" in res_angry["ref_audio_path"]
        assert "tsundere.ogg" not in res_angry["ref_audio_path"]

        res_tsun = mgr.resolve_emotion_audio_path(char_id, "tsundere")
        assert res_tsun is not None, f"Failed to resolve tsundere for {char_id}"
        assert "tsundere.ogg" in res_tsun["ref_audio_path"]
        assert "angry.ogg" not in res_tsun["ref_audio_path"]


def test_character_manager_rejects_duplicate_audio_md5(tmp_path):
    """Verifies that CharacterManager marks package invalid if two emotions share identical MD5 or file."""
    import shutil
    char_dir = tmp_path / "dup_char"
    char_dir.mkdir(parents=True)
    refs_dir = char_dir / "refs"
    refs_dir.mkdir(parents=True)

    # Create synthetic WAV and duplicate it
    _create_synthetic_wav(refs_dir / "audio1.wav", duration_sec=4.0)
    shutil.copy2(refs_dir / "audio1.wav", refs_dir / "audio2.wav")

    manifest_data = {
        "id": "dup_char",
        "name": "Duplicate Character",
        "emotions": {
            "gentle": {"audio": "refs/audio1.wav", "text": "Gentle Text"},
            "shy": {"audio": "refs/audio2.wav", "text": "Shy Text with same audio"},
        },
    }
    (char_dir / "manifest.json").write_text(json.dumps(manifest_data), encoding="utf-8")

    mgr = CharacterManager(tmp_path)
    discovered = mgr.discover_characters()
    assert len(discovered) == 0, "Package with duplicate audio MD5 must not be considered valid"
    pkg = mgr.get_character("dup_char")
    assert pkg is not None
    assert pkg.is_valid is False
    assert any("重复" in err for err in pkg.validation_errors)


@pytest.mark.requires_character_assets
def test_all_reference_audios_duration_boundary():
    """Verifies that all reference audios across all 8 packages have duration in [3.0s, 10.0s]."""
    from galgame2voice.services.tts_service import TtsService
    try:
        import soundfile as sf
    except ImportError:
        sf = None

    mgr = CharacterManager(get_settings().characters_dir)
    discovered = mgr.discover_characters()
    assert len(discovered) >= 8

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

    assert audio_count >= 56, f"Expected at least 56 reference audios (8 chars * 7 emotions), tested {audio_count}"


@pytest.mark.requires_character_assets
def test_character_weights_are_real_binaries_gt_100mb():
    """Verifies that all model weights across all characters are valid binaries > 100MB (> 104,857,600 bytes)."""
    mgr = CharacterManager(get_settings().characters_dir)
    discovered = mgr.discover_characters()
    assert len(discovered) >= 8

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


@pytest.mark.requires_character_assets
@pytest.mark.asyncio
async def test_character_manager_sync_with_db_all_characters_and_default(tmp_path):
    """Verifies that DB sync syncs all 3 characters, keeps user profiles, and sets Natsume as deterministic default."""
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
        # Insert a user profile that no package owns; sync must never delete it
        await conn.execute("""
            INSERT INTO voice_profiles (name, gpt_weights_path, ref_audio_path)
            VALUES ('my-custom-voice', 'models/custom.ckpt', 'audio/references/natsume/gentle.ogg');
        """)
        # Insert a stale Kanna profile with wrong audio and external E: weights
        mgr = CharacterManager(get_settings().characters_dir)
        natsume = mgr.get_character("natsume")
        natsume_ref = to_project_relative_path(
            natsume.resolve_audio_path(natsume.manifest.emotions["gentle"].audio)
        )
        await conn.execute("""
            INSERT INTO voice_profiles (name, gpt_weights_path, sovits_weights_path, ref_audio_path, prompt_text)
            VALUES ('明月栞那', 'E:\\stale\\kanna.ckpt', 'E:\\stale\\kanna.pth', ?, '');
        """, (natsume_ref,))
        await conn.commit()

        synced = await mgr.sync_with_db(conn)
        assert synced >= 2

        # 1. Profiles are never deleted by a rescan
        cur = await conn.execute("SELECT * FROM voice_profiles WHERE name = 'my-custom-voice';")
        assert (await cur.fetchone()) is not None

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
        assert kanna_row["ref_audio_path"] != natsume_ref
        assert "characters/明月栞那" in kanna_row["ref_audio_path"]
        assert "characters/明月栞那" in kanna_row["gpt_weights_path"]
        assert "characters/明月栞那" in kanna_row["sovits_weights_path"]

        # 4. Natsume must be the default
        cur = await conn.execute("SELECT * FROM voice_profiles WHERE is_default = 1;")
        default_rows = await cur.fetchall()
        assert len(default_rows) == 1
        assert default_rows[0]["name"] == "四季夏目"


@pytest.mark.requires_character_assets
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


@pytest.mark.requires_character_assets
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
            ("三司绫濑", 200),
            ("ayase", 200),
            ("二条院羽月", 200),
            ("hazuki", 200),
            ("在原七海", 200),
            ("nanami", 200),
            ("常陆茉子", 200),
            ("mako", 200),
            ("丛雨", 200),
            ("murasame", 200),
            ("白雪乃爱", 200),
            ("noa", 200),
            ("谷风天音", 200),
            ("amane", 200),
            ("小云雀来海", 200),
            ("kurumi", 200),
            ("星河辉耶", 200),
            ("kaguya", 200),
            ("高楯欧丽叶", 200),
            ("orie", 200),
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


# ============================================================================
# 9. Manifest default_emotion Resolution & Fallback Regression Tests
# ============================================================================


def _same_resolved_path(p1: str | Path, p2: str | Path) -> bool:
    return Path(p1).resolve() == Path(p2).resolve()


def _unlink_file(path: Path) -> None:
    path.unlink()


@pytest.mark.asyncio
async def test_manifest_default_emotion_respected_in_sync_emotion_resolution_and_voice_resolver(
    tmp_path, monkeypatch
):
    """
    When a character package defines emotions {"angry", "gentle", "calm"} and
    manifest.json explicitly sets "default_emotion": "calm":
    1. Package validation preserves manifest.default_emotion == "calm";
    2. Syncing into SQLite voice_profiles stores "calm" as default ref_audio_path & prompt_text;
    3. resolve_emotion_audio_path(char_id, None) and unknown emotion "unknown_mood" resolve to "calm";
    4. VoiceProfileResolver heals EMPTY, CROSS_PACKAGE, and broken PACKAGE_OWNED references to "calm".
    """
    from galgame2voice.database import crud
    from galgame2voice.database.models import VoiceProfileUpdate
    from galgame2voice.database.session import get_db, init_db
    from galgame2voice.services.voice_resolver import VoiceProfileResolver

    monkeypatch.setenv("GALGAME2VOICE_PROJECT_ROOT", str(tmp_path))
    get_settings.cache_clear()
    CharacterManager.reset_instance()

    def _setup_packages() -> tuple[Path, Path, Path, Path, Path]:
        chars_dir = tmp_path / "characters"
        calm_dir = chars_dir / "calm_heroine"
        calm_refs = calm_dir / "refs"
        angry_wav = _create_synthetic_wav(calm_refs / "angry.wav", duration_sec=4.0, amplitude=1200)
        gentle_wav = _create_synthetic_wav(calm_refs / "gentle.wav", duration_sec=4.0, amplitude=1800)
        calm_wav = _create_synthetic_wav(calm_refs / "calm.wav", duration_sec=4.0, amplitude=2400)
        (calm_dir / "gpt.ckpt").write_bytes(b"synthetic gpt model")
        (calm_dir / "sovits.pth").write_bytes(b"synthetic sovits model")

        (calm_dir / "manifest.json").write_text(
            json.dumps(
                {
                    "id": "calm_heroine",
                    "name": "Calm Heroine",
                    "gpt_weights": "gpt.ckpt",
                    "sovits_weights": "sovits.pth",
                    "default_emotion": "calm",
                    "emotions": {
                        "angry": {"audio": "refs/angry.wav", "text": "Angry prompt text", "lang": "ja"},
                        "gentle": {"audio": "refs/gentle.wav", "text": "Gentle prompt text", "lang": "ja"},
                        "calm": {"audio": "refs/calm.wav", "text": "Calm prompt text", "lang": "ja"},
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        other_dir = chars_dir / "other_heroine"
        other_wav = _create_synthetic_wav(other_dir / "refs" / "other.wav", duration_sec=4.0, amplitude=3000)
        (other_dir / "manifest.json").write_text(
            json.dumps(
                {
                    "id": "other_heroine",
                    "name": "Other Heroine",
                    "emotions": {
                        "gentle": {"audio": "refs/other.wav", "text": "Other prompt text", "lang": "ja"},
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return chars_dir, angry_wav, gentle_wav, calm_wav, other_wav

    try:
        chars_dir, angry_wav, gentle_wav, calm_wav, other_wav = _setup_packages()

        mgr = get_character_manager(chars_dir)
        mgr.discover_characters()
        pkg = mgr.get_character("calm_heroine")
        assert pkg is not None
        assert pkg.is_valid is True
        assert pkg.manifest.default_emotion == "calm"

        db_path = tmp_path / "default_emotion_test.db"
        await init_db(str(db_path))

        async with get_db(str(db_path)) as conn:
            synced = await mgr.sync_with_db(conn)
            assert synced == 2
            profiles = await crud.list_voice_profiles(conn)
            calm_profile = next(p for p in profiles if p.name == "Calm Heroine")
            assert calm_profile.ref_audio_path == "characters/calm_heroine/refs/calm.wav"
            assert calm_profile.prompt_text == "Calm prompt text"

        res_none = mgr.resolve_emotion_audio_path("calm_heroine", None)
        assert res_none is not None
        assert _same_resolved_path(res_none["ref_audio_path"], calm_wav)
        assert res_none["prompt_text"] == "Calm prompt text"
        assert res_none["emotion"] == "calm"

        res_unknown = mgr.resolve_emotion_audio_path("calm_heroine", "unknown_mood")
        assert res_unknown is not None
        assert _same_resolved_path(res_unknown["ref_audio_path"], calm_wav)
        assert res_unknown["prompt_text"] == "Calm prompt text"
        assert res_unknown["emotion"] == "calm"

        # Explicit emotion requests still resolve their exact targets
        res_gentle = mgr.resolve_emotion_audio_path("calm_heroine", "gentle")
        assert res_gentle is not None
        assert _same_resolved_path(res_gentle["ref_audio_path"], gentle_wav)
        res_angry = mgr.resolve_emotion_audio_path("calm_heroine", "angry")
        assert res_angry is not None
        assert _same_resolved_path(res_angry["ref_audio_path"], angry_wav)

        resolver = VoiceProfileResolver()

        # 1) EMPTY ref_audio_path falls back to calm
        async with get_db(str(db_path)) as conn:
            await crud.update_voice_profile(
                conn,
                calm_profile.id,
                VoiceProfileUpdate(ref_audio_path="", prompt_text=""),
            )
        resolver.invalidate(db_path=str(db_path))
        ctx_empty = await resolver.resolve_context(db_path=str(db_path), profile_id=calm_profile.id)
        assert ctx_empty is not None
        assert _same_resolved_path(ctx_empty.ref_audio_path, calm_wav)
        assert ctx_empty.prompt_text == "Calm prompt text"

        # 2) PACKAGE_OWNED missing file falls back to calm
        async with get_db(str(db_path)) as conn:
            await crud.update_voice_profile(
                conn,
                calm_profile.id,
                VoiceProfileUpdate(
                    ref_audio_path="characters/calm_heroine/refs/deleted_audio.wav",
                    prompt_text="Stale prompt",
                ),
            )
        resolver.invalidate(db_path=str(db_path))
        ctx_broken = await resolver.resolve_context(db_path=str(db_path), profile_id=calm_profile.id)
        assert ctx_broken is not None
        assert _same_resolved_path(ctx_broken.ref_audio_path, calm_wav)
        assert ctx_broken.prompt_text == "Calm prompt text"

        # 3) CROSS_PACKAGE reference heals to calm
        async with get_db(str(db_path)) as conn:
            await crud.update_voice_profile(
                conn,
                calm_profile.id,
                VoiceProfileUpdate(
                    ref_audio_path=to_project_relative_path(other_wav),
                    prompt_text="Other prompt text",
                ),
            )
        resolver.invalidate(db_path=str(db_path))
        ctx_cross = await resolver.resolve_context(db_path=str(db_path), profile_id=calm_profile.id)
        assert ctx_cross is not None
        assert _same_resolved_path(ctx_cross.ref_audio_path, calm_wav)
        assert ctx_cross.prompt_text == "Calm prompt text"
    finally:
        CharacterManager.reset_instance()
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_manifest_default_emotion_case_insensitive_synonym_and_missing_file_fallback(
    tmp_path, monkeypatch
):
    """
    Verifies:
    1. Case-insensitive matching of default_emotion (e.g. "CALM" -> "calm");
    2. Synonym normalization matching when default_emotion is not a literal key (e.g. "cheerful" -> "happy");
    3. Secondary fallback order when audio files disappear after discovery:
       default_emotion -> "gentle" -> first valid emotion in definition order.
    """
    from galgame2voice.database import crud
    from galgame2voice.database.models import VoiceProfileUpdate
    from galgame2voice.database.session import get_db, init_db
    from galgame2voice.services.voice_resolver import VoiceProfileResolver

    monkeypatch.setenv("GALGAME2VOICE_PROJECT_ROOT", str(tmp_path))
    get_settings.cache_clear()
    CharacterManager.reset_instance()

    def _setup_packages() -> tuple[Path, Path, Path, Path, Path]:
        chars_dir = tmp_path / "characters"

        # Package A: case-insensitive default_emotion "CALM"
        dir_a = chars_dir / "case_char"
        angry_a = _create_synthetic_wav(dir_a / "refs" / "angry.wav", duration_sec=4.0, amplitude=1100)
        gentle_a = _create_synthetic_wav(dir_a / "refs" / "gentle.wav", duration_sec=4.0, amplitude=1700)
        calm_a = _create_synthetic_wav(dir_a / "refs" / "calm.wav", duration_sec=4.0, amplitude=2300)
        (dir_a / "manifest.json").write_text(
            json.dumps(
                {
                    "id": "case_char",
                    "name": "Case Char",
                    "default_emotion": "CALM",
                    "emotions": {
                        "angry": {"audio": "refs/angry.wav", "text": "Angry A", "lang": "ja"},
                        "gentle": {"audio": "refs/gentle.wav", "text": "Gentle A", "lang": "ja"},
                        "calm": {"audio": "refs/calm.wav", "text": "Calm A", "lang": "ja"},
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        # Package B: synonym default_emotion "cheerful" -> matches "happy" over "gentle"
        dir_b = chars_dir / "synonym_char"
        _create_synthetic_wav(dir_b / "refs" / "angry.wav", duration_sec=4.0, amplitude=1300)
        _create_synthetic_wav(dir_b / "refs" / "gentle.wav", duration_sec=4.0, amplitude=1900)
        happy_b = _create_synthetic_wav(dir_b / "refs" / "happy.wav", duration_sec=4.0, amplitude=2500)
        (dir_b / "manifest.json").write_text(
            json.dumps(
                {
                    "id": "synonym_char",
                    "name": "Synonym Char",
                    "default_emotion": "cheerful",
                    "emotions": {
                        "angry": {"audio": "refs/angry.wav", "text": "Angry B", "lang": "ja"},
                        "gentle": {"audio": "refs/gentle.wav", "text": "Gentle B", "lang": "ja"},
                        "happy": {"audio": "refs/happy.wav", "text": "Happy B", "lang": "ja"},
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return chars_dir, angry_a, gentle_a, calm_a, happy_b

    try:
        chars_dir, angry_a, gentle_a, calm_a, happy_b = _setup_packages()

        mgr = get_character_manager(chars_dir)
        mgr.discover_characters()

        res_case = mgr.resolve_emotion_audio_path("case_char", None)
        assert res_case is not None
        assert _same_resolved_path(res_case["ref_audio_path"], calm_a)
        assert res_case["emotion"] == "calm"

        res_syn = mgr.resolve_emotion_audio_path("synonym_char", None)
        assert res_syn is not None
        assert _same_resolved_path(res_syn["ref_audio_path"], happy_b)
        assert res_syn["emotion"] == "happy"

        db_path = tmp_path / "fallback_chain.db"
        await init_db(str(db_path))
        async with get_db(str(db_path)) as conn:
            await mgr.sync_with_db(conn)
            profiles = await crud.list_voice_profiles(conn)
            case_prof = next(p for p in profiles if p.name == "Case Char")
            syn_prof = next(p for p in profiles if p.name == "Synonym Char")
            assert syn_prof.ref_audio_path == "characters/synonym_char/refs/happy.wav"
            assert syn_prof.prompt_text == "Happy B"

        # Secondary fallback 1: angry.wav missing -> falls back to default_emotion ("calm")
        _unlink_file(angry_a)
        res_missing_angry = mgr.resolve_emotion_audio_path("case_char", "angry")
        assert res_missing_angry is not None
        assert _same_resolved_path(res_missing_angry["ref_audio_path"], calm_a)
        assert res_missing_angry["emotion"] == "calm"

        # Secondary fallback 2: calm.wav also missing -> falls back to "gentle"
        _unlink_file(calm_a)
        res_missing_calm = mgr.resolve_emotion_audio_path("case_char", None)
        assert res_missing_calm is not None
        assert _same_resolved_path(res_missing_calm["ref_audio_path"], gentle_a)
        assert res_missing_calm["emotion"] == "gentle"

        resolver = VoiceProfileResolver()
        async with get_db(str(db_path)) as conn:
            await crud.update_voice_profile(
                conn, case_prof.id, VoiceProfileUpdate(ref_audio_path="", prompt_text="")
            )
        resolver.invalidate(db_path=str(db_path))
        ctx_after_calm_deleted = await resolver.resolve_context(db_path=str(db_path), profile_id=case_prof.id)
        assert ctx_after_calm_deleted is not None
        assert _same_resolved_path(ctx_after_calm_deleted.ref_audio_path, gentle_a)
        assert ctx_after_calm_deleted.prompt_text == "Gentle A"
    finally:
        CharacterManager.reset_instance()
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_manifest_default_emotion_omitted_or_nonexistent_compatible_fallback(
    tmp_path, monkeypatch
):
    """
    Verifies backward compatibility when default_emotion is omitted or points to a non-existent key:
    - Package validation never rejects the package due to missing/unmatched default_emotion;
    - Falls back to "gentle" if available, otherwise to the first valid emotion in definition order.
    """
    from galgame2voice.database import crud
    from galgame2voice.database.models import VoiceProfileUpdate
    from galgame2voice.database.session import get_db, init_db
    from galgame2voice.schemas.character_manifest import validate_character_package
    from galgame2voice.services.voice_resolver import VoiceProfileResolver

    monkeypatch.setenv("GALGAME2VOICE_PROJECT_ROOT", str(tmp_path))
    get_settings.cache_clear()
    CharacterManager.reset_instance()

    def _setup_packages() -> tuple[Path, Path, Path, Path, tuple[Path, Path, Path]]:
        chars_dir = tmp_path / "characters"

        # 1. Omitted default_emotion, has gentle -> gentle
        omitted_dir = chars_dir / "omitted_default"
        _create_synthetic_wav(omitted_dir / "refs" / "angry.wav", duration_sec=4.0, amplitude=1200)
        omitted_gentle = _create_synthetic_wav(omitted_dir / "refs" / "gentle.wav", duration_sec=4.0, amplitude=1800)
        (omitted_dir / "manifest.json").write_text(
            json.dumps(
                {
                    "id": "omitted_default",
                    "name": "Omitted Default",
                    "emotions": {
                        "angry": {"audio": "refs/angry.wav", "text": "Angry 1", "lang": "ja"},
                        "gentle": {"audio": "refs/gentle.wav", "text": "Gentle 1", "lang": "ja"},
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        # 2. Non-existent default_emotion, has gentle -> valid package, falls back to gentle
        bad_with_gentle_dir = chars_dir / "bad_default_with_gentle"
        _create_synthetic_wav(bad_with_gentle_dir / "refs" / "angry.wav", duration_sec=4.0, amplitude=1400)
        bad_gentle = _create_synthetic_wav(bad_with_gentle_dir / "refs" / "gentle.wav", duration_sec=4.0, amplitude=2000)
        (bad_with_gentle_dir / "manifest.json").write_text(
            json.dumps(
                {
                    "id": "bad_default_with_gentle",
                    "name": "Bad Default With Gentle",
                    "default_emotion": "does_not_exist",
                    "emotions": {
                        "angry": {"audio": "refs/angry.wav", "text": "Angry 2", "lang": "ja"},
                        "gentle": {"audio": "refs/gentle.wav", "text": "Gentle 2", "lang": "ja"},
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        # 3. Non-existent default_emotion, no gentle -> valid package, falls back to first ("angry")
        bad_no_gentle_dir = chars_dir / "bad_default_no_gentle"
        first_angry = _create_synthetic_wav(bad_no_gentle_dir / "refs" / "angry.wav", duration_sec=4.0, amplitude=1600)
        _create_synthetic_wav(bad_no_gentle_dir / "refs" / "cool.wav", duration_sec=4.0, amplitude=2200)
        (bad_no_gentle_dir / "manifest.json").write_text(
            json.dumps(
                {
                    "id": "bad_default_no_gentle",
                    "name": "Bad Default No Gentle",
                    "default_emotion": "does_not_exist",
                    "emotions": {
                        "angry": {"audio": "refs/angry.wav", "text": "Angry 3", "lang": "ja"},
                        "cool": {"audio": "refs/cool.wav", "text": "Cool 3", "lang": "ja"},
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return (
            chars_dir,
            omitted_gentle,
            bad_gentle,
            first_angry,
            (omitted_dir, bad_with_gentle_dir, bad_no_gentle_dir),
        )

    try:
        chars_dir, omitted_gentle, bad_gentle, first_angry, pkg_dirs = _setup_packages()

        for d in pkg_dirs:
            ok, errs = validate_character_package(d)
            assert ok is True, f"Expected {d.name} valid, got errors: {errs}"

        mgr = get_character_manager(chars_dir)
        discovered = mgr.discover_characters()
        assert len(discovered) == 3

        db_path = tmp_path / "compat_fallback.db"
        await init_db(str(db_path))
        async with get_db(str(db_path)) as conn:
            await mgr.sync_with_db(conn)
            profiles = {p.name: p for p in await crud.list_voice_profiles(conn)}

        assert profiles["Omitted Default"].prompt_text == "Gentle 1"
        assert profiles["Bad Default With Gentle"].prompt_text == "Gentle 2"
        assert profiles["Bad Default No Gentle"].prompt_text == "Angry 3"

        for char_id, expected_wav, expected_emo in (
            ("omitted_default", omitted_gentle, "gentle"),
            ("bad_default_with_gentle", bad_gentle, "gentle"),
            ("bad_default_no_gentle", first_angry, "angry"),
        ):
            for query in (None, "unknown_mood"):
                res = mgr.resolve_emotion_audio_path(char_id, query)
                assert res is not None
                assert _same_resolved_path(res["ref_audio_path"], expected_wav)
                assert res["emotion"] == expected_emo

        resolver = VoiceProfileResolver()
        async with get_db(str(db_path)) as conn:
            for prof in profiles.values():
                await crud.update_voice_profile(
                    conn, prof.id, VoiceProfileUpdate(ref_audio_path="", prompt_text="")
                )
        resolver.invalidate(db_path=str(db_path))

        ctx_1 = await resolver.resolve_context(
            db_path=str(db_path), profile_id=profiles["Omitted Default"].id
        )
        assert _same_resolved_path(ctx_1.ref_audio_path, omitted_gentle)

        ctx_2 = await resolver.resolve_context(
            db_path=str(db_path), profile_id=profiles["Bad Default With Gentle"].id
        )
        assert _same_resolved_path(ctx_2.ref_audio_path, bad_gentle)

        ctx_3 = await resolver.resolve_context(
            db_path=str(db_path), profile_id=profiles["Bad Default No Gentle"].id
        )
        assert _same_resolved_path(ctx_3.ref_audio_path, first_angry)
    finally:
        CharacterManager.reset_instance()
        get_settings.cache_clear()



