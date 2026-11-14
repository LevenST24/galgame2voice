"""
Adversarial Challenge & Stress-Test Suite for Character Packages,
Prompt JSON Streaming Parser, Prosody Clamping, and DB Synchronization.
"""

import json
import math
from pathlib import Path
import aiosqlite
import pytest

from galgame2voice.config import get_settings
from galgame2voice.services.character_manager import CharacterManager
from galgame2voice.services.streaming_parser import StreamingBilingualParser
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


@pytest.mark.parametrize("raw,expected", [
    ("gentle", "gentle"), ("shy", "shy"), ("happy", "happy"), ("tsundere", "tsundere"),
    ("cool", "cool"), ("sad", "sad"), ("angry", "angry"),
    ("GENTLE", "gentle"), ("Shy", "shy"), ("HAPPY", "happy"), ("TsUnDeRe", "tsundere"),
    ("COOL", "cool"), ("SAD", "sad"), ("ANGRY", "angry"),
    ("傲娇", "tsundere"), ("害羞", "shy"), ("温柔", "gentle"), ("开心", "happy"),
    ("高冷", "cool"), ("伤心", "sad"), ("难过", "sad"), ("生气", "angry"),
])
def test_streaming_parser_resolves_all_standard_emotions(raw, expected):
    """Verify that StreamingBilingualParser cleanly parses all 7 standard emotions, case variants, and synonyms."""
    parser = StreamingBilingualParser()
    json_payload = json.dumps({
        "tts": {"speed": 1.1, "temp": 0.85, "emotion": raw},
        "chinese": "测试台词",
        "japanese": "テストセリフです。",
    }, ensure_ascii=False)
    parser.feed_chunk(json_payload)
    ch, ja, _ = parser.finalize()
    assert parser.tts_speed == 1.1 and parser.tts_temperature == 0.85
    assert parser.tts_emotion == expected or parser.get_emotion() == expected
    assert ch == "测试台词" and ja == "テストセリフです。"


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


# ============================================================================
# 2. Database Synchronization & Character Switching Probes
# ============================================================================

@pytest.mark.requires_character_assets
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


@pytest.mark.requires_character_assets
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
