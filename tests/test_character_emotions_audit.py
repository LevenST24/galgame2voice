# -*- coding: utf-8 -*-
"""
Automated unit tests asserting complete compliance, non-duplication,
proper duration [3.0s, 10.0s], and manifest consistency for all 13 characters'
7 emotion reference audio clips (91 total).
"""

import hashlib
import json
import struct
import wave
from pathlib import Path
import pytest
from galgame2voice.config import get_settings

EXPECTED_EMOTIONS = ["gentle", "happy", "angry", "sad", "shy", "tsundere", "cool"]

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
                sample_rate = struct.unpack("<I", head[sr_offset:sr_offset+4])[0]
                
                f.seek(0, 2)
                size = f.tell()
                seek_size = min(size, 65536)
                f.seek(size - seek_size)
                tail = f.read(seek_size)
                last_ogg = tail.rfind(b"OggS")
                if last_ogg == -1:
                    return 0.0
                granule = struct.unpack("<q", tail[last_ogg+6:last_ogg+14])[0]
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

def test_all_13_characters_exist():
    settings = get_settings()
    chars_dir = settings.characters_dir
    char_dirs = [d for d in chars_dir.iterdir() if d.is_dir() and (d / "manifest.json").exists()]
    assert len(char_dirs) == 13, f"Expected 13 characters, found {len(char_dirs)}"

def test_all_91_emotions_valid_and_within_duration():
    settings = get_settings()
    chars_dir = settings.characters_dir
    char_dirs = [d for d in chars_dir.iterdir() if d.is_dir() and (d / "manifest.json").exists()]
    
    for cdir in char_dirs:
        manifest = json.loads((cdir / "manifest.json").read_text(encoding="utf-8"))
        emotions = manifest.get("emotions", {})
        seen_md5s = {}
        
        for emo in EXPECTED_EMOTIONS:
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

def test_all_system_prompts_match_manifest():
    settings = get_settings()
    chars_dir = settings.characters_dir
    for cdir in chars_dir.iterdir():
        if cdir.is_dir() and (cdir / "manifest.json").exists():
            sp_file = cdir / "system_prompt.txt"
            assert sp_file.is_file(), f"Missing system_prompt.txt in {cdir.name}"
            manifest = json.loads((cdir / "manifest.json").read_text(encoding="utf-8"))
            assert manifest.get("system_prompt") == sp_file.read_text(encoding="utf-8")
