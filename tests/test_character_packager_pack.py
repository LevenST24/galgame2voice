"""The character packager CLI packs a folder without its own output zip."""

import json
import wave
import zipfile

from scripts.tools.character_packager import cmd_pack


def _wav(path, seconds=4):
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\x01\x00" * 8000 * seconds)


def _package(root, name):
    pkg = root / "src"
    pkg.mkdir()
    _wav(pkg / "gentle.wav")
    (pkg / "manifest.json").write_text(json.dumps({
        "id": "pkg", "name": name,
        "emotions": {"gentle": {"audio": "gentle.wav", "text": "x", "lang": "ja"}},
    }), encoding="utf-8")
    return pkg


def test_packing_into_the_package_folder_leaves_the_zip_out(tmp_path):
    pkg = _package(tmp_path, "Ann")
    out = pkg / "ann.zip"

    for _ in range(2):  # a second pack over the first must not nest it either
        assert cmd_pack(pkg, out) == 0
        assert sorted(zipfile.ZipFile(out).namelist()) == ["gentle.wav", "manifest.json"]
