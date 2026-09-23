"""
Empirical Challenge & Verification Test Suite for Character Packages.
Evaluates:
1. Discovery and Pydantic schema validation of all 13 character packages.
2. Strict string equality between manifest.json["system_prompt"] and system_prompt.txt for all 5 new characters.
3. Presence of all 4 affection stages (Level 0~20, 21~50, 51~80, 81~100) and 7 standard emotions in prompts.
4. Audio references in refs/ with unique MD5s and durations in [3.0s, 10.0s].
5. Dynamic emotion resolution and anti-hijacking across all character packages.
6. Canon lore integrity (Amane step-sister canon correction).
7. Global audio uniqueness across all 13 packages (91 audio tracks).
8. SQLite persistence synchronization roundtrip for all 13 characters.
"""

import hashlib
import json
from pathlib import Path
import aiosqlite
import pytest

from galgame2voice.config import get_settings
from galgame2voice.services.character_manager import (
    CharacterManager,
    CharacterManifest,
    get_character_manager,
)
from galgame2voice.services.emotion_references import (
    resolve_emotion_reference,
    normalize_emotion,
)
from galgame2voice.services.tts_service import TtsService

NEW_FIVE_CHARACTERS = ["白雪乃爱", "谷风天音", "小云雀来海", "星河辉耶", "高楯欧丽叶"]
ALL_THIRTEEN_CHARACTERS = [
    "四季夏目", "明月栞那", "西园寺风莉", "三司绫濑",
    "二条院羽月", "在原七海", "常陆茉子", "丛雨",
    "白雪乃爱", "谷风天音", "小云雀来海", "星河辉耶", "高楯欧丽叶"
]
STANDARD_EMOTIONS = ["gentle", "shy", "happy", "tsundere", "cool", "sad", "angry"]
AFFECTION_STAGES = ["0~20", "21~50", "51~80", "81~100"]


def test_discovery_and_pydantic_validation_all_13_packages():
    """Validates that all 13 packages are discovered and satisfy CharacterManifest schema."""
    settings = get_settings()
    mgr = CharacterManager(settings.characters_dir)
    discovered = mgr.discover_characters()

    assert len(discovered) == 13, f"Expected 13 discovered packages, got {len(discovered)}: {[p.name for p in discovered]}"

    discovered_names = {pkg.name for pkg in discovered}
    for expected_name in ALL_THIRTEEN_CHARACTERS:
        assert expected_name in discovered_names, f"Expected character '{expected_name}' not found in discovered packages"

    for pkg in discovered:
        # Pydantic validation of manifest
        manifest_path = pkg.folder_path / "manifest.json"
        assert manifest_path.exists(), f"manifest.json missing for {pkg.name}"
        raw_json = json.loads(manifest_path.read_text(encoding="utf-8"))
        validated = CharacterManifest.model_validate(raw_json)

        assert validated.id == pkg.id
        assert validated.name == pkg.name
        assert pkg.is_valid is True, f"Package {pkg.name} marked invalid: {pkg.validation_errors}"
        assert len(pkg.validation_errors) == 0, f"Package {pkg.name} has errors: {pkg.validation_errors}"

        # Weights check
        for w_field in ("gpt_weights", "sovits_weights"):
            w_file = pkg.resolve_weight_path(w_field)
            assert w_file, f"Weight {w_field} unresolved for {pkg.name}"
            w_path = settings.project_root / w_file
            assert w_path.is_file(), f"Weight file {w_path} does not exist for {pkg.name}"
            assert w_path.stat().st_size > 100 * 1024 * 1024, f"Weight {w_path} size <= 100MB for {pkg.name}"


def test_strict_string_equality_manifest_and_system_prompt_five_characters():
    """Asserts strict string equality between manifest.json['system_prompt'] and system_prompt.txt for all 5 new characters."""
    settings = get_settings()

    for char_name in NEW_FIVE_CHARACTERS:
        char_dir = settings.characters_dir / char_name
        manifest_path = char_dir / "manifest.json"
        txt_path = char_dir / "system_prompt.txt"

        assert manifest_path.is_file(), f"manifest.json missing for {char_name}"
        assert txt_path.is_file(), f"system_prompt.txt missing for {char_name}"

        manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest_prompt = manifest_data.get("system_prompt", "")
        txt_prompt = txt_path.read_text(encoding="utf-8")

        # Strict string equality assertion
        assert manifest_prompt == txt_prompt, (
            f"Strict string mismatch for {char_name}!\n"
            f"manifest length: {len(manifest_prompt)}, txt length: {len(txt_prompt)}\n"
            f"manifest snippet: {repr(manifest_prompt[:60])}...\n"
            f"txt snippet:      {repr(txt_prompt[:60])}..."
        )


def test_affection_stages_and_emotions_in_prompts_five_characters():
    """Asserts that each prompt contains all 4 affection stages and 7 standard emotions."""
    settings = get_settings()

    for char_name in NEW_FIVE_CHARACTERS:
        txt_path = settings.characters_dir / char_name / "system_prompt.txt"
        prompt = txt_path.read_text(encoding="utf-8")

        # 4 Affection stages check
        for stage in AFFECTION_STAGES:
            assert stage in prompt, f"Affection stage '{stage}' missing from prompt for {char_name}"

        # 7 Standard emotions check
        for emo in STANDARD_EMOTIONS:
            assert emo in prompt, f"Standard emotion '{emo}' missing from prompt for {char_name}"

        # JSON TTS output format contract check
        assert '"tts"' in prompt, f'JSON TTS contract missing "tts" key in prompt for {char_name}'
        assert '"speed"' in prompt, f'JSON TTS contract missing "speed" in prompt for {char_name}'
        assert '"temp"' in prompt, f'JSON TTS contract missing "temp" in prompt for {char_name}'
        assert '"emotion"' in prompt, f'JSON TTS contract missing "emotion" in prompt for {char_name}'
        assert '"chinese"' in prompt, f'JSON TTS contract missing "chinese" in prompt for {char_name}'
        assert '"japanese"' in prompt, f'JSON TTS contract missing "japanese" in prompt for {char_name}'


def test_audio_references_unique_md5_and_duration_bounds():
    """Asserts that each of the 13 characters has 7 audio references in refs/ with unique MD5s and durations in [3.0s, 10.0s]."""
    settings = get_settings()

    for char_name in ALL_THIRTEEN_CHARACTERS:
        char_dir = settings.characters_dir / char_name
        refs_dir = char_dir / "refs"
        assert refs_dir.is_dir(), f"refs directory missing for {char_name}"

        manifest_data = json.loads((char_dir / "manifest.json").read_text(encoding="utf-8"))
        emotions_map = manifest_data.get("emotions", {})

        # Exactly 7 emotions defined
        for emo in STANDARD_EMOTIONS:
            assert emo in emotions_map, f"Emotion {emo} missing from manifest for {char_name}"

        seen_md5s = {}
        for emo in STANDARD_EMOTIONS:
            emo_cfg = emotions_map[emo]
            audio_rel = emo_cfg["audio"]
            audio_file = (char_dir / audio_rel).resolve()

            assert audio_file.is_file(), f"Audio file {audio_file} missing for {char_name} emotion {emo}"

            # Check MD5 uniqueness within package
            audio_bytes = audio_file.read_bytes()
            audio_md5 = hashlib.md5(audio_bytes).hexdigest()
            assert audio_md5 not in seen_md5s, (
                f"Duplicate MD5 hash detected for {char_name}: {emo} and {seen_md5s[audio_md5]} "
                f"share hash {audio_md5}"
            )
            seen_md5s[audio_md5] = emo

            # Check duration in [3.0s, 10.0s]
            duration = TtsService.get_audio_duration(audio_file)
            assert duration is not None, f"Unable to decode duration for {audio_file}"
            assert 3.0 <= duration <= 10.0, (
                f"Audio duration for {char_name} ({emo}: {audio_file.name}) "
                f"is {duration:.2f}s, outside required [3.0s, 10.0s]"
            )


def test_dynamic_emotion_resolution_all_thirteen_characters():
    """Verifies that all 13 characters resolve all 7 standard emotions without cross-hijacking."""
    for char_name in ALL_THIRTEEN_CHARACTERS:
        resolved_files = set()
        for emo in STANDARD_EMOTIONS:
            res = resolve_emotion_reference(char_name, emo)
            assert res is not None, f"Failed to resolve emotion '{emo}' for {char_name}"
            assert res["emotion"] == emo, f"Expected emotion {emo}, got {res['emotion']} for {char_name}"
            audio_path = res["ref_audio_path"]
            assert f"{emo}.ogg" in audio_path or f"{emo}." in audio_path, f"Expected {emo} in {audio_path}"
            assert audio_path not in resolved_files, f"Duplicate audio file resolved for {emo} in {char_name}"
            resolved_files.add(audio_path)

        # Anti-hijacking checks
        res_tsun = resolve_emotion_reference(char_name, "tsundere")
        res_angry = resolve_emotion_reference(char_name, "angry")
        assert res_tsun["ref_audio_path"] != res_angry["ref_audio_path"], (
            f"Tsundere and angry must not resolve to the same audio in {char_name}"
        )

        res_shy = resolve_emotion_reference(char_name, "shy")
        res_gentle = resolve_emotion_reference(char_name, "gentle")
        assert res_shy["ref_audio_path"] != res_gentle["ref_audio_path"], (
            f"Shy and gentle must not resolve to the same audio in {char_name}"
        )


def test_tanikaze_amane_non_blood_sister_canon_correction():
    """Verifies that Tanikaze Amane's persona explicitly identifies as step-sister (义妹/义理之妹) and not biological sister."""
    settings = get_settings()
    char_dir = settings.characters_dir / "谷风天音"
    manifest_data = json.loads((char_dir / "manifest.json").read_text(encoding="utf-8"))
    desc = manifest_data.get("description", "")
    prompt = manifest_data.get("system_prompt", "")

    # Must specify non-blood / step-sister
    assert ("义妹" in desc or "义理" in desc), f"Amane description missing '义妹' / '义理': {desc}"
    assert "亲妹" not in desc and "同胞亲妹妹" not in desc, f"Amane description erroneously claims biological sister: {desc}"

    assert ("义理妹妹" in prompt or "义妹" in prompt or "非血缘" in prompt or "重组家庭" in prompt), (
        "Amane prompt missing non-blood step-sister canon identification"
    )
    assert "同胞亲生" not in prompt and "亲妹妹" not in prompt, (
        "Amane prompt erroneously claims biological sister"
    )


def test_global_audio_uniqueness_across_all_91_tracks():
    """Stress-test verifying that all 91 reference audio tracks across all 13 characters are globally unique."""
    settings = get_settings()
    global_hashes = {}
    total_tracks = 0

    for char_name in ALL_THIRTEEN_CHARACTERS:
        char_dir = settings.characters_dir / char_name
        manifest_data = json.loads((char_dir / "manifest.json").read_text(encoding="utf-8"))
        emotions = manifest_data.get("emotions", {})

        for emo in STANDARD_EMOTIONS:
            total_tracks += 1
            audio_file = char_dir / emotions[emo]["audio"]
            assert audio_file.is_file(), f"Missing audio file {audio_file}"
            file_hash = hashlib.md5(audio_file.read_bytes()).hexdigest()

            assert file_hash not in global_hashes, (
                f"Global audio hash collision! {char_name}:{emo} shares hash {file_hash} "
                f"with {global_hashes[file_hash]}"
            )
            global_hashes[file_hash] = f"{char_name}:{emo}"

    assert total_tracks == 91, f"Expected 91 total audio tracks, verified {total_tracks}"
    assert len(global_hashes) == 91, f"Expected 91 unique MD5 hashes, got {len(global_hashes)}"


@pytest.mark.asyncio
async def test_character_manager_sync_with_db_all_thirteen_characters(tmp_path):
    """Verifies that DB synchronization works flawlessly for all 13 characters."""
    db_file = tmp_path / "all_13_chars_sync.db"
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
        synced_count = await mgr.sync_with_db(conn)

        assert synced_count == 13, f"Expected 13 synced characters, got {synced_count}"

        cur = await conn.execute("SELECT name, ref_audio_path, is_default FROM voice_profiles;")
        rows = await cur.fetchall()
        assert len(rows) == 13, f"Expected 13 rows in voice_profiles, got {len(rows)}"

        db_names = {r["name"] for r in rows}
        for expected in ALL_THIRTEEN_CHARACTERS:
            assert expected in db_names, f"Character {expected} missing from synced DB rows"

        default_rows = [r for r in rows if r["is_default"] == 1]
        assert len(default_rows) == 1, f"Expected exactly 1 default profile, found {len(default_rows)}"
        assert default_rows[0]["name"] == "四季夏目", f"Expected 四季夏目 to be default, got {default_rows[0]['name']}"
