"""Portrait metadata compatibility without modifying installed character assets."""

import json
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from galgame2voice.routers import characters


@pytest.fixture
def portrait_package(tmp_path, monkeypatch):
    folder = tmp_path / "character"
    portrait = folder / "portrait"
    portrait.mkdir(parents=True)
    package = SimpleNamespace(folder=folder, manifest=SimpleNamespace(id="demo", name="Demo"))
    monkeypatch.setattr(characters, "get_character_manager", lambda: SimpleNamespace(get_character=lambda _: package))
    return portrait


def write_legacy(root, *, missing=False):
    costume = root / "casual"
    costume.mkdir()
    (costume / "gentle.png").write_bytes(b"portrait")
    (costume / "happy.png").write_bytes(b"portrait")
    manifest = {
        "default_costume": "casual",
        "costumes": [{"id": "casual", "name": "Casual", "icon": "C"}],
        "sprites": {"casual": {
            "gentle": {"file": "/static/characters/demo/casual/gentle.png", "description": "Calm"},
            "happy": {"file": "happy.png"},
        }},
    }
    if missing:
        manifest["sprites"]["casual"]["sad"] = {"file": "missing.png"}
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def test_existing_emotion_png_package_is_available(portrait_package):
    write_legacy(portrait_package)
    data = characters._resolve_character_portrait("Demo", 7)
    assert data is not None, "existing PNG portraits were ignored because expressions.json was absent"
    assert data["enabled"] is True
    assert data["default_costume"] == "casual"
    url = urlsplit(data["sprites"]["casual"]["gentle"]["file"])
    assert url.path == "/api/characters/7/portrait/file/casual/gentle.png"
    assert url.query.startswith("v=")
    assert data["expression_sets"]["happy"][0]["id"] == "happy"
    assert isinstance(data["faces"]["happy"]["parts"], list)


def test_missing_legacy_images_are_not_advertised(portrait_package):
    write_legacy(portrait_package, missing=True)
    data = characters._resolve_character_portrait("Demo", 7)
    assert data is not None
    assert "sad" not in data["sprites"]["casual"]
    assert all(entry["id"] in data["faces"] for pool in data["expression_sets"].values() for entry in pool)


@pytest.mark.parametrize("contents", ["[]", "{", '{"costumes": [1]}'])
def test_bad_numbered_index_falls_back_to_valid_legacy(portrait_package, contents):
    write_legacy(portrait_package)
    (portrait_package / "expressions.json").write_text(contents, encoding="utf-8")
    data = characters._resolve_character_portrait("Demo", 7)
    assert data is not None
    assert urlsplit(data["sprites"]["casual"]["happy"]["file"]).path.endswith("/happy.png")


def test_numbered_differentials_keep_priority(portrait_package):
    write_legacy(portrait_package)
    (portrait_package / "casual" / "01.webp").write_bytes(b"numbered portrait")
    (portrait_package / "expressions.json").write_text(json.dumps({
        "default_costume": "casual", "pose": "a",
        "costumes": {"casual": {"name": "Numbered", "sprites": {"01": {"file": "01.webp"}}}},
        "faces": {"01": {"label": "Calm", "parts": [], "blush": None}},
        "expression_sets": {"gentle": [{"id": "01", "strength": 2}]},
    }), encoding="utf-8")
    data = characters._resolve_character_portrait("Demo", 7)
    assert data["costumes"][0]["name"] == "Numbered"
    assert data["expression_sets"]["gentle"][0]["id"] == "01"
    assert urlsplit(data["sprites"]["casual"]["01"]["file"]).path.endswith("/01.webp")


def test_reexported_image_gets_a_new_browser_cache_url(portrait_package):
    write_legacy(portrait_package)
    first = characters._resolve_character_portrait("Demo", 7)
    (portrait_package / "casual" / "gentle.png").write_bytes(b"corrected portrait artwork")
    second = characters._resolve_character_portrait("Demo", 7)
    assert first["sprites"]["casual"]["gentle"]["file"] != second["sprites"]["casual"]["gentle"]["file"]
    assert first["sprites"]["casual"]["happy"]["file"] == second["sprites"]["casual"]["happy"]["file"]


def test_external_costume_paths_cannot_be_imported(portrait_package):
    write_legacy(portrait_package)
    outside = portrait_package.parent / "outside"
    outside.mkdir()
    (outside / "gentle.png").write_bytes(b"not a character portrait")
    (portrait_package / "manifest.json").write_text(json.dumps({
        "costumes": [{"id": "../outside"}],
        "sprites": {"../outside": {"gentle": {"file": "gentle.png"}}},
    }), encoding="utf-8")
    assert characters._resolve_character_portrait("Demo", 7) is None
