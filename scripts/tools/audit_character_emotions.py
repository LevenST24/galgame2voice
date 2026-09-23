#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Character Emotions Diagnostic and Verification Tool for galgame2voice.
Inspects all installed character packages in characters/ and validates:
- Manifest JSON structure and emotion mappings
- Audio file existence and format integrity (.ogg, .wav)
- Audio duration requirements (3.0s ~ 10.0s for optimal GPT-SoVITS cloning)
- Uniqueness of reference audios across emotion slots within each character
"""

import argparse
import hashlib
import json
from pathlib import Path
import struct
import sys
from typing import Dict, List, Tuple
import wave

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
CHARACTERS_DIR = ROOT_DIR / "characters"
REQUIRED_EMOTIONS = ["gentle", "happy", "angry", "sad", "shy", "tsundere", "cool"]


def get_audio_duration(path: Path) -> float:
    """Calculates duration in seconds for .ogg or .wav audio files without external dependencies."""
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
                sample_rate = struct.unpack("<I", head[sr_offset : sr_offset + 4])[0]

                f.seek(0, 2)
                size = f.tell()
                seek_size = min(size, 65536)
                f.seek(size - seek_size)
                tail = f.read(seek_size)
                last_ogg = tail.rfind(b"OggS")
                if last_ogg == -1:
                    return 0.0
                granule = struct.unpack("<q", tail[last_ogg + 6 : last_ogg + 14])[0]
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


def md5_file(path: Path) -> str:
    """Computes MD5 hash of a file."""
    h = hashlib.md5()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def audit_characters(
    chars_dir: Path = CHARACTERS_DIR,
    strict_duration: bool = True,
) -> Tuple[int, int, List[str]]:
    """
    Audits all character packages in chars_dir.
    Returns (total_emotions, passed_emotions, error_messages).
    """
    char_dirs = [d for d in chars_dir.iterdir() if d.is_dir() and (d / "manifest.json").exists()]
    char_dirs.sort(key=lambda d: d.name)

    total_emotions = 0
    passed_emotions = 0
    errors: List[str] = []

    print(f"[*] Auditing {len(char_dirs)} character packages in '{chars_dir}'...")

    for cdir in char_dirs:
        cname = cdir.name
        manifest_path = cdir / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception as exc:
            errors.append(f"[{cname}] Failed to parse manifest.json: {exc}")
            continue

        emotions: Dict = manifest.get("emotions", {})
        seen_md5s: Dict[str, str] = {}

        print(f"\n- Character: {cname} ({manifest.get('name', 'N/A')})")

        for emo in REQUIRED_EMOTIONS:
            total_emotions += 1
            if emo not in emotions:
                msg = f"[{cname}] Missing required emotion: '{emo}'"
                errors.append(msg)
                print(f"  [X] {emo:8s}: {msg}")
                continue

            einfo = emotions[emo]
            audio_rel = einfo.get("audio")
            text = einfo.get("text", "")

            if not audio_rel:
                msg = f"[{cname}] Emotion '{emo}' missing 'audio' field"
                errors.append(msg)
                print(f"  [X] {emo:8s}: {msg}")
                continue

            if not text:
                msg = f"[{cname}] Emotion '{emo}' missing 'text' field"
                errors.append(msg)
                print(f"  [X] {emo:8s}: {msg}")
                continue

            audio_path = cdir / audio_rel
            if not audio_path.is_file():
                msg = f"[{cname}] Reference audio not found: {audio_rel}"
                errors.append(msg)
                print(f"  [X] {emo:8s}: {msg}")
                continue

            file_size = audio_path.stat().st_size
            if file_size < 1000:
                msg = f"[{cname}] Reference audio suspiciously small ({file_size} bytes): {audio_rel}"
                errors.append(msg)
                print(f"  [X] {emo:8s}: {msg}")
                continue

            dur = get_audio_duration(audio_path)
            if strict_duration and not (3.0 <= dur <= 10.0):
                msg = f"[{cname}] Audio duration {dur:.2f}s outside [3.0s, 10.0s]: {audio_rel}"
                errors.append(msg)
                print(f"  [!] {emo:8s}: {dur:5.2f}s | {msg}")
                continue

            file_hash = md5_file(audio_path)
            if file_hash in seen_md5s:
                prior_emo = seen_md5s[file_hash]
                msg = f"[{cname}] Emotion '{emo}' shares identical audio with '{prior_emo}'"
                errors.append(msg)
                print(f"  [X] {emo:8s}: {msg}")
                continue

            seen_md5s[file_hash] = emo
            passed_emotions += 1
            print(f"  [OK] {emo:8s}: {dur:5.2f}s | {audio_rel}")

    return total_emotions, passed_emotions, errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit and verify character emotion profiles.")
    parser.add_argument(
        "--chars-dir",
        type=Path,
        default=CHARACTERS_DIR,
        help="Path to characters directory",
    )
    parser.add_argument(
        "--no-strict-duration",
        action="store_true",
        help="Disable strict 3.0s~10.0s duration validation",
    )
    args = parser.parse_args()

    total, passed, errors = audit_characters(
        chars_dir=args.chars_dir,
        strict_duration=not args.no_strict_duration,
    )

    print("\n" + "=" * 60)
    print(f"Audit Complete: {passed}/{total} emotion slots verified successfully.")
    if errors:
        print(f"Detected {len(errors)} issue(s):")
        for err in errors:
            print(f" - {err}")
        return 1

    print("[SUCCESS] All character emotion reference audio configurations are valid and unique!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
