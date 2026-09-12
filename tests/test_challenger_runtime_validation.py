"""
Empirical Adversarial Validation Suite for Tenshi Souzou RE-BOOT! Character Overhaul.
Executed by challenger_character_2_gen2.
"""

import json
import math
import re
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
from galgame2voice.services.streaming_parser import StreamingBilingualParser
from galgame2voice.services.tts_service import TtsService
from galgame2voice.utils.prosody import (
    DYNAMIC_SPEED_MIN,
    DYNAMIC_SPEED_MAX,
    DYNAMIC_TEMP_MIN,
    DYNAMIC_TEMP_MAX,
    clamp_dynamic_speed,
    clamp_dynamic_temperature,
)

TENSHI_5 = ["白雪乃爱", "谷风天音", "小云雀来海", "星河辉耶", "高楯欧丽叶"]
STANDARD_EMOTIONS = {"gentle", "shy", "happy", "tsundere", "cool", "sad", "angry"}


# ============================================================================
# Task 1: Validate Prompt JSON Output Schema & Streaming Parser / Prosody
# ============================================================================

@pytest.mark.parametrize("char_name", TENSHI_5)
def test_prompt_json_example_extraction_and_schema(char_name):
    """
    Extracts the JSON example from each heroine's system prompt and validates
    its structure, keys, values, and adherence to TTS prosody constraints.
    """
    settings = get_settings()
    prompt_file = settings.characters_dir / char_name / "system_prompt.txt"
    assert prompt_file.is_file(), f"system_prompt.txt missing for {char_name}"
    content = prompt_file.read_text(encoding="utf-8")

    # Find the JSON snippet in the prompt
    match = re.search(r'\{"tts":\s*\{[^}]+\},\s*"chinese":\s*"[^"]+",\s*"japanese":\s*"[^"]+"\}', content)
    assert match is not None, f"Could not find valid JSON format example in {char_name} prompt"
    json_str = match.group(0)

    # 1. Parse JSON
    parsed = json.loads(json_str)
    assert "tts" in parsed
    assert "chinese" in parsed
    assert "japanese" in parsed

    tts = parsed["tts"]
    assert "speed" in tts
    assert "temp" in tts
    assert "emotion" in tts

    speed = tts["speed"]
    temp = tts["temp"]
    emotion = tts["emotion"]

    # 2. Assert prosody constraints
    assert isinstance(speed, (int, float))
    assert 0.5 <= speed <= 1.5, f"{char_name} JSON example speed {speed} not in [0.5, 1.5]"

    assert isinstance(temp, (int, float))
    assert 0.6 <= temp <= 1.2, f"{char_name} JSON example temp {temp} not in [0.6, 1.2]"

    assert emotion in STANDARD_EMOTIONS, f"{char_name} JSON example emotion {emotion} not in {STANDARD_EMOTIONS}"


@pytest.mark.parametrize("char_name", TENSHI_5)
def test_streaming_parser_with_prompt_json_complete_and_chunked(char_name):
    """
    Tests feeding each character's prompt JSON through StreamingBilingualParser:
    - Complete chunk
    - Token-by-token (1 char at a time)
    - Markdown fenced code blocks (```json ... ```)
    """
    settings = get_settings()
    content = (settings.characters_dir / char_name / "system_prompt.txt").read_text(encoding="utf-8")
    match = re.search(r'\{"tts":\s*\{[^}]+\},\s*"chinese":\s*"[^"]+",\s*"japanese":\s*"[^"]+"\}', content)
    raw_json = match.group(0)
    expected = json.loads(raw_json)

    # 1. Single chunk test
    parser = StreamingBilingualParser()
    parser.feed_chunk(raw_json)
    ch, ja, _ = parser.finalize()

    assert parser.tts_speed == expected["tts"]["speed"]
    assert parser.tts_temperature == expected["tts"]["temp"]
    assert parser.tts_emotion == expected["tts"]["emotion"]
    assert parser.get_emotion() == expected["tts"]["emotion"]
    assert ch == expected["chinese"]
    assert ja == expected["japanese"]

    # 2. Token-by-token streaming (1 character per chunk)
    parser2 = StreamingBilingualParser()
    for char in raw_json:
        parser2.feed_chunk(char)
    ch2, ja2, _ = parser2.finalize()

    assert parser2.tts_speed == expected["tts"]["speed"]
    assert parser2.tts_temperature == expected["tts"]["temp"]
    assert parser2.tts_emotion == expected["tts"]["emotion"]
    assert ch2 == expected["chinese"]
    assert ja2 == expected["japanese"]

    # 3. Markdown code block wrapping
    parser3 = StreamingBilingualParser()
    md_payload = f"```json\n{raw_json}\n```"
    parser3.feed_chunk(md_payload)
    ch3, ja3, _ = parser3.finalize()

    assert parser3.tts_speed == expected["tts"]["speed"]
    assert parser3.tts_temperature == expected["tts"]["temp"]
    assert parser3.tts_emotion == expected["tts"]["emotion"]
    assert ch3 == expected["chinese"]
    assert ja3 == expected["japanese"]


def test_adversarial_prosody_clamping_and_fallback():
    """
    Adversarially probe prosody clamping against pathological LLM outputs:
    extreme speeds/temps, NaN, Inf, strings, negative values.
    """
    assert clamp_dynamic_speed(0.1) == 0.50
    assert clamp_dynamic_speed(2.5) == 1.50
    assert clamp_dynamic_speed(-10) == 0.50
    assert clamp_dynamic_speed("corrupt") == 1.0
    assert clamp_dynamic_speed(float("nan")) == 1.0

    assert clamp_dynamic_temperature(0.1) == 0.60
    assert clamp_dynamic_temperature(2.5) == 1.20
    assert clamp_dynamic_temperature(-1.0) == 0.60
    assert clamp_dynamic_temperature("corrupt") == 1.0
    assert clamp_dynamic_temperature(float("nan")) == 1.0


# ============================================================================
# Task 2: Test DB Synchronization with CharacterManager
# ============================================================================

def test_character_manager_sync_method_existence():
    """
    Verify the exact method signature for DB synchronization on CharacterManager.
    Documents whether sync_characters_to_db or sync_with_db is implemented.
    """
    mgr = get_character_manager()
    has_sync_with_db = hasattr(mgr, "sync_with_db")
    has_sync_characters_to_db = hasattr(mgr, "sync_characters_to_db")

    assert has_sync_with_db is True, "CharacterManager must implement sync_with_db(conn)"
    # Document finding: sync_characters_to_db is not the actual method name
    assert callable(getattr(mgr, "sync_with_db"))


@pytest.mark.asyncio
async def test_character_manager_sync_roundtrip_and_idempotence(tmp_path):
    """
    Empirically test CharacterManager.sync_with_db on a fresh SQLite DB:
    - 13 rows created
    - Portable relative paths
    - Non-empty prompt_text and system_prompt
    - Default profile is 四季夏目
    - Idempotency on second call (returns 0)
    """
    db_file = tmp_path / "sync_validation.db"
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

        cur = await conn.execute("SELECT * FROM voice_profiles;")
        rows = await cur.fetchall()
        assert len(rows) == 13

        names = {r["name"] for r in rows}
        expected_names = {
            "三司绫濑", "丛雨", "二条院羽月", "四季夏目", "在原七海",
            "小云雀来海", "常陆茉子", "明月栞那", "星河辉耶", "白雪乃爱",
            "西园寺风莉", "谷风天音", "高楯欧丽叶"
        }
        assert names == expected_names

        defaults = [r for r in rows if r["is_default"] == 1]
        assert len(defaults) == 1
        assert "夏目" in defaults[0]["name"]

        for r in rows:
            assert r["ref_audio_path"], f"Empty ref_audio_path for {r['name']}"
            assert not Path(r["ref_audio_path"]).is_absolute(), f"Absolute path stored: {r['ref_audio_path']}"
            assert r["prompt_text"], f"Empty prompt_text for {r['name']}"
            assert r["system_prompt"], f"Empty system_prompt for {r['name']}"

        # Idempotency check
        synced_second = await mgr.sync_with_db(conn)
        assert synced_second == 0, f"Expected 0 on idempotent sync, got {synced_second}"


# ============================================================================
# Task 3: Verify All 13 Characters Discovered Have is_valid == True
# ============================================================================

def test_all_13_characters_discovered_is_valid():
    """
    Verifies that CharacterManager discovers exactly 13 character packages
    and all 13 have is_valid == True with 0 validation errors.
    """
    mgr = CharacterManager(get_settings().characters_dir)
    pkgs = mgr.discover_characters()

    assert len(pkgs) == 13, f"Expected 13 packages, discovered {len(pkgs)}"

    for pkg in pkgs:
        assert pkg.is_valid is True, f"Character package {pkg.name} is invalid: {pkg.validation_errors}"
        assert len(pkg.validation_errors) == 0, f"Validation errors found in {pkg.name}: {pkg.validation_errors}"
        assert pkg.manifest is not None
        assert len(pkg.manifest.emotions) == 7, f"Package {pkg.name} has {len(pkg.manifest.emotions)} emotions != 7"


def test_manifest_and_system_prompt_txt_synchronized_tenshi_5():
    """
    Verifies that manifest.json['system_prompt'] strictly matches system_prompt.txt
    for all 5 Tenshi Souzou heroines.
    """
    settings = get_settings()
    for name in TENSHI_5:
        char_dir = settings.characters_dir / name
        manifest_data = json.loads((char_dir / "manifest.json").read_text(encoding="utf-8"))
        prompt_txt = (char_dir / "system_prompt.txt").read_text(encoding="utf-8")

        assert manifest_data.get("system_prompt") == prompt_txt, (
            f"Manifest system_prompt is not strictly synchronized with system_prompt.txt in {name}"
        )
