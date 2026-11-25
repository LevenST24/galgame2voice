"""The character packager CLI keeps installs inside --target-dir."""

import json
import wave

import pytest

from scripts.tools.character_packager import cmd_install, cmd_pack


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


@pytest.mark.parametrize("name,folder", [
    ("../../escaped", "escaped"),
    ("a:b", "a_b"),
    ("..", "character_package"),
])
def test_install_puts_the_package_in_one_folder_inside_the_target(tmp_path, name, folder):
    out = tmp_path / "dist" / "pkg.zip"
    assert cmd_pack(_package(tmp_path, name), out) == 0
    target = tmp_path / "a" / "b" / "characters"

    assert cmd_install(out, target) == 0
    assert (target / folder / "manifest.json").is_file()
    assert sorted(p.name for p in target.iterdir()) == [folder]
    assert not (tmp_path / "escaped").exists()
