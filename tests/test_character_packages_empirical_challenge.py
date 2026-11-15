"""
Empirical Challenge & Verification Test Suite for Character Packages.
Evaluates:
1. Discovery and Pydantic schema validation of all 13 character packages.
2. Presence of all 4 affection stages (Level 0~20, 21~50, 51~80, 81~100) and 7 standard emotions in prompts.
3. Audio references in refs/ with unique MD5s and durations in [3.0s, 10.0s].
4. Dynamic emotion resolution and anti-hijacking across all character packages.
5. Canon lore integrity (Amane step-sister canon correction).
6. Global audio uniqueness across all 13 packages (91 audio tracks).
"""

import hashlib
import json
import re
from pathlib import Path
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

TENSHI_5 = ["白雪乃爱", "谷风天音", "小云雀来海", "星河辉耶", "高楯欧丽叶"]
NEW_FIVE_CHARACTERS = ["白雪乃爱", "谷风天音", "小云雀来海", "星河辉耶", "高楯欧丽叶"]
ALL_THIRTEEN_CHARACTERS = [
    "四季夏目", "明月栞那", "西园寺风莉", "三司绫濑",
    "二条院羽月", "在原七海", "常陆茉子", "丛雨",
    "白雪乃爱", "谷风天音", "小云雀来海", "星河辉耶", "高楯欧丽叶"
]
STANDARD_EMOTIONS = ["gentle", "shy", "happy", "tsundere", "cool", "sad", "angry"]
AFFECTION_STAGES = ["0~20", "21~50", "51~80", "81~100"]


@pytest.mark.requires_character_assets
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


@pytest.mark.requires_character_assets
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


@pytest.mark.requires_character_assets
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


@pytest.mark.requires_character_assets
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


@pytest.mark.requires_character_assets
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


@pytest.mark.requires_character_assets
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


@pytest.mark.requires_character_assets
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


@pytest.mark.requires_character_assets
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


@pytest.mark.requires_character_assets
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
