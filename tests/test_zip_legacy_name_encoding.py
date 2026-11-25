"""Zips made by Windows Explorer store names in the local code page without
the UTF-8 flag.

A Japanese Windows zip (cp932) used to be decoded as GBK, so files landed
under garbled names and the manifest's reference audio was "missing". On
Windows, zipfile also turns every 0x5C byte of such a name into "/", and 0x5C
is the second byte of characters such as 黒 (GBK) and ソ (cp932), which split
those names into bogus folders.
"""

from __future__ import annotations

import io
import json
import zipfile

import pytest

from galgame2voice.services.character_package_import import extract_archive_safely


def _legacy_zip(entries: dict[str, bytes], enc: str) -> bytes:
    """A zip whose names are stored in `enc` bytes without the UTF-8 flag."""
    buf = io.BytesIO()
    swaps = []
    with zipfile.ZipFile(buf, "w") as zf:
        for i, (name, data) in enumerate(entries.items()):
            raw = name.encode(enc)
            placeholder = (f"{i:02d}" + "q" * len(raw))[: len(raw)]
            swaps.append((placeholder.encode(), raw))
            zf.writestr(placeholder, data)
    blob = buf.getvalue()
    for placeholder, raw in swaps:
        blob = blob.replace(placeholder, raw)
    return blob


def _manifest(audio: str) -> bytes:
    return json.dumps({
        "id": "yuzu", "name": "柚子",
        "emotions": {"gentle": {"audio": audio, "text": "声。", "lang": "ja"}},
    }, ensure_ascii=False).encode()


def _extracted(tmp_path, blob: bytes) -> list[str]:
    archive = tmp_path / "a.zip"
    archive.write_bytes(blob)
    out = tmp_path / "out"
    extract_archive_safely(archive, "zip", out)
    return sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file())


@pytest.mark.parametrize(("enc", "audio"), [
    ("cp932", "refs/通常ボイス.wav"),
    ("cp932", "refs/ソフト.wav"),
    ("gbk", "refs/温柔语气.wav"),
    ("gbk", "refs/黒猫.wav"),
])
@pytest.mark.parametrize("windows", [False, True])
def test_legacy_zip_names_extract_under_their_real_name(tmp_path, monkeypatch, enc, audio, windows):
    blob = _legacy_zip({"yuzu/manifest.json": _manifest(audio), f"yuzu/{audio}": b"RIFF"}, enc)
    if windows:
        monkeypatch.setattr(zipfile.os, "sep", "\\")  # what zipfile sees on Windows
    archive = tmp_path / "a.zip"
    archive.write_bytes(blob)
    out = tmp_path / "out"
    extract_archive_safely(archive, "zip", out)
    monkeypatch.undo()
    names = sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file())
    assert names == sorted(["yuzu/manifest.json", f"yuzu/{audio}"])


def test_utf8_names_without_the_flag_stay_utf8(tmp_path):
    audio = "refs/通常ボイス.wav"
    blob = _legacy_zip({"yuzu/manifest.json": _manifest(audio), f"yuzu/{audio}": b"RIFF"}, "utf-8")
    assert _extracted(tmp_path, blob) == sorted(["yuzu/manifest.json", f"yuzu/{audio}"])
