"""Local background manifests, signed media, and distribution boundaries."""

import json
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit
import zipfile

import httpx
import pytest

from galgame2voice.config import Settings, get_settings
from galgame2voice.main import create_app
from galgame2voice.security import media_urls
from galgame2voice.services.background_manifest import load_background_manifest


@pytest.fixture
def library(tmp_path, monkeypatch):
    root = tmp_path / "backgrounds"
    (root / "cafe").mkdir(parents=True)
    for variant in ("day", "evening", "night"):
        (root / "cafe" / f"{variant}.webp").write_bytes(f"image-{variant}".encode())
    monkeypatch.setenv("BACKGROUNDS_DIR_NAME", str(root))
    monkeypatch.setenv("GALGAME2VOICE_AUTH_DISABLED", "0")
    monkeypatch.setenv("GALGAME2VOICE_CONSOLE_TOKEN", "background-test-token")
    monkeypatch.setattr(media_urls, "_cached_key", b"b" * 32)
    get_settings.cache_clear()
    write_manifest(root, [entry()])
    return root


def entry(**overrides):
    return {"id": "cafe", "name": "咖啡店", "source": "喫茶ステラ", "variants": {
        "day": "cafe/day.webp", "evening": "cafe/evening.webp", "night": "cafe/night.webp",
    }, **overrides}


def write_manifest(root, entries):
    (root / "manifest.json").write_text(json.dumps({"version": 1, "backgrounds": entries}), encoding="utf-8")


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
    entry(name=""), entry(name="  "), entry(name=None), entry(source=[]),
    entry(variants={}), entry(variants=[]), entry(variants={"dawn": "cafe/day.webp"}),
    entry(variants={"day": "cafe/day.webp", "dawn": "cafe/day.webp"}),
    entry(variants={"day": 12}), entry(variants={"day": "cafe/readme.txt"}),
    entry(variants={"day": "cafe/day.webp", "night": "cafe/readme.txt"}),
])
def test_manifest_skips_invalid_entry_and_keeps_valid_neighbor(library, invalid, caplog):
    write_manifest(library, [invalid, entry(id="valid_neighbor")])
    assert [bg.id for bg in load_background_manifest(library)] == ["valid_neighbor"]
    assert any(record.levelname == "WARNING" for record in caplog.records)


def test_manifest_unique_ids_optional_source_and_id_boundaries(library, caplog):
    no_source = entry(id="A_-09", name="minimal", variants={"night": "cafe/night.webp"})
    no_source.pop("source")
    write_manifest(library, [no_source, entry(id="A_-09"), entry(id="a" * 64)])
    backgrounds = load_background_manifest(library)
    assert [bg.id for bg in backgrounds] == ["A_-09", "a" * 64]
    assert backgrounds[0].name == "minimal"
    assert backgrounds[0].source == ""
    assert list(backgrounds[0].variants) == ["night"]
    assert any(record.levelname == "WARNING" for record in caplog.records)


@pytest.mark.parametrize("suffix", [".webp", ".png", ".jpg", ".jpeg", ".avif", ".PNG"])
def test_manifest_all_supported_image_extensions(library, suffix):
    image = library / f"image{suffix}"
    image.write_bytes(b"image")
    write_manifest(library, [entry(variants={"day": image.name})])
    assert load_background_manifest(library)[0].variants == {"day": image.resolve()}


@pytest.mark.parametrize("bad_path", [
    "cafe/missing.webp", "../outside.webp", "cafe/../../outside.webp",
    "..\\outside.webp", "/outside.webp", "C:/outside.webp", "C:outside.webp", "bad\u0000.webp",
])
def test_manifest_drops_missing_and_escaping_variant(library, bad_path, caplog):
    (library.parent / "outside.webp").write_bytes(b"private")
    write_manifest(library, [entry(variants={"day": bad_path, "night": "cafe/night.webp"})])
    backgrounds = load_background_manifest(library)
    assert len(backgrounds) == 1
    assert backgrounds[0].variants == {"night": (library / "cafe/night.webp").resolve()}
    assert any(record.levelname == "WARNING" for record in caplog.records)


def test_manifest_omits_background_with_no_existing_variants(library):
    assert len(load_background_manifest(library)) == 1
    write_manifest(library, [entry(variants={"day": "missing.png"})])
    assert load_background_manifest(library) == []


@pytest.mark.parametrize("content", ["{broken", "[]", "null", '{}',
    json.dumps({"version": 2, "backgrounds": [entry()]}),
    json.dumps({"version": True, "backgrounds": [entry()]}),
    '{"version": 1, "backgrounds": {}}', '\ud800',
])
def test_broken_manifest_is_empty(library, content):
    assert len(load_background_manifest(library)) == 1
    (library / "manifest.json").write_bytes(content.encode("utf-8", errors="surrogatepass"))
    assert load_background_manifest(library) == []


def test_missing_manifest_is_empty(library):
    assert len(load_background_manifest(library)) == 1
    (library / "manifest.json").unlink()
    assert load_background_manifest(library) == []


@pytest.fixture
async def client(library):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app()), base_url="http://test") as client:
        yield client


AUTH = {"Authorization": "Bearer background-test-token"}


async def test_list_requires_auth_and_returns_only_existing_signed_variants(client, library):
    assert (await client.get("/api/backgrounds")).status_code == 401
    (library / "cafe/evening.webp").unlink()
    response = await client.get("/api/backgrounds", headers=AUTH)
    assert response.status_code == 200
    backgrounds = response.json()["backgrounds"]
    assert len(backgrounds) == 1
    bg = backgrounds[0]
    assert {k: v for k, v in bg.items() if k != "variants"} == {
        "id": "cafe", "name": "咖啡店", "source": "喫茶ステラ",
    }
    assert set(bg["variants"]) == {"day", "night"}
    for variant, url in bg["variants"].items():
        parsed = urlsplit(url)
        params = parse_qs(parsed.query)
        assert parsed.path == f"/api/media/background/cafe/{variant}"
        assert int(params["exp"][0]) == media_urls.compute_bucketed_expiry()
        assert media_urls.verify_media_url_signature(parsed.path, params["exp"][0], params.get("v", [""])[0], params["sig"][0])
        media = await client.get(url)  # Signed images work without a console token.
        assert media.status_code == 200
        assert media.content == f"image-{variant}".encode()
        assert media.headers["content-type"] == "image/webp"
        assert media.headers["cache-control"].startswith("private, max-age=")
    (library / "manifest.json").write_text("broken")
    assert (await client.get("/api/backgrounds", headers=AUTH)).json() == {"backgrounds": []}


@pytest.mark.parametrize("kind", ["missing", "tampered", "unicode", "expiry-tampered", "version-tampered", "expired", "bad-exp", "path-tampered"])
async def test_media_rejects_invalid_signatures(client, kind):
    path = "/api/media/background/cafe/day"
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
        path = "/api/media/background/cafe/night"
    assert (await client.get(f"{path}?{urlencode(params)}")).status_code == 403


@pytest.mark.parametrize("bg_id,variant", [("unknown", "day"), ("cafe", "dawn"), ("cafe", "DAY"), ("cafe", "missing")])
async def test_media_rejects_unknown_id_or_variant_even_when_signed(client, bg_id, variant):
    assert (await client.get(media_urls.sign_media_url("/api/media/background/cafe/day"))).status_code == 200
    response = await client.get(media_urls.sign_media_url(f"/api/media/background/{bg_id}/{variant}"))
    assert response.status_code == 404


@pytest.mark.parametrize("path", [
    "/api/media/background/%2e%2e/day", "/api/media/background/cafe/%2e%2e",
    "/api/media/background/cafe%2f..%2fsecret/day", "/api/media/background/cafe/day%5c..%5csecret",
    "/api/media/background/%252e%252e/day", "/api/media/background/C%3A/day",
])
async def test_media_rejects_traversal_even_when_signed(client, path):
    assert (await client.get(media_urls.sign_media_url("/api/media/background/cafe/day"))).status_code == 200
    assert (await client.get(media_urls.sign_media_url(path))).status_code in {403, 404}


async def test_media_rechecks_manifest_and_file_after_list(client, library):
    url = (await client.get("/api/backgrounds", headers=AUTH)).json()["backgrounds"][0]["variants"]["day"]
    write_manifest(library, [entry(variants={"day": "../outside.webp"})])
    (library.parent / "outside.webp").write_bytes(b"private")
    assert (await client.get(url)).status_code == 404
    write_manifest(library, [entry()])
    (library / "cafe/day.webp").unlink()
    assert (await client.get(url)).status_code == 404
    write_manifest(library, [])
    assert (await client.get(url)).status_code == 404


def test_release_zip_excludes_local_backgrounds(tmp_path, monkeypatch):
    from scripts import package_release

    static = tmp_path / "galgame2voice" / "static"
    static.mkdir(parents=True)
    (static / "index.html").write_text("public app")
    library = tmp_path / "backgrounds"
    library.mkdir()
    (library / "private.avif").write_bytes(b"private image")
    write_manifest(library, [entry()])
    monkeypatch.setattr(package_release, "PROJECT_ROOT", tmp_path)
    assert not package_release.should_include(Path("backgrounds/manifest.json"))
    assert not package_release.should_include(Path("backgrounds/private.avif"))
    archive = package_release.build_release_zip("test-p6")
    with zipfile.ZipFile(archive) as package:
        names = package.namelist()
        assert "galgame2voice-vtest-p6/galgame2voice/static/index.html" in names
        assert not any("backgrounds" in Path(name).parts for name in names)
    root = Path(__file__).resolve().parents[1]
    assert "/backgrounds/" in (root / ".gitignore").read_text().splitlines()
    assert "/backgrounds/" in (root / ".dockerignore").read_text().splitlines()
