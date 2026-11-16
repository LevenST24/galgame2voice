# 本文件检查用户本机 characters/ 目录下的真实角色素材包；CI 中无本地素材时会自动跳过。

import hashlib
import json
import re
import struct
import wave
from pathlib import Path

import aiosqlite
import pytest

from galgame2voice.config import get_settings
from galgame2voice.services.character_manager import (
    CharacterManager,
    CharacterManifest,
)

TENSHI_5 = ["白雪乃爱", "谷风天音", "小云雀来海", "星河辉耶", "高楯欧丽叶"]
ALL_THIRTEEN_CHARACTERS = [
    "四季夏目", "明月栞那", "西园寺风莉", "三司绫濑",
    "二条院羽月", "在原七海", "常陆茉子", "丛雨",
    "白雪乃爱", "谷风天音", "小云雀来海", "星河辉耶", "高楯欧丽叶",
]
STANDARD_EMOTIONS = ["gentle", "shy", "happy", "tsundere", "cool", "sad", "angry"]
AFFECTION_STAGES = ["0~20", "21~50", "51~80", "81~100"]


def _get_audio_duration(path: Path) -> float:
    if not path.is_file():
        return 0.0
    ext = path.suffix.lower()
    if ext == ".ogg":
        try:
            with open(path, "rb") as f:
                head = f.read(200)
                idx = head.find(b"\x01vorbis")
                if idx == -1:
                    return 0.0
                sr_offset = idx + 7 + 4 + 1
                sample_rate = struct.unpack("<I", head[sr_offset:sr_offset + 4])[0]

                f.seek(0, 2)
                size = f.tell()
                seek_size = min(size, 65536)
                f.seek(size - seek_size)
                tail = f.read(seek_size)
                last_ogg = tail.rfind(b"OggS")
                if last_ogg == -1:
                    return 0.0
                granule = struct.unpack("<q", tail[last_ogg + 6:last_ogg + 14])[0]
                if sample_rate > 0:
                    return granule / sample_rate
        except Exception:
            return 0.0
    elif ext == ".wav":
        try:
            with wave.open(str(path), "rb") as wf:
                frames = wf.getnframes()
                rate = wf.getframerate()
                if rate > 0:
                    return float(frames) / float(rate)
        except Exception:
            return 0.0
    return 0.0


def _md5(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


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

    for char_name in TENSHI_5:
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
            "西园寺风莉", "谷风天音", "高楯欧丽叶",
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
        for emo in STANDARD_EMOTIONS:
            assert emo in content, f"Heroine {char_name} prompt missing standard emotion '{emo}' in contract"

        # Check speed bounds 0.5~1.5 and temp bounds 0.6~1.2 mentioned
        assert "0.5" in content and "1.5" in content
        assert "0.6" in content and "1.2" in content


def test_all_91_emotions_valid_and_within_duration():
    settings = get_settings()
    chars_dir = settings.characters_dir
    char_dirs = [d for d in chars_dir.iterdir() if d.is_dir() and (d / "manifest.json").exists()]

    for cdir in char_dirs:
        manifest = json.loads((cdir / "manifest.json").read_text(encoding="utf-8"))
        emotions = manifest.get("emotions", {})
        seen_md5s = {}

        for emo in STANDARD_EMOTIONS:
            assert emo in emotions, f"{cdir.name} missing emotion '{emo}'"
            entry = emotions[emo]
            audio_rel = entry.get("audio")
            text = entry.get("text", "")
            desc = entry.get("description", "")

            assert audio_rel, f"{cdir.name} [{emo}] has no audio path"
            assert text.strip(), f"{cdir.name} [{emo}] has empty transcript text"
            assert desc.strip(), f"{cdir.name} [{emo}] has empty description"

            audio_path = cdir / audio_rel
            assert audio_path.is_file(), f"{cdir.name} [{emo}] audio missing: {audio_path}"
            assert audio_path.stat().st_size > 500, f"{cdir.name} [{emo}] audio file suspiciously small: {audio_path}"

            dur = _get_audio_duration(audio_path)
            assert 3.0 <= dur <= 10.0, f"{cdir.name} [{emo}] duration {dur:.2f}s not in [3.0, 10.0]s"

            f_hash = _md5(audio_path)
            assert f_hash not in seen_md5s, f"{cdir.name} [{emo}] has duplicate audio with [{seen_md5s.get(f_hash)}]"
            seen_md5s[f_hash] = emo
