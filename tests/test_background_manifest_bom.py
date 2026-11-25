"""Windows Notepad saves manifest.json as UTF-8 with a BOM.

Character and portrait manifests already accept it; a background pack saved
that way used to vanish from the background panel without any message.
"""

import json

from galgame2voice.services.background_manifest import load_background_packs


def test_background_manifest_with_bom_is_loaded(tmp_path):
    pack = tmp_path / "backgrounds" / "cafe"
    pack.mkdir(parents=True)
    (pack / "day.webp").write_bytes(b"RIFF....WEBP")
    doc = {"version": 1, "work": "cafe", "name": "喫茶ステラ",
           "backgrounds": [{"id": "stella", "name": "咖啡店", "variants": {"day": "day.webp"}}]}
    (pack / "manifest.json").write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8-sig")
    packs = load_background_packs(tmp_path / "backgrounds")
    assert [p.work for p in packs] == ["cafe"]




def test_bundled_background_manifest_with_bom_is_loaded(tmp_path):
    chars = tmp_path / "characters"
    pack = chars / "yuzu" / "background"
    pack.mkdir(parents=True)
    (pack / "day.webp").write_bytes(b"RIFF....WEBP")
    doc = {"version": 1, "work": "cafe",
           "backgrounds": [{"id": "stella", "name": "咖啡店", "variants": {"day": "day.webp"}}]}
    (pack / "manifest.json").write_text(json.dumps(doc), encoding="utf-8-sig")
    packs = load_background_packs(tmp_path / "backgrounds", chars)
    assert [p.work for p in packs] == ["cafe"]
