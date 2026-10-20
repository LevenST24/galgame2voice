"""
Comprehensive tests for signed media URLs and face-window metadata pass-through (P3 backend).

Covers:
1. HMAC-SHA256 sign and verify round trip
2. Expiry bucketing and freeze/patch time boundary testing (before/at/after exp)
3. Tampering defense (sig, exp, v, character_id, costume, file_name, malformed params)
4. Path traversal defense (.., %2e%2e, %2f, %5c, absolute paths, files outside portrait/)
5. Token-on <img> loading with auth enabled (unauthenticated signed route vs token-protected route)
6. Manifest URL signing and hour-bucket caching
7. Face-window metadata pass-through (faceRect, headAnchor, crop) and dropping invalid values
"""

import hashlib
import json
import sqlite3
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import HTTPException

from galgame2voice.database import crud
from galgame2voice.database.models import VoiceProfileCreate
from galgame2voice.database.session import get_db
from galgame2voice.main import create_app
from galgame2voice.routers import characters as characters_router
from galgame2voice.security.media_urls import (
    compute_bucketed_expiry,
    generate_media_signature,
    get_media_signing_key,
    sign_media_url,
    verify_media_url_signature,
)
from galgame2voice.services.portrait_manifest import _normalize, load_portrait_manifest

TEST_CONSOLE_TOKEN = "test_adv_p3_console_token"

# Valid 100x100 PNG header for sprite bounds tests
TINY_PNG_100x100 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    b"\x00\x00\x00\x64\x00\x00\x00\x64"  # 100 x 100
    b"\x08\x06\x00\x00\x00\x70\xe1\xd5\x12"
    b"\x00\x00\x00\x0bIDATx\x9cc\xf8\x0f\x00\x01\x01\x01\x00\x18\xdd\x8d\xb0"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


# ---------------------------------------------------------------------------
# Test Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def fresh_media_key(monkeypatch):
    """Each test derives its own key instead of reusing the process-wide cache."""
    from galgame2voice.security import media_urls

    monkeypatch.setattr(media_urls, "_cached_key", None)


@pytest.fixture
def auth_enabled(monkeypatch):
    """Enables console token auth for the duration of the test."""
    monkeypatch.setenv("GALGAME2VOICE_AUTH_DISABLED", "0")
    yield


@pytest.fixture
def test_character_pack(tmp_path, monkeypatch):
    """Creates a temporary character package with valid portrait sprites and a secret file."""
    package_dir = tmp_path / "characters" / "TestHeroine"
    portrait_dir = package_dir / "portrait"
    casual_dir = portrait_dir / "casual"
    uniform_dir = portrait_dir / "uniform"
    casual_dir.mkdir(parents=True)
    uniform_dir.mkdir(parents=True)

    (casual_dir / "gentle.png").write_bytes(TINY_PNG_100x100)
    (casual_dir / "happy.webp").write_bytes(TINY_PNG_100x100)
    (uniform_dir / "normal.png").write_bytes(TINY_PNG_100x100)

    # Secret file outside portrait/
    (package_dir / "secret.txt").write_text("SUPER_SECRET_PACKAGE_FILE", encoding="utf-8")

    manifest = {
        "id": "heroine",
        "name": "TestHeroine",
        "description": "Test heroine for signed media tests",
    }
    (package_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    expressions = {
        "version": 2,
        "default_costume": "casual",
        "pose": "pose_a",
        "costumes": {
            "casual": {
                "id": "casual",
                "name": "Casual Outfit",
                "icon": "👗",
                "faceRect": {"x": 10, "y": 10, "width": 40, "height": 40},
                "headAnchor": {"x": 30, "y": 20},
                "crop": {"left": 0, "top": 0, "width": 100, "height": 100},
                "sprites": {
                    "gentle": {"file": "gentle.png", "width": 100, "height": 100},
                    "happy": {"file": "happy.webp", "width": 100, "height": 100},
                },
            },
            "uniform": {
                "id": "uniform",
                "name": "School Uniform",
                "icon": "🎒",
                "sprites": {
                    "normal": {"file": "normal.png", "width": 100, "height": 100},
                },
            },
        },
        "faces": {
            "gentle": {"primary": "gentle", "label": "Gentle", "parts": []},
            "happy": {"primary": "happy", "label": "Happy", "parts": []},
            "normal": {"primary": "normal", "label": "Normal", "parts": []},
        },
        "expression_sets": {
            "gentle": [{"id": "gentle", "strength": 1}],
            "happy": [{"id": "happy", "strength": 1}],
        },
    }
    (portrait_dir / "expressions.json").write_text(json.dumps(expressions), encoding="utf-8")

    pkg_obj = SimpleNamespace(folder=package_dir, manifest=SimpleNamespace(id="heroine", name="TestHeroine"))
    monkeypatch.setattr(
        characters_router,
        "get_character_manager",
        lambda: SimpleNamespace(
            get_character=lambda name: pkg_obj if name in ("TestHeroine", "heroine") else None,
            discover_characters=lambda: [pkg_obj],
            sync_with_db=lambda conn: 1,
        ),
    )
    return SimpleNamespace(package_dir=package_dir, portrait_dir=portrait_dir, pkg_obj=pkg_obj)


@pytest.fixture
async def setup_test_character_db(isolate_test_database, test_character_pack):
    """Inserts a matching voice profile in SQLite."""
    conn = sqlite3.connect(isolate_test_database)
    conn.execute("INSERT OR IGNORE INTO settings (id, console_token) VALUES (1, ?);", (TEST_CONSOLE_TOKEN,))
    conn.commit()
    conn.close()

    async with get_db(isolate_test_database) as db_conn:
        profile = await crud.create_voice_profile(
            db_conn,
            VoiceProfileCreate(
                name="TestHeroine",
                gpt_weights_path="heroine.ckpt",
                sovits_weights_path="heroine.pth",
                ref_audio_path="heroine.wav",
                prompt_text="こんにちは",
                prompt_lang="ja",
                text_lang="ja",
            ),
        )
    return profile


# ---------------------------------------------------------------------------
# 1. Sign then verify round trip
# ---------------------------------------------------------------------------


def test_sign_and_verify_round_trip():
    path = "/api/media/portrait/1/casual/gentle.png"
    v = "18cf78b8a00-12ab"
    t0 = 1700000000.0

    url = sign_media_url(path, v=v, now=t0)
    assert url.startswith(path + "?")
    assert "sig=" in url
    assert "exp=" in url
    assert f"v={v}" in url

    split = urlsplit(url)
    params = parse_qs(split.query)
    exp = int(params["exp"][0])
    sig = params["sig"][0]
    extracted_v = params["v"][0]

    assert extracted_v == v
    assert len(sig) == 64  # HMAC-SHA256 hex length
    assert verify_media_url_signature(path, exp, extracted_v, sig, now=t0) is True


def test_sign_and_verify_without_v():
    path = "/api/media/portrait/1/casual/gentle.png"
    t0 = 1700000000.0

    url = sign_media_url(path, v=None, now=t0)
    assert "sig=" in url
    assert "exp=" in url
    assert "v=" not in url

    split = urlsplit(url)
    params = parse_qs(split.query)
    exp = int(params["exp"][0])
    sig = params["sig"][0]

    assert verify_media_url_signature(path, exp, None, sig, now=t0) is True
    assert verify_media_url_signature(path, exp, "", sig, now=t0) is True


def test_key_derivation_cross_worker_consistency(monkeypatch, tmp_path):
    """Verifies that multiple worker processes / calls derive the exact same key."""
    monkeypatch.setenv("GALGAME2VOICE_MASTER_KEY", "deterministic_test_master_key_12345")
    key1 = get_media_signing_key()
    key2 = get_media_signing_key()
    assert key1 is not None
    assert key1 == key2
    assert len(key1) == 32

    # Derived key must differ from raw master key
    raw_sha = hashlib.sha256(b"deterministic_test_master_key_12345").digest()
    assert key1 != raw_sha


# ---------------------------------------------------------------------------
# 2. Expiry Bucketing & Boundary Testing
# ---------------------------------------------------------------------------


def test_expiry_bucketing():
    """Requests within the same hour bucket must receive the exact same expiry and URL."""
    path = "/api/media/portrait/1/casual/gentle.png"
    v = "test_v"

    # Suppose hour begins at 3600*100 = 360000
    base_hour = 360000.0
    t1 = base_hour + 10.0  # 00:00:10 into the hour
    t2 = base_hour + 1800.0  # 00:30:00 into the hour
    t3 = base_hour + 3599.0  # 00:59:59 into the hour
    next_hour_t = base_hour + 3601.0  # 01:00:01 into next hour

    exp1 = compute_bucketed_expiry(t1)
    exp2 = compute_bucketed_expiry(t2)
    exp3 = compute_bucketed_expiry(t3)
    exp_next = compute_bucketed_expiry(next_hour_t)

    assert exp1 == exp2 == exp3
    assert exp_next > exp1
    assert exp1 - t1 >= 12 * 3600  # at least 12 hours validity

    url1 = sign_media_url(path, v=v, now=t1)
    url2 = sign_media_url(path, v=v, now=t2)
    url_next = sign_media_url(path, v=v, now=next_hour_t)

    assert url1 == url2
    assert url1 != url_next


def test_expiry_boundary_just_before_and_after():
    path = "/api/media/portrait/1/casual/gentle.png"
    v = "cache_v"
    t0 = 1700000000.0
    exp = compute_bucketed_expiry(t0)
    sig = generate_media_signature(path, exp, v=v)
    assert sig is not None

    # Just before expiry (valid)
    assert verify_media_url_signature(path, exp, v, sig, now=exp - 1) is True
    # Exactly at expiry (valid)
    assert verify_media_url_signature(path, exp, v, sig, now=exp) is True
    # Just after expiry (expired)
    assert verify_media_url_signature(path, exp, v, sig, now=exp + 1) is False
    assert verify_media_url_signature(path, exp, v, sig, now=exp + 1000) is False


# ---------------------------------------------------------------------------
# 3. Tampering Defense
# ---------------------------------------------------------------------------


def test_tampering_signature_or_params():
    path = "/api/media/portrait/1/casual/gentle.png"
    v = "version1"
    exp = 1750000000
    sig = generate_media_signature(path, exp, v=v)
    assert sig is not None

    # 1. Tampered signature (one char changed)
    bad_sig = ("0" if sig[0] != "0" else "1") + sig[1:]
    assert verify_media_url_signature(path, exp, v, bad_sig, now=1700000000) is False

    # 2. Tampered expiry
    assert verify_media_url_signature(path, exp + 3600, v, sig, now=1700000000) is False
    assert verify_media_url_signature(path, exp - 3600, v, sig, now=1700000000) is False

    # 3. Tampered cache-buster v
    assert verify_media_url_signature(path, exp, "version2", sig, now=1700000000) is False
    assert verify_media_url_signature(path, exp, "", sig, now=1700000000) is False

    # 4. Tampered character ID in path
    assert verify_media_url_signature("/api/media/portrait/2/casual/gentle.png", exp, v, sig, now=1700000000) is False

    # 5. Tampered costume in path
    assert verify_media_url_signature("/api/media/portrait/1/uniform/gentle.png", exp, v, sig, now=1700000000) is False

    # 6. Tampered file name in path
    assert verify_media_url_signature("/api/media/portrait/1/casual/happy.webp", exp, v, sig, now=1700000000) is False


# ---------------------------------------------------------------------------
# 4. HTTP Endpoint: Expiry, Tampering, Cache-Control
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_endpoint_expiry_and_cache_control(setup_test_character_db, monkeypatch):
    profile = setup_test_character_db
    app = create_app()
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        t0 = 1700000000.0
        monkeypatch.setattr(time, "time", lambda: t0)

        path = f"/api/media/portrait/{profile.id}/casual/gentle.png"
        signed_url = sign_media_url(path, v="testv", now=t0)

        # Before expiry -> 200 OK
        resp = await client.get(signed_url)
        assert resp.status_code == 200
        assert resp.content == TINY_PNG_100x100
        # Check Cache-Control private and max-age
        cc = resp.headers.get("Cache-Control", "")
        assert "private" in cc
        assert "max-age=" in cc

        # Simulate time advancing to just after expiry
        params = parse_qs(urlsplit(signed_url).query)
        exp = int(params["exp"][0])
        monkeypatch.setattr(time, "time", lambda: exp + 1.0)

        expired_resp = await client.get(signed_url)
        assert expired_resp.status_code == 403
        assert expired_resp.json()["detail"] == "Forbidden"


@pytest.mark.asyncio
async def test_endpoint_tampering_rejected_with_403(setup_test_character_db):
    profile = setup_test_character_db
    app = create_app()
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        path = f"/api/media/portrait/{profile.id}/casual/gentle.png"
        signed_url = sign_media_url(path, v="v1", now=1700000000.0)

        # 1. Changed sig
        bad_sig_url = signed_url[:-4] + "ffff"
        res = await client.get(bad_sig_url)
        assert res.status_code == 403
        assert res.json()["detail"] == "Forbidden"

        # 2. Changed exp
        split = urlsplit(signed_url)
        params = parse_qs(split.query)
        tampered_exp = int(params["exp"][0]) + 100
        url_tampered_exp = f"{path}?v=v1&exp={tampered_exp}&sig={params['sig'][0]}"
        res = await client.get(url_tampered_exp)
        assert res.status_code == 403

        # 3. Changed v
        url_tampered_v = f"{path}?v=v2&exp={params['exp'][0]}&sig={params['sig'][0]}"
        res = await client.get(url_tampered_v)
        assert res.status_code == 403

        # 4. Changed costume in path
        tampered_path_url = f"/api/media/portrait/{profile.id}/uniform/gentle.png?{split.query}"
        res = await client.get(tampered_path_url)
        assert res.status_code == 403

        # 5. Missing sig
        res = await client.get(f"{path}?v=v1&exp={params['exp'][0]}")
        assert res.status_code == 403

        # 6. Missing exp
        res = await client.get(f"{path}?v=v1&sig={params['sig'][0]}")
        assert res.status_code == 403


# ---------------------------------------------------------------------------
# 5. Path Traversal & Security Boundary Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_endpoint_path_traversal_rejection(setup_test_character_db):
    profile = setup_test_character_db
    app = create_app()
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        base = f"/api/media/portrait/{profile.id}"
        exp = compute_bucketed_expiry()

        traversal_attempts = [
            f"{base}/casual/../secret.txt",
            f"{base}/casual/..%2Fsecret.txt",
            f"{base}/casual/..%5Csecret.txt",
            f"{base}/casual/%2e%2e/secret.txt",
            f"{base}/..%2Fcasual/gentle.png",
            f"{base}/casual%2fother/gentle.png",
            f"{base}/casual\\other/gentle.png",
            f"{base}/casual/secret.txt",  # non-whitelisted extension (.txt)
            f"{base}/casual/C:/secret.txt",
        ]

        for target in traversal_attempts:
            # Sign the malicious path to see if handler itself catches traversal even with a signature
            sig = generate_media_signature(target, exp, v="")
            url = f"{target}?exp={exp}&sig={sig}"
            resp = await client.get(url)
            assert resp.status_code in (400, 404), f"Traversal target {target} should be rejected, got {resp.status_code}"
            assert b"SUPER_SECRET" not in resp.content

            # Also verify without signature: traversal attacks must never succeed
            unsigned_resp = await client.get(target)
            assert unsigned_resp.status_code in (400, 403, 404)
            assert b"SUPER_SECRET" not in unsigned_resp.content

    # Direct handler call check
    for costume, file_name in (
        ("..", "secret.txt"),
        ("casual", ".."),
        ("casual", "../../secret.txt"),
        ("casual", r"..\..\secret.txt"),
        ("casual", "secret.txt"),  # invalid extension
    ):
        mock_req = SimpleNamespace(scope={"raw_path": b"/api/media/portrait/1/"})
        with pytest.raises(HTTPException) as exc_info:
            await characters_router.get_media_portrait(
                request=mock_req,
                character_id=profile.id,
                costume=costume,
                file_name=file_name,
                sig="0" * 64,
                exp=str(exp),
            )
        assert exc_info.value.status_code in (400, 403, 404)


# ---------------------------------------------------------------------------
# 6. Token-on <img> Loading (Authentication Enforcement)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_token_on_img_loading_under_auth(auth_enabled, setup_test_character_db):
    """
    With console token auth enabled:
    - Signed media URL succeeds without any token header (<img> tag loading).
    - Signed media URL without signature returns 403.
    - Old /api/characters/.../portrait/file route still demands the token (returns 401 without it).
    """
    profile = setup_test_character_db
    app = create_app()
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        media_path = f"/api/media/portrait/{profile.id}/casual/gentle.png"
        signed_media_url = sign_media_url(media_path, v="stat_hash")

        old_file_path = f"/api/characters/{profile.id}/portrait/file/casual/gentle.png"

        # 1. Signed media route WITHOUT token header -> succeeds (200 OK)
        img_resp = await client.get(signed_media_url)
        assert img_resp.status_code == 200
        assert img_resp.content == TINY_PNG_100x100

        # 2. Signed media route WITHOUT signature or token -> 403 Forbidden
        no_sig_resp = await client.get(media_path)
        assert no_sig_resp.status_code == 403

        # 3. Old file route WITHOUT token header -> 401 Unauthorized
        old_unauthed_resp = await client.get(old_file_path)
        assert old_unauthed_resp.status_code == 401

        # 4. Old file route WITH token header -> 200 OK
        old_authed_resp = await client.get(
            old_file_path,
            headers={"Authorization": f"Bearer {TEST_CONSOLE_TOKEN}"},
        )
        assert old_authed_resp.status_code == 200
        assert old_authed_resp.content == TINY_PNG_100x100


# ---------------------------------------------------------------------------
# 7. Manifest Signed URLs & Bucketing
# ---------------------------------------------------------------------------


def test_manifest_returns_signed_urls_and_buckets(test_character_pack, monkeypatch):
    portrait_dir = test_character_pack.portrait_dir
    t0 = 1700000000.0
    monkeypatch.setattr(time, "time", lambda: t0)

    data1 = characters_router._resolve_character_portrait("TestHeroine", 1)
    assert data1 is not None
    sprite_file1 = data1["sprites"]["casual"]["gentle"]["file"]

    assert sprite_file1.startswith("/api/media/portrait/1/casual/gentle.png?")
    assert "sig=" in sprite_file1
    assert "exp=" in sprite_file1
    assert "v=" in sprite_file1

    # Same file in the same hour bucket gets the EXACT same signed URL
    data2 = characters_router._resolve_character_portrait("TestHeroine", 1)
    sprite_file2 = data2["sprites"]["casual"]["gentle"]["file"]
    assert sprite_file1 == sprite_file2

    # Advance time to next hour bucket -> new signed URL
    monkeypatch.setattr(time, "time", lambda: t0 + 3600.0)
    data3 = characters_router._resolve_character_portrait("TestHeroine", 1)
    sprite_file3 = data3["sprites"]["casual"]["gentle"]["file"]
    assert sprite_file1 != sprite_file3

    # File modification (re-export) changes v and URL
    time.sleep(0.01)
    (portrait_dir / "casual" / "gentle.png").write_bytes(TINY_PNG_100x100 + b"extra")
    data4 = characters_router._resolve_character_portrait("TestHeroine", 1)
    sprite_file4 = data4["sprites"]["casual"]["gentle"]["file"]
    assert sprite_file3 != sprite_file4


def test_manifest_fallback_when_key_unavailable(test_character_pack, monkeypatch):
    """When signing key is unavailable, falls back to unsigned URL without crashing."""
    monkeypatch.setattr(characters_router, "get_media_signing_key", lambda: None)

    data = characters_router._resolve_character_portrait("TestHeroine", 1)
    assert data is not None
    sprite_file = data["sprites"]["casual"]["gentle"]["file"]

    assert sprite_file.startswith("/api/characters/1/portrait/file/casual/gentle.png")
    assert "sig=" not in sprite_file


# ---------------------------------------------------------------------------
# 8. Face-Window Metadata Pass-Through (faceRect, headAnchor, crop)
# ---------------------------------------------------------------------------


def test_face_window_metadata_pass_through_valid(tmp_path):
    root = tmp_path / "portrait"
    casual = root / "casual"
    casual.mkdir(parents=True)
    (casual / "01.png").write_bytes(TINY_PNG_100x100)

    data = {
        "version": 2,
        "default_costume": "casual",
        "pose": "pose_a",
        "costumes": {
            "casual": {
                "name": "Casual",
                "sprites": {"01": {"file": "01.png", "width": 100, "height": 100}},
                "faceRect": {"x": 10, "y": 15, "width": 50, "height": 60},
                "headAnchor": {"x": 35, "y": 25},
                "crop": {"left": 0, "top": 0, "width": 100, "height": 100},
            }
        },
        "faces": {"01": {"primary": "01", "label": "Normal", "parts": []}},
        "expression_sets": {"gentle": [{"id": "01", "strength": 1}]},
    }

    normalized = _normalize(root, data, legacy=False)
    assert normalized is not None
    costume_entry = normalized["costumes"][0]

    assert costume_entry["faceRect"] == {"x": 10, "y": 15, "width": 50, "height": 60}
    assert costume_entry["headAnchor"] == {"x": 35, "y": 25}
    assert costume_entry["crop"] == {"left": 0, "top": 0, "width": 100, "height": 100}


def test_face_window_metadata_drops_invalid_values(tmp_path):
    root = tmp_path / "portrait"
    casual = root / "casual"
    casual.mkdir(parents=True)
    (casual / "01.png").write_bytes(TINY_PNG_100x100)

    invalid_cases = [
        # Outside sprite bounds (100x100)
        {"faceRect": {"x": 80, "y": 80, "width": 50, "height": 50}},
        # Negative coords
        {"faceRect": {"x": -5, "y": 10, "width": 30, "height": 30}},
        # Width/height <= 0
        {"faceRect": {"x": 10, "y": 10, "width": 0, "height": 30}},
        {"faceRect": {"x": 10, "y": 10, "width": 30, "height": -10}},
        # Non-numeric / boolean / NaN / Inf
        {"faceRect": {"x": "10", "y": 10, "width": 30, "height": 30}},
        {"faceRect": {"x": True, "y": 10, "width": 30, "height": 30}},
        {"faceRect": {"x": float("nan"), "y": 10, "width": 30, "height": 30}},
        {"faceRect": {"x": float("inf"), "y": 10, "width": 30, "height": 30}},
        # Invalid headAnchor
        {"headAnchor": {"x": "20", "y": 10}},
        {"headAnchor": {"x": 20}},
        {"headAnchor": "not_a_dict"},
        # Invalid crop
        {"crop": {"left": 0, "top": 0, "width": 100}},  # missing height
        {"crop": {"left": 0, "top": "bad", "width": 100, "height": 100}},
    ]

    for bad_fields in invalid_cases:
        data = {
            "version": 2,
            "default_costume": "casual",
            "pose": "pose_a",
            "costumes": {
                "casual": {
                    "name": "Casual",
                    "sprites": {"01": {"file": "01.png", "width": 100, "height": 100}},
                    **bad_fields,
                }
            },
            "faces": {"01": {"primary": "01", "label": "Normal", "parts": []}},
            "expression_sets": {"gentle": [{"id": "01", "strength": 1}]},
        }
        normalized = _normalize(root, data, legacy=False)
        assert normalized is not None
        costume_entry = normalized["costumes"][0]
        for key in bad_fields:
            assert key not in costume_entry, f"Field {key} with value {bad_fields[key]} was not dropped"


def test_legacy_manifest_json_unchanged_by_pass_through(tmp_path):
    """Legacy manifest.json packs never get faceRect/headAnchor/crop."""
    root = tmp_path / "portrait"
    casual = root / "casual"
    casual.mkdir(parents=True)
    (casual / "gentle.png").write_bytes(TINY_PNG_100x100)

    legacy_manifest = {
        "default_costume": "casual",
        "costumes": [{
            "id": "casual",
            "name": "Casual",
            "faceRect": {"x": 10, "y": 10, "width": 40, "height": 40},
            "headAnchor": {"x": 30, "y": 20},
            "crop": {"left": 0, "top": 0, "width": 100, "height": 100},
        }],
        "sprites": {"casual": {"gentle": {"file": "gentle.png"}}},
    }
    (root / "manifest.json").write_text(json.dumps(legacy_manifest), encoding="utf-8")

    loaded = load_portrait_manifest(root)
    assert loaded is not None
    costume_entry = loaded["costumes"][0]
    assert "faceRect" not in costume_entry
    assert "headAnchor" not in costume_entry
    assert "crop" not in costume_entry
