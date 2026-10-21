"""Per-work background packs, signed media, and distribution boundaries."""

import json
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit
import zipfile

import httpx
import pytest

from galgame2voice.config import Settings, get_settings
from galgame2voice.main import create_app
from galgame2voice.security import media_urls
from galgame2voice.services.background_manifest import load_background_pack, load_background_packs
from galgame2voice.services.chat_backup import validate_adv_settings


@pytest.fixture
def library(tmp_path, monkeypatch):
    root = tmp_path / "backgrounds"
    pack = root / "cafe"
    pack.mkdir(parents=True)
    for variant in ("day", "evening", "night"):
        (pack / f"{variant}.webp").write_bytes(f"image-{variant}".encode())
    monkeypatch.setenv("BACKGROUNDS_DIR_NAME", str(root))
    monkeypatch.setenv("GALGAME2VOICE_AUTH_DISABLED", "0")
    monkeypatch.setenv("GALGAME2VOICE_CONSOLE_TOKEN", "background-test-token")
    monkeypatch.setattr(media_urls, "_cached_key", b"b" * 32)
    get_settings.cache_clear()
    write_pack(root, "cafe", [entry()], name="喫茶ステラ")
    return root


def entry(**overrides):
    return {"id": "stella", "name": "咖啡店", "variants": {
        "day": "day.webp", "evening": "evening.webp", "night": "night.webp",
    }, **overrides}


def write_pack(root, work, entries, *, name=None, version=1, declared_work=None, default=None):
    """Write <root>/<work>/manifest.json; declared_work overrides the stored work id."""
    folder = root / work
    folder.mkdir(parents=True, exist_ok=True)
    document = {"version": version, "work": work if declared_work is None else declared_work,
                "backgrounds": entries}
    if name is not None:
        document["name"] = name
    if default is not None:
        document["default"] = default
    (folder / "manifest.json").write_text(json.dumps(document), encoding="utf-8")
    return folder


def all_backgrounds(root):
    return [bg for pack in load_background_packs(root) for bg in pack.backgrounds]


def test_background_directory_follows_character_path_overrides(tmp_path, monkeypatch):
    monkeypatch.delenv("BACKGROUNDS_DIR_NAME", raising=False)
    monkeypatch.setenv("GALGAME2VOICE_PROJECT_ROOT", str(tmp_path))
    assert Settings(_env_file=None).backgrounds_dir == tmp_path / "backgrounds"
    monkeypatch.setenv("BACKGROUNDS_DIR_NAME", "custom-backgrounds")
    assert Settings(_env_file=None).backgrounds_dir == tmp_path / "custom-backgrounds"
    monkeypatch.setenv("BACKGROUNDS_DIR_NAME", str(tmp_path / "external"))
    assert Settings(_env_file=None).backgrounds_dir == tmp_path / "external"


@pytest.mark.parametrize("invalid", [
    None, [], {}, {"id": "missing-fields"},
    entry(id="../cafe"), entry(id="咖啡店"), entry(id="a" * 65), entry(id=""), entry(id=1),
    entry(name=""), entry(name="  "), entry(name=None),
    entry(variants={}), entry(variants=[]), entry(variants={"dawn": "day.webp"}),
    entry(variants={"day": "day.webp", "dawn": "day.webp"}),
    entry(variants={"day": 12}), entry(variants={"day": "readme.txt"}),
    entry(variants={"day": "day.webp", "night": "readme.txt"}),
])
def test_pack_skips_invalid_entry_and_keeps_valid_neighbor(library, invalid, caplog):
    write_pack(library, "cafe", [invalid, entry(id="valid_neighbor")])
    assert [bg.id for bg in all_backgrounds(library)] == ["valid_neighbor"]
    assert any(record.levelname == "WARNING" for record in caplog.records)


def test_pack_unique_ids_and_id_boundaries(library, caplog):
    minimal = entry(id="A_-09", name="minimal", variants={"night": "night.webp"})
    write_pack(library, "cafe", [minimal, entry(id="A_-09"), entry(id="a" * 64)])
    backgrounds = all_backgrounds(library)
    assert [bg.id for bg in backgrounds] == ["A_-09", "a" * 64]
    assert backgrounds[0].name == "minimal"
    assert backgrounds[0].key == "cafe/A_-09"
    assert list(backgrounds[0].variants) == ["night"]
    assert any(record.levelname == "WARNING" for record in caplog.records)


@pytest.mark.parametrize("suffix", [".webp", ".png", ".jpg", ".jpeg", ".avif", ".PNG"])
def test_pack_all_supported_image_extensions(library, suffix):
    image = library / "cafe" / f"image{suffix}"
    image.write_bytes(b"image")
    write_pack(library, "cafe", [entry(variants={"day": image.name})])
    assert all_backgrounds(library)[0].variants == {"day": image.resolve()}


@pytest.mark.parametrize("bad_path", [
    "missing.webp", "../outside.webp", "cafe/../../outside.webp",
    "..\\outside.webp", "/outside.webp", "C:/outside.webp", "C:outside.webp", "bad\u0000.webp",
])
def test_pack_drops_missing_and_escaping_variant(library, bad_path, caplog):
    (library / "outside.webp").write_bytes(b"private")
    write_pack(library, "cafe", [entry(variants={"day": bad_path, "night": "night.webp"})])
    backgrounds = all_backgrounds(library)
    assert len(backgrounds) == 1
    assert backgrounds[0].variants == {"night": (library / "cafe" / "night.webp").resolve()}
    assert any(record.levelname == "WARNING" for record in caplog.records)


def test_pack_omits_background_with_no_existing_variants(library):
    assert len(all_backgrounds(library)) == 1
    write_pack(library, "cafe", [entry(variants={"day": "missing.png"})])
    assert load_background_packs(library) == []
    assert all_backgrounds(library) == []


@pytest.mark.parametrize("content", ["{broken", "[]", "null", '{}',
    json.dumps({"version": 2, "work": "cafe", "backgrounds": [entry()]}),
    json.dumps({"version": True, "work": "cafe", "backgrounds": [entry()]}),
    '{"version": 1, "work": "cafe", "backgrounds": {}}', '\ud800',
])
def test_broken_pack_manifest_is_empty(library, content):
    assert len(all_backgrounds(library)) == 1
    (library / "cafe" / "manifest.json").write_bytes(content.encode("utf-8", errors="surrogatepass"))
    assert load_background_packs(library) == []


def test_missing_pack_manifest_is_empty(library):
    assert len(all_backgrounds(library)) == 1
    (library / "cafe" / "manifest.json").unlink()
    assert load_background_packs(library) == []


def test_two_packs_load_and_merge(library):
    beach = library / "beach"
    beach.mkdir()
    (beach / "day.webp").write_bytes(b"beach-day")
    write_pack(library, "beach", [{"id": "shore", "name": "海边", "variants": {"day": "day.webp"}}], name="海辺")
    packs = load_background_packs(library)
    assert [(p.work, p.name) for p in packs] == [("beach", "海辺"), ("cafe", "喫茶ステラ")]
    assert [bg.key for p in packs for bg in p.backgrounds] == ["beach/shore", "cafe/stella"]


@pytest.mark.parametrize("default,expected", [
    (None, "cafe/stella"), ("hall", "cafe/hall"), ("stella", "cafe/stella"),
    ("gone", "cafe/stella"), ("../hall", "cafe/stella"), (7, "cafe/stella"),
])
def test_pack_default_names_the_auto_background(library, default, expected):
    write_pack(library, "cafe", [entry(), entry(id="hall", name="大厅")], name="喫茶ステラ", default=default)
    assert load_background_packs(library)[0].default_key == expected


def test_load_one_pack_by_work(library):
    assert [bg.key for bg in load_background_pack(library, "cafe").backgrounds] == ["cafe/stella"]
    for work in ("missing", "../cafe", "咖啡店", ""):
        assert load_background_pack(library, work) is None


def test_missing_background_root_is_quietly_empty(tmp_path, caplog):
    assert load_background_packs(tmp_path / "no-backgrounds") == []
    assert not [record for record in caplog.records if record.levelname == "WARNING"]


def test_pack_whose_work_differs_from_folder_is_skipped(library, caplog):
    write_pack(library, "cafe", [entry()], declared_work="other_work")
    assert load_background_packs(library) == []
    assert any(record.levelname == "WARNING" for record in caplog.records)


def test_pack_with_invalid_work_folder_name_is_skipped(library, caplog):
    write_pack(library, "咖啡店", [entry()], name="不该加载")
    assert [p.work for p in load_background_packs(library)] == ["cafe"]
    assert any(record.levelname == "WARNING" for record in caplog.records)


def test_root_level_manifest_is_no_longer_read(library):
    (library / "cafe" / "manifest.json").unlink()
    (library / "manifest.json").write_text(json.dumps({"version": 1, "backgrounds": [entry()]}), encoding="utf-8")
    assert load_background_packs(library) == []


@pytest.mark.parametrize("key", ["", "none", "cafe/stella", "A_-09/a", "a" * 64 + "/" + "b" * 64])
def test_settings_accept_global_background_key(key):
    assert validate_adv_settings({"advBackground": key}) == {"advBackground": key}


@pytest.mark.parametrize("bad", [
    "cafe", "cafe/stella/day", "cafe//stella", "/stella", "cafe/", "../stella",
    "咖啡店/stella", "cafe/" + "a" * 65, "cafe/stella\n", True, 1, None,
])
def test_settings_reject_malformed_background_key(bad):
    assert "advBackground" not in validate_adv_settings({"advBackground": bad})


@pytest.fixture
async def client(library):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app()), base_url="http://test") as client:
        yield client


AUTH = {"Authorization": "Bearer background-test-token"}


async def test_list_requires_auth_and_returns_only_existing_signed_variants(client, library):
    assert (await client.get("/api/backgrounds")).status_code == 401
    (library / "cafe" / "evening.webp").unlink()
    response = await client.get("/api/backgrounds", headers=AUTH)
    assert response.status_code == 200
    body = response.json()
    assert body["works"] == [{"id": "cafe", "name": "喫茶ステラ", "default": body["backgrounds"][0]["key"]}]
    backgrounds = body["backgrounds"]
    assert len(backgrounds) == 1
    bg = backgrounds[0]
    assert {k: v for k, v in bg.items() if k != "variants"} == {
        "key": "cafe/stella", "work": "cafe", "id": "stella", "name": "咖啡店",
    }
    assert set(bg["variants"]) == {"day", "night"}
    for variant, url in bg["variants"].items():
        parsed = urlsplit(url)
        params = parse_qs(parsed.query)
        assert parsed.path == f"/api/media/background/cafe/stella/{variant}"
        assert int(params["exp"][0]) == media_urls.compute_bucketed_expiry()
        assert media_urls.verify_media_url_signature(parsed.path, params["exp"][0], params.get("v", [""])[0], params["sig"][0])
        media = await client.get(url)  # Signed images work without a console token.
        assert media.status_code == 200
        assert media.content == f"image-{variant}".encode()
        assert media.headers["content-type"] == "image/webp"
        assert media.headers["cache-control"].startswith("private, max-age=")
    (library / "cafe" / "manifest.json").write_text("broken")
    assert (await client.get("/api/backgrounds", headers=AUTH)).json() == {"works": [], "backgrounds": []}


async def test_list_merges_multiple_work_packs(client, library):
    beach = library / "beach"
    beach.mkdir()
    (beach / "day.webp").write_bytes(b"beach-day")
    write_pack(library, "beach", [{"id": "shore", "name": "海边", "variants": {"day": "day.webp"}}], name="海辺")
    body = (await client.get("/api/backgrounds", headers=AUTH)).json()
    assert [w["id"] for w in body["works"]] == ["beach", "cafe"]
    assert [bg["key"] for bg in body["backgrounds"]] == ["beach/shore", "cafe/stella"]
    url = body["backgrounds"][0]["variants"]["day"]
    assert urlsplit(url).path == "/api/media/background/beach/shore/day"
    assert (await client.get(url)).content == b"beach-day"


@pytest.mark.parametrize("kind", ["missing", "tampered", "unicode", "expiry-tampered", "version-tampered", "expired", "bad-exp", "path-tampered"])
async def test_media_rejects_invalid_signatures(client, kind):
    path = "/api/media/background/cafe/stella/day"
    url = media_urls.sign_media_url(path, now=1 if kind == "expired" else None)
    parsed = urlsplit(url)
    params = {k: values[0] for k, values in parse_qs(parsed.query).items()}
    if kind == "missing":
        params = {}
    elif kind == "tampered":
        params["sig"] = "0" * 64
    elif kind == "unicode":
        params["sig"] = "非" * 64
    elif kind == "expiry-tampered":
        params["exp"] = str(int(params["exp"]) + 3600)
    elif kind == "version-tampered":
        params["v"] = "tampered"
    elif kind == "bad-exp":
        params["exp"] = "bad"
    elif kind == "path-tampered":
        path = "/api/media/background/cafe/stella/night"
    assert (await client.get(f"{path}?{urlencode(params)}")).status_code == 403


@pytest.mark.parametrize("work,bg_id,variant", [
    ("cafe", "unknown", "day"), ("unknown", "stella", "day"),
    ("cafe", "stella", "dawn"), ("cafe", "stella", "DAY"), ("cafe", "stella", "missing"),
])
async def test_media_rejects_unknown_work_id_or_variant_even_when_signed(client, work, bg_id, variant):
    assert (await client.get(media_urls.sign_media_url("/api/media/background/cafe/stella/day"))).status_code == 200
    response = await client.get(media_urls.sign_media_url(f"/api/media/background/{work}/{bg_id}/{variant}"))
    assert response.status_code == 404


@pytest.mark.parametrize("path", [
    "/api/media/background/%2e%2e/stella/day", "/api/media/background/cafe/%2e%2e/day",
    "/api/media/background/cafe/stella/%2e%2e", "/api/media/background/cafe%2f..%2fsecret/stella/day",
    "/api/media/background/cafe/stella/day%5c..%5csecret", "/api/media/background/%252e%252e/stella/day",
    "/api/media/background/C%3A/stella/day",
])
async def test_media_rejects_traversal_even_when_signed(client, path):
    assert (await client.get(media_urls.sign_media_url("/api/media/background/cafe/stella/day"))).status_code == 200
    assert (await client.get(media_urls.sign_media_url(path))).status_code in {403, 404}


async def test_media_rechecks_manifest_and_file_after_list(client, library):
    url = (await client.get("/api/backgrounds", headers=AUTH)).json()["backgrounds"][0]["variants"]["day"]
    write_pack(library, "cafe", [entry(variants={"day": "../outside.webp"})])
    (library / "outside.webp").write_bytes(b"private")
    assert (await client.get(url)).status_code == 404
    write_pack(library, "cafe", [entry()])
    (library / "cafe" / "day.webp").unlink()
    assert (await client.get(url)).status_code == 404
    write_pack(library, "cafe", [])
    assert (await client.get(url)).status_code == 404


def test_release_zip_excludes_local_backgrounds(tmp_path, monkeypatch):
    from scripts import package_release

    static = tmp_path / "galgame2voice" / "static"
    static.mkdir(parents=True)
    (static / "index.html").write_text("public app")
    library = tmp_path / "backgrounds"
    library.mkdir()
    pack = library / "cafe"
    pack.mkdir()
    (pack / "private.avif").write_bytes(b"private image")
    write_pack(library, "cafe", [entry()])
    monkeypatch.setattr(package_release, "PROJECT_ROOT", tmp_path)
    assert not package_release.should_include(Path("backgrounds/manifest.json"))
    assert not package_release.should_include(Path("backgrounds/cafe/private.avif"))
    archive = package_release.build_release_zip("test-p6")
    with zipfile.ZipFile(archive) as package:
        names = package.namelist()
        assert "galgame2voice-vtest-p6/galgame2voice/static/index.html" in names
        assert not any("backgrounds" in Path(name).parts for name in names)
    root = Path(__file__).resolve().parents[1]
    assert "/backgrounds/" in (root / ".gitignore").read_text().splitlines()
    assert "/backgrounds/" in (root / ".dockerignore").read_text().splitlines()
