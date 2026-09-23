"""
Adversarial Challenge & Stress-Test Suite for Character Packages,
Prompt JSON Streaming Parser, Prosody Clamping, and DB Synchronization.
"""

import hashlib
import json
import math
import os
from pathlib import Path
import aiosqlite
import pytest

from galgame2voice.config import get_settings
from galgame2voice.services.character_manager import (
    CharacterManager,
    CharacterManifest,
    CharacterPackage,
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


# ============================================================================
# 1. Prompt JSON Output Parsing & Prosody Clamping Probes
# ============================================================================

def test_prosody_bounds_constants():
    """Verify prosody clamping bounds adhere strictly to contract."""
    assert DYNAMIC_SPEED_MIN == 0.50
    assert DYNAMIC_SPEED_MAX == 1.50
    assert DYNAMIC_TEMP_MIN == 0.60
    assert DYNAMIC_TEMP_MAX == 1.20


@pytest.mark.parametrize("input_val,expected", [
    # Normal in-range
    (1.0, 1.0),
    (0.50, 0.50),
    (1.50, 1.50),
    (0.85, 0.85),
    (1.2345, 1.2345),
    # Underflow clamping
    (0.49, 0.50),
    (0.1, 0.50),
    (0.0, 0.50),
    (-5.0, 0.50),
    # Overflow clamping
    (1.51, 1.50),
    (2.0, 1.50),
    (99.9, 1.50),
    # Non-numeric / NaN / Inf
    ("invalid", 1.0),
    (None, 1.0),
    (float("nan"), 1.0),
    (float("inf"), 1.0),
    (float("-inf"), 1.0),
])
def test_clamp_dynamic_speed_adversarial(input_val, expected):
    """Stress-test speed clamping with adversarial boundaries and corrupt values."""
    res = clamp_dynamic_speed(input_val)
    assert math.isclose(res, expected, rel_tol=1e-5), f"Speed {input_val} -> {res} != {expected}"
    assert 0.50 <= res <= 1.50


@pytest.mark.parametrize("input_val,expected", [
    # Normal in-range
    (1.0, 1.0),
    (0.60, 0.60),
    (1.20, 1.20),
    (0.95, 0.95),
    # Underflow clamping
    (0.59, 0.60),
    (0.1, 0.60),
    (0.0, 0.60),
    (-2.0, 0.60),
    # Overflow clamping
    (1.21, 1.20),
    (1.50, 1.20),
    (5.0, 1.20),
    # Non-numeric / NaN / Inf
    ("invalid", 1.0),
    (None, 1.0),
    (float("nan"), 1.0),
    (float("inf"), 1.0),
    (float("-inf"), 1.0),
])
def test_clamp_dynamic_temperature_adversarial(input_val, expected):
    """Stress-test temperature clamping with adversarial boundaries and corrupt values."""
    res = clamp_dynamic_temperature(input_val)
    assert math.isclose(res, expected, rel_tol=1e-5), f"Temp {input_val} -> {res} != {expected}"
    assert 0.60 <= res <= 1.20


@pytest.mark.parametrize("emotion_tag", [
    "gentle", "shy", "happy", "tsundere", "cool", "sad", "angry"
])
def test_streaming_parser_resolves_all_standard_emotions(emotion_tag):
    """Verify that StreamingBilingualParser cleanly parses all 7 standard emotions."""
    parser = StreamingBilingualParser()
    json_payload = json.dumps({
        "tts": {
            "speed": 1.1,
            "temp": 0.85,
            "emotion": emotion_tag
        },
        "chinese": "测试台词",
        "japanese": "テストセリフです。"
    }, ensure_ascii=False)

    delta_ch, sentences = parser.feed_chunk(json_payload)
    ch, ja, rem = parser.finalize()

    assert parser.tts_speed == 1.1
    assert parser.tts_temperature == 0.85
    assert parser.tts_emotion == emotion_tag
    assert parser.get_emotion() == emotion_tag
    assert ch == "测试台词"
    assert ja == "テストセリフです。"


def test_streaming_parser_case_insensitive_emotions():
    """Verify emotion resolution is case-insensitive and normalizes synonyms."""
    for raw, expected in [
        ("GENTLE", "gentle"),
        ("Shy", "shy"),
        ("HAPPY", "happy"),
        ("TsUnDeRe", "tsundere"),
        ("COOL", "cool"),
        ("SAD", "sad"),
        ("ANGRY", "angry"),
        ("傲娇", "tsundere"),
        ("害羞", "shy"),
        ("温柔", "gentle"),
        ("开心", "happy"),
        ("高冷", "cool"),
        ("伤心", "sad"),
        ("难过", "sad"),
        ("生气", "angry"),
    ]:
        parser = StreamingBilingualParser()
        json_payload = json.dumps({
            "tts": {"speed": 1.0, "temp": 0.9, "emotion": raw},
            "chinese": "台词",
            "japanese": "セリフです。"
        }, ensure_ascii=False)
        parser.feed_chunk(json_payload)
        parser.finalize()
        assert parser.tts_emotion == expected or parser.get_emotion() == expected, (
            f"Failed to map {raw} to {expected}, got tts_emotion={parser.tts_emotion}, get_emotion={parser.get_emotion()}"
        )


def test_streaming_parser_token_by_token_stream():
    """Simulate single-character token streaming with markdown codeblock wrapping."""
    parser = StreamingBilingualParser()
    full_text = (
        "```json\n"
        "{\n"
        '  "tts": {\n'
        '    "speed": 1.45,\n'
        '    "temp": 0.72,\n'
        '    "emotion": "tsundere"\n'
        '  },\n'
        '  "chinese": "才、才不是为了你特意做的呢！",\n'
        '  "japanese": "べ、別にあんたのために作ったんじゃないんだからね！"\n'
        "}\n"
        "```"
    )

    collected_chinese = []
    collected_japanese = []

    # Stream 2 chars at a time
    for i in range(0, len(full_text), 2):
        chunk = full_text[i:i+2]
        delta_ch, new_ja = parser.feed_chunk(chunk)
        if delta_ch:
            collected_chinese.append(delta_ch)
        if new_ja:
            collected_japanese.extend(new_ja)

    full_ch, full_ja, rem_ja = parser.finalize()
    if rem_ja:
        collected_japanese.extend(rem_ja)

    assert "".join(collected_chinese) == "才、才不是为了你特意做的呢！"
    assert full_ch == "才、才不是为了你特意做的呢！"
    assert full_ja == "べ、別にあんたのために作ったんじゃないんだからね！"
    assert parser.tts_speed == 1.45
    assert parser.tts_temperature == 0.72
    assert parser.tts_emotion == "tsundere"

    # Verify dynamic options output
    opts = parser.get_dynamic_tts_options({"speed": 1.0, "temperature": 0.8}, adaptive_enabled=True)
    assert opts["speed"] == 1.45
    assert opts["temperature"] == 0.72
    assert opts["emotion"] == "tsundere"


def test_streaming_parser_out_of_bounds_clamped_in_stream():
    """Verify that streaming parser clamps extreme LLM dynamic values into [0.5, 1.5] and [0.6, 1.2]."""
    parser = StreamingBilingualParser()
    extreme_json = json.dumps({
        "tts": {
            "speed": 5.0,        # Out of upper bound 1.5
            "temp": 0.1,         # Out of lower bound 0.6
            "emotion": "happy"
        },
        "chinese": "极速高温测试",
        "japanese": "スピードテスト。"
    }, ensure_ascii=False)

    parser.feed_chunk(extreme_json)
    parser.finalize()

    assert parser.tts_speed == 1.50
    assert parser.tts_temperature == 0.60
    assert parser.tts_emotion == "happy"


# ============================================================================
# 2. Database Synchronization & Character Switching Probes
# ============================================================================

@pytest.mark.asyncio
async def test_character_manager_sync_to_db_all_13_packages(tmp_path):
    """
    Empirically test CharacterManager.sync_with_db on a fresh SQLite DB.
    Verifies that all 13 packages are synced, paths are portable, and Natsume is default.
    """
    db_file = tmp_path / "sync_13_test.db"
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
        pkgs = mgr.discover_characters()
        assert len(pkgs) == 13, f"Expected 13 packages, discovered {len(pkgs)}"

        # 1. Initial sync
        synced_count = await mgr.sync_with_db(conn)
        assert synced_count == 13, f"Expected 13 synced rows, got {synced_count}"

        cur = await conn.execute("SELECT * FROM voice_profiles;")
        rows = await cur.fetchall()
        assert len(rows) == 13

        names_in_db = {r["name"] for r in rows}
        expected_13 = {
            "三司绫濑", "丛雨", "二条院羽月", "四季夏目", "在原七海",
            "小云雀来海", "常陆茉子", "明月栞那", "星河辉耶", "白雪乃爱",
            "西园寺风莉", "谷风天音", "高楯欧丽叶"
        }
        assert names_in_db == expected_13, f"DB missing characters: {expected_13 - names_in_db}"

        # Verify default profile: Natsume must be default
        defaults = [r for r in rows if r["is_default"] == 1]
        assert len(defaults) == 1
        assert "夏目" in defaults[0]["name"]

        # Verify portable paths and non-empty prompt texts
        for r in rows:
            assert r["ref_audio_path"], f"Character {r['name']} has empty ref_audio_path"
            assert not Path(r["ref_audio_path"]).is_absolute(), f"Path {r['ref_audio_path']} is absolute!"
            assert r["prompt_text"], f"Character {r['name']} has empty prompt_text"
            assert r["system_prompt"], f"Character {r['name']} has empty system_prompt"

        # 2. Idempotency test: Second sync must NOT modify anything
        synced_second = await mgr.sync_with_db(conn)
        assert synced_second == 0, f"Expected 0 updates on second sync, got {synced_second}"


@pytest.mark.asyncio
async def test_character_manager_sync_preserves_user_edits(tmp_path):
    """Test that sync_with_db preserves user modifications to prompt_text, default flag, and system_prompt."""
    db_file = tmp_path / "user_edit_test.db"
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

        # User modifies Kaguya
        await conn.execute("""
            UPDATE voice_profiles
            SET prompt_text = 'Custom Kaguya Prompt',
                system_prompt = 'Custom Kaguya Lore'
            WHERE name = '星河辉耶';
        """)
        await conn.commit()

        # Re-run sync
        synced = await mgr.sync_with_db(conn)
        assert synced == 0

        cur = await conn.execute("SELECT prompt_text, system_prompt FROM voice_profiles WHERE name = '星河辉耶';")
        row = await cur.fetchone()
        assert row["prompt_text"] == "Custom Kaguya Prompt"
        assert row["system_prompt"] == "Custom Kaguya Lore"


@pytest.mark.asyncio
async def test_character_switching_and_emotion_resolution_all_13(tmp_path):
    """
    Stress-test character switching across all 13 characters.
    Simulates DB default switching, verifies CharacterManager resolution,
    and asserts that all 7 standard emotions resolve cleanly for every character.
    """
    db_file = tmp_path / "switch_test.db"
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

        all_13 = [
            "三司绫濑", "丛雨", "二条院羽月", "四季夏目", "在原七海",
            "小云雀来海", "常陆茉子", "明月栞那", "星河辉耶", "白雪乃爱",
            "西园寺风莉", "谷风天音", "高楯欧丽叶"
        ]

        standard_emotions = ["gentle", "shy", "happy", "tsundere", "cool", "sad", "angry"]

        for char_name in all_13:
            # 1. Switch active character in DB
            await conn.execute("UPDATE voice_profiles SET is_default = 0;")
            await conn.execute("UPDATE voice_profiles SET is_default = 1 WHERE name = ?;", (char_name,))
            await conn.commit()

            cur = await conn.execute("SELECT name FROM voice_profiles WHERE is_default = 1;")
            active_row = await cur.fetchone()
            assert active_row["name"] == char_name

            # 2. Package retrieval
            pkg = mgr.get_character(char_name)
            assert pkg is not None, f"Could not find package for {char_name}"
            assert pkg.is_valid is True, f"Package {char_name} is invalid: {pkg.validation_errors}"

            # 3. Resolve all 7 standard emotions via CharacterManager and emotion_references
            seen_audio_paths = set()
            for emo in standard_emotions:
                res = mgr.resolve_emotion_audio_path(char_name, emo)
                assert res is not None, f"Failed to resolve emotion '{emo}' for {char_name}"
                ref_path = Path(res["ref_audio_path"])
                assert ref_path.exists(), f"Audio file {ref_path} does not exist for {char_name} [{emo}]"
                assert res["prompt_text"], f"Prompt text empty for {char_name} [{emo}]"
                assert res["prompt_lang"] == "ja"

                # Ensure each emotion points to a distinct physical audio file within the character
                seen_audio_paths.add(str(ref_path.resolve()))

            assert len(seen_audio_paths) == 7, (
                f"Character {char_name} has duplicate audio paths across emotions: {len(seen_audio_paths)} != 7"
            )


# ============================================================================
# 3. Assert All 13 Packages are Valid & Compliant
# ============================================================================

def test_all_13_packages_discovered_and_valid():
    """Assert that exactly 13 character packages exist and all have is_valid == True."""
    mgr = CharacterManager(get_settings().characters_dir)
    pkgs = mgr.discover_characters()

    assert len(pkgs) == 13, f"Expected 13 packages, discovered {len(pkgs)}"

    for pkg in pkgs:
        assert pkg.is_valid is True, f"Package {pkg.name} is invalid: {pkg.validation_errors}"
        assert len(pkg.validation_errors) == 0, f"Errors in {pkg.name}: {pkg.validation_errors}"


def test_all_13_packages_manifest_schema_and_prompts():
    """
    Assert that manifest.json validates under CharacterManifest schema.
    For the 5 updated Tenshi Souzou heroines, assert strict equality with system_prompt.txt.
    For all packages, assert non-empty system prompt is available.
    """
    mgr = CharacterManager(get_settings().characters_dir)
    pkgs = mgr.discover_characters()

    tenshi_souzou_5 = {"白雪乃爱", "谷风天音", "小云雀来海", "星河辉耶", "高楯欧丽叶"}

    for pkg in pkgs:
        manifest_file = pkg.folder_path / "manifest.json"
        assert manifest_file.exists(), f"manifest.json missing in {pkg.name}"

        with open(manifest_file, "r", encoding="utf-8") as f:
            data = json.load(f)

        manifest = CharacterManifest.model_validate(data)
        assert manifest.name == pkg.name

        # Package system prompt should be non-empty
        assert pkg.system_prompt.strip(), f"System prompt empty for package {pkg.name}"

        # Tenshi Souzou 5 heroines must have synchronized manifest['system_prompt'] == system_prompt.txt
        if pkg.name in tenshi_souzou_5:
            prompt_file = pkg.folder_path / "system_prompt.txt"
            assert prompt_file.exists(), f"system_prompt.txt missing in {pkg.name}"
            txt_content = prompt_file.read_text(encoding="utf-8")
            assert data.get("system_prompt") == txt_content, (
                f"Manifest system_prompt out of sync with system_prompt.txt in {pkg.name}"
            )


def test_all_13_packages_audio_duration_and_md5_uniqueness():
    """
    Assert that all reference audios across all 13 packages have durations in [3.0s, 10.0s]
    and zero duplicate MD5 hashes within any character package.
    """
    mgr = CharacterManager(get_settings().characters_dir)
    pkgs = mgr.discover_characters()

    for pkg in pkgs:
        manifest = pkg.manifest
        assert len(manifest.emotions) >= 7, f"Package {pkg.name} has fewer than 7 emotions"

        hashes = {}
        for emo_name, emo_cfg in manifest.emotions.items():
            audio_path = pkg.resolve_audio_path(emo_cfg.audio)
            assert audio_path is not None and audio_path.exists(), (
                f"Audio path {emo_cfg.audio} in {pkg.name} not found"
            )

            # Check duration [3.0s, 10.0s]
            duration = TtsService.get_audio_duration(audio_path)
            assert 3.0 <= duration <= 10.0, (
                f"Audio {audio_path.name} in {pkg.name} [{emo_name}] duration {duration:.2f}s not in [3.0, 10.0]"
            )

            # Check MD5 uniqueness
            with open(audio_path, "rb") as f:
                digest = hashlib.md5(f.read()).hexdigest()
            assert digest not in hashes, (
                f"Duplicate audio MD5 in {pkg.name}: {emo_name} duplicates {hashes[digest]}"
            )
            hashes[digest] = emo_name


def test_tenshi_souzou_5_heroines_lore_and_contract():
    """
    Forensic audit of the 5 Tenshi Souzou heroines prompts:
    - 白雪乃爱: Boku-musume, 谷风李空, wings/halo, ice cream, heavenly punishment, 4 stages
    - 谷风天音: 义妹 (non-blood step-sister), 谷风李空, couch potato, hem tugging, 4 stages
    - 小云雀来海: Gyaru childhood friend, 谷风李空, 4 stages, micro-actions
    - 星河辉耶: Lunar princess / shrine maiden, 谷风李空, 4 stages, micro-actions
    - 高楯欧丽叶: Demon maid/butler, keigo + sarcasm, 谷风李空, 4 stages, micro-actions
    - All 5: Strict JSON output contract with speed (0.5~1.5), temp (0.6~1.2), 7 standard emotions
    """
    characters_dir = get_settings().characters_dir

    heroines = {
        "白雪乃爱": {
            "keywords": ["僕", "李空", "羽翼", "光环", "冰淇淋", "天罰", "Level 0~20", "Level 21~50", "Level 51~80", "Level 81~100"],
            "anti_keywords": ["亲妹妹", "谷风陆"],
        },
        "谷风天音": {
            "keywords": ["義妹", "义理", "再婚", "李空", "裾", "お兄", "Level 0~20", "Level 21~50", "Level 51~80", "Level 81~100"],
            "anti_keywords": ["亲妹妹", "同胞亲妹妹", "谷风陆"],
        },
        "小云雀来海": {
            "keywords": ["李空", "青梅竹马", "0~20", "21~50", "51~80", "81~100"],
            "anti_keywords": ["谷风陆"],
        },
        "星河辉耶": {
            "keywords": ["李空", "大和抚子", "0~20", "21~50", "51~80", "81~100"],
            "anti_keywords": ["谷风陆"],
        },
        "高楯欧丽叶": {
            "keywords": ["李空", "执事", "0~20", "21~50", "51~80", "81~100"],
            "anti_keywords": ["谷风陆"],
        },
    }

    standard_emotions = ["gentle", "shy", "happy", "tsundere", "cool", "sad", "angry"]

    for char_name, reqs in heroines.items():
        prompt_path = characters_dir / char_name / "system_prompt.txt"
        assert prompt_path.exists(), f"system_prompt.txt missing for {char_name}"
        content = prompt_path.read_text(encoding="utf-8")

        # Check positive lore keywords
        for kw in reqs["keywords"]:
            assert kw in content, f"Heroine {char_name} missing expected keyword '{kw}'"

        # Check negative anti-keywords
        for anti in reqs["anti_keywords"]:
            assert anti not in content, f"Heroine {char_name} contains forbidden anti-keyword '{anti}'"

        # Check JSON output contract
        assert "tts" in content
        assert "speed" in content
        assert "temp" in content
        assert "chinese" in content
        assert "japanese" in content

        # Check 7 standard emotions are present in prompt
        for emo in standard_emotions:
            assert emo in content, f"Heroine {char_name} prompt missing standard emotion '{emo}' in contract"

        # Check speed bounds 0.5~1.5 and temp bounds 0.6~1.2 mentioned
        assert "0.5" in content and "1.5" in content
        assert "0.6" in content and "1.2" in content
