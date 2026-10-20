"""Tests for P5 layered portraits (v3), validation, loader precedence, signed media, and acting menu."""

import json
import logging
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from galgame2voice.database import crud
from galgame2voice.database.models import VoiceProfileCreate
from galgame2voice.database.session import get_db
from galgame2voice.main import create_app
from galgame2voice.routers import characters as characters_router
from galgame2voice.security.media_urls import (
    compute_bucketed_expiry,
    generate_media_signature,
    sign_media_url,
)
from galgame2voice.services.chat_pipelines.acting_menu import build_acting_menu, sanitize_act
from galgame2voice.services.portrait_manifest import (
    _normalize_v3,
    _validate_v3,
    load_portrait_manifest,
)

TINY_WEBP = b"RIFF\x18\x00\x00\x00WEBPVP8L\x0c\x00\x00\x00/\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15c4"
    b"\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


def write_legacy_pack(root, *, version=2):
    (root / "uniform").mkdir(exist_ok=True)
    (root / "uniform" / "01.png").write_bytes(TINY_PNG)
    data = {
        "version": version,
        "default_costume": "uniform",
        "costumes": {"uniform": {
            "name": "旧校服",
            "crop": {"left": 0, "top": 0, "width": 1, "height": 1},
            "faceRect": {"x": 0, "y": 0, "width": 1, "height": 1},
            "headAnchor": {"x": 0, "y": 0},
            "sprites": {"01": {"file": "01.png", "width": 1, "height": 1}},
        }},
        "faces": {"01": {"label": "平常", "primary": "gentle"}},
        "expression_sets": {"gentle": [{"id": "01", "strength": 3}]},
    }
    if version == 1:
        data["sprites"] = {"uniform": {"gentle": {"file": "01.png"}}}
        data["costumes"] = [{"id": "uniform", "name": "旧校服"}]
    filename = "expressions.json" if version == 2 else "manifest.json"
    (root / filename).write_text(json.dumps(data), encoding="utf-8")


@pytest.mark.parametrize("file", ["layers/B/1278.webp", "1278.webp", "layers/A/sub/1278.png", "layers/A/bad name.png", "layers/A/1278.PNG"])
def test_v3_layer_file_must_match_canvas_and_filename(tmp_path, file):
    root, data = make_valid_v3(tmp_path)
    data["layers"]["A/1278"]["file"] = file
    assert _validate_v3(root, data)[0] is False


@pytest.mark.parametrize("canvas", ["", "A/B", "A\\B", "画布", "A" * 33, "A.B"])
def test_v3_canvas_id_must_be_route_segment(tmp_path, canvas):
    root, data = make_valid_v3(tmp_path)
    data["canvases"][canvas] = {"width": 1, "height": 1}
    assert _validate_v3(root, data)[0] is False


@pytest.mark.parametrize("version", [1, 2])
def test_v3_attaches_normalized_legacy_with_own_geometry(tmp_path, version):
    root, data = make_valid_v3(tmp_path)
    (root / "layers.json").write_text(json.dumps(data), encoding="utf-8")
    write_legacy_pack(root, version=version)
    if version == 1:
        (root / "expressions.json").write_text("{}", encoding="utf-8")
    loaded = load_portrait_manifest(root)
    assert loaded["version"] == 3
    assert loaded["sprites"] == {}
    assert loaded["layers"]
    assert loaded["legacy"]["sprites"]["uniform"]
    if version == 2:
        assert loaded["legacy"]["costumes"][0]["crop"]["width"] == 1


def test_v3_only_has_no_legacy(tmp_path):
    root, data = make_valid_v3(tmp_path)
    (root / "layers.json").write_text(json.dumps(data), encoding="utf-8")
    loaded = load_portrait_manifest(root)
    assert loaded["sprites"] == {}
    assert "legacy" not in loaded


def test_v3_menu_intersects_labels_and_preserves_per_look_choices(tmp_path):
    root, data = make_valid_v3(tmp_path)
    data["looks"]["b/uniform"]["faces"]["01"] = {"layers": ["A/1511"], "eyesClosed": False}
    data["looks"]["b/uniform"]["faces"]["14"] = {"layers": ["A/1531"], "eyesClosed": True}
    menu = build_acting_menu(_normalize_v3(root, data), "uniform")
    assert menu["faces"] == ["平常", "闭眼"]
    assert menu["pose_names"] == {"a": "站姿1", "b": "站姿2"}
    assert menu["look_faces"]["a"] == ["平常", "微笑", "闭眼", "半闭"]
    assert menu["look_faces"]["b"] == ["惊讶", "平常", "闭眼"]
    assert menu["face_ids"]["微笑"] == ["02"]
    assert sanitize_act({"face": "微笑"}, menu, line_index=0, character="测试", current_pose="a") == {"face": "微笑"}


@pytest.mark.asyncio
@pytest.mark.parametrize("signed", [True, False])
async def test_layer_route_strict_paths_and_auth(setup_v3_character_db, v3_character_pack, monkeypatch, signed):
    profile = setup_v3_character_db
    monkeypatch.setenv("GALGAME2VOICE_AUTH_DISABLED", "0")
    token = "synthetic_p5_console_token"
    async with get_db() as conn:
        await conn.execute(
            "INSERT INTO settings (id, console_token) VALUES (1, ?) "
            "ON CONFLICT(id) DO UPDATE SET console_token = excluded.console_token", (token,),
        )
        await conn.commit()
    headers = {} if signed else {"Authorization": f"Bearer {token}"}
    base = (f"/api/media/portrait/{profile.id}/layers" if signed else
            f"/api/characters/{profile.id}/portrait/layers")
    path = f"{base}/A/1278.webp"
    url = sign_media_url(path) if signed else path
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app()), base_url="http://test") as client:
        if not signed:
            assert (await client.get(path)).status_code == 401
        good = await client.get(url, headers=headers)
        assert good.status_code == 200
        assert good.content == TINY_WEBP
        if signed:
            assert (await client.get(url.replace("/A/", "/B/"))).status_code == 403
            assert (await client.get(url.replace("1278.webp", "1301.webp"))).status_code == 403
            assert (await client.get(url + "&v=tampered")).status_code == 403
            expired = int(time.time()) - 1
            sig = generate_media_signature(path, expired, "")
            assert (await client.get(f"{path}?exp={expired}&sig={sig}")).status_code == 403
            assert (await client.get(path)).status_code == 403
            for exp in ("invalid", ""):
                assert (await client.get(f"{path}?exp={exp}&sig={'0' * 64}")).status_code == 403
        # Existing outside files must still be inaccessible under a layer URL.
        (v3_character_pack.portrait_dir / "outside.webp").write_bytes(b"PRIVATE")
        for tail in ("B/1278.webp", "A/outside.webp", "A/1278.jpg", "A/bad name.png", "A/1278.PNG",
                     "A%2fB/1278.webp", "A/..%2foutside.webp", "A/%2e%2e%5coutside.webp",
                     "A/%252e%252e%252foutside.webp", "A%252fB/1278.webp", "A.B/1278.webp", "A/one..png"):
            target = f"{base}/{tail}"
            bad = await client.get(sign_media_url(target) if signed else target, headers=headers)
            assert bad.status_code in (400, 404), (target, bad.status_code)
            assert b"PRIVATE" not in bad.content


@pytest.mark.asyncio
@pytest.mark.parametrize("signed", [True, False])
async def test_layer_route_rejects_resolved_file_outside_canvas(setup_v3_character_db, v3_character_pack, monkeypatch, signed):
    target = v3_character_pack.portrait_dir / "layers" / "A" / "1278.webp"
    outside = v3_character_pack.portrait_dir / "layers" / "B" / "1278.webp"
    outside.parent.mkdir()
    outside.write_bytes(b"PRIVATE")
    original_resolve = Path.resolve

    def resolve(path, *args, **kwargs):
        return outside if path == target else original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    profile = setup_v3_character_db
    path = (f"/api/media/portrait/{profile.id}/layers/A/1278.webp" if signed else
            f"/api/characters/{profile.id}/portrait/layers/A/1278.webp")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app()), base_url="http://test") as client:
        result = await client.get(sign_media_url(path) if signed else path)
    assert result.status_code == 404
    assert b"PRIVATE" not in result.content


@pytest.mark.asyncio
@pytest.mark.parametrize("signed", [True, False])
async def test_manifest_layer_and_legacy_urls_load(setup_v3_character_db, v3_character_pack, monkeypatch, signed):
    write_legacy_pack(v3_character_pack.portrait_dir)
    if not signed:
        monkeypatch.setattr(characters_router, "get_media_signing_key", lambda: None)
    profile = setup_v3_character_db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app()), base_url="http://test") as client:
        payload = (await client.get(f"/api/characters/{profile.id}/portrait")).json()
        assert payload["version"] == 3
        assert payload["sprites"] == {}
        layer_url = payload["layers"]["A/1278"]["file"]
        legacy_url = payload["legacy"]["sprites"]["uniform"]["01"]["file"]
        prefix = f"/api/media/portrait/{profile.id}/" if signed else f"/api/characters/{profile.id}/portrait/"
        assert layer_url.startswith(prefix + "layers/A/1278.webp?")
        assert "v=" in layer_url and "v=" in legacy_url
        info = (v3_character_pack.portrait_dir / "layers" / "A" / "1278.webp").stat()
        assert f"v={info.st_mtime_ns:x}-{info.st_size:x}" in layer_url
        assert ("sig=" in layer_url) is signed
        assert ("sig=" in legacy_url) is signed
        assert (await client.get(layer_url)).content == TINY_WEBP
        assert (await client.get(legacy_url)).content == TINY_PNG


def make_valid_v3(tmp_path: Path) -> tuple[Path, dict]:
    portrait_dir = tmp_path / "portrait"
    portrait_dir.mkdir(parents=True, exist_ok=True)
    layers_dir = portrait_dir / "layers" / "A"
    layers_dir.mkdir(parents=True, exist_ok=True)

    for fname in (
        "1278.webp", "1301.webp", "1490.webp", "1511.webp", "1402.webp",
        "1512.webp", "1531.webp", "1533.webp", "1600.webp",
    ):
        (layers_dir / fname).write_bytes(TINY_WEBP)

    data = {
        "version": 3,
        "defaultOutfit": "uniform",
        "defaultPose": "a",
        "canvases": {"A": {"width": 1875, "height": 2656}},
        "outfits": {
            "uniform": {"name": "校服", "icon": "🎒"},
            "casual": {"name": "便服", "icon": "👗"},
        },
        "poses": {
            "a": {"name": "站姿1", "canvas": "A"},
            "b": {"name": "站姿2", "canvas": "A"},
        },
        "faces": {
            "01": {"label": "平常", "jp": "1通常１", "emotion": "gentle", "weights": {"gentle": 3}},
            "02": {"label": "微笑", "jp": "2笑顔", "emotion": "happy", "weights": {"happy": 3}},
            "14": {"label": "闭眼", "jp": "14目閉じ", "emotion": "gentle", "weights": {"gentle": 3}},
            "16": {"label": "半闭", "jp": "16半目", "emotion": "gentle", "weights": {"gentle": 3}},
            "20": {"label": "惊讶", "jp": "20驚き", "emotion": "surprised", "weights": {"surprised": 3}},
        },
        "looks": {
            "a/uniform": {
                "pose": "a",
                "outfit": "uniform",
                "crop": {"left": 540, "top": 865, "width": 792, "height": 1623},
                "faceRect": {"x": 175, "y": 0, "width": 362, "height": 325},
                "headAnchor": {"x": 356, "y": 2},
                "body": ["A/1278", "A/1301"],
                "blush": ["A/1490"],
                "fx": {"sweat": ["A/1600"]},
                "defaultFace": "01",
                "faces": {
                    "01": {
                        "layers": ["A/1511", "A/1402"],
                        "eyesClosed": False,
                        "blink": {"half": "16", "closed": "14"},
                        "mouth": {"closed": "01", "open": "02"},
                        "combinedFrames": {"half:open": "16", "closed:open": "14"},
                    },
                    "02": {"layers": ["A/1512", "A/1402"], "eyesClosed": False},
                    "14": {"layers": ["A/1531", "A/1402"], "eyesClosed": True},
                    "16": {"layers": ["A/1533", "A/1402"], "eyesClosed": False},
                },
            },
            "b/uniform": {
                "pose": "b",
                "outfit": "uniform",
                "crop": {"left": 540, "top": 865, "width": 792, "height": 1623},
                "faceRect": {"x": 175, "y": 0, "width": 362, "height": 325},
                "headAnchor": {"x": 356, "y": 2},
                "body": ["A/1278"],
                "defaultFace": "20",
                "faces": {
                    "20": {"layers": ["A/1511"], "eyesClosed": False},
                },
            },
            "a/casual": {
                "pose": "a",
                "outfit": "casual",
                "crop": {"left": 540, "top": 865, "width": 792, "height": 1623},
                "faceRect": {"x": 175, "y": 0, "width": 362, "height": 325},
                "headAnchor": {"x": 356, "y": 2},
                "body": ["A/1278"],
                "defaultFace": "01",
                "faces": {
                    "01": {"layers": ["A/1511"], "eyesClosed": False},
                },
            },
        },
        "layers": {
            "A/1278": {"canvas": "A", "file": "layers/A/1278.webp", "rect": {"x": 600, "y": 900, "width": 700, "height": 1500}, "z": 0, "opacity": 1},
            "A/1301": {"canvas": "A", "file": "layers/A/1301.webp", "rect": {"x": 600, "y": 900, "width": 700, "height": 1500}, "z": 1, "opacity": 1},
            "A/1490": {"canvas": "A", "file": "layers/A/1490.webp", "rect": {"x": 650, "y": 950, "width": 200, "height": 100}, "z": 20, "opacity": 0.8},
            "A/1511": {"canvas": "A", "file": "layers/A/1511.webp", "rect": {"x": 650, "y": 950, "width": 300, "height": 200}, "z": 10, "opacity": 1},
            "A/1402": {"canvas": "A", "file": "layers/A/1402.webp", "rect": {"x": 650, "y": 900, "width": 320, "height": 220}, "z": 15, "opacity": 1},
            "A/1512": {"canvas": "A", "file": "layers/A/1512.webp", "rect": {"x": 650, "y": 950, "width": 300, "height": 200}, "z": 10, "opacity": 1},
            "A/1531": {"canvas": "A", "file": "layers/A/1531.webp", "rect": {"x": 650, "y": 950, "width": 300, "height": 200}, "z": 10, "opacity": 1},
            "A/1533": {"canvas": "A", "file": "layers/A/1533.webp", "rect": {"x": 650, "y": 950, "width": 300, "height": 200}, "z": 10, "opacity": 1},
            "A/1600": {"canvas": "A", "file": "layers/A/1600.webp", "rect": {"x": 650, "y": 920, "width": 80, "height": 80}, "z": 25, "opacity": 1},
        },
    }
    return portrait_dir, data


# ===========================================================================
# 1. Validation Rules
# ===========================================================================


class TestV3Validation:
    def test_valid_manifest_passes(self, tmp_path):
        root, data = make_valid_v3(tmp_path)
        valid, reason = _validate_v3(root, data)
        assert valid is True
        assert reason == ""

    def test_version_must_be_3(self, tmp_path):
        root, data = make_valid_v3(tmp_path)
        data["version"] = 2
        valid, reason = _validate_v3(root, data)
        assert valid is False
        assert "version must be 3" in reason

    def test_canvas_dimensions_must_be_positive_ints(self, tmp_path):
        root, data = make_valid_v3(tmp_path)
        data["canvases"]["A"]["width"] = -100
        valid, reason = _validate_v3(root, data)
        assert valid is False
        assert "width and height must be positive integers" in reason

    def test_pose_references_unknown_canvas(self, tmp_path):
        root, data = make_valid_v3(tmp_path)
        data["poses"]["a"]["canvas"] = "UNKNOWN"
        valid, reason = _validate_v3(root, data)
        assert valid is False
        assert "unknown canvas" in reason

    def test_layer_rect_must_lie_inside_canvas(self, tmp_path):
        root, data = make_valid_v3(tmp_path)
        data["layers"]["A/1278"]["rect"]["x"] = 2000  # canvas width is 1875
        valid, reason = _validate_v3(root, data)
        assert valid is False
        assert "lies outside canvas" in reason

    def test_layer_file_must_end_in_webp_or_png(self, tmp_path):
        root, data = make_valid_v3(tmp_path)
        data["layers"]["A/1278"]["file"] = "layers/A/1278.jpg"
        valid, reason = _validate_v3(root, data)
        assert valid is False
        assert "must end in .webp or .png" in reason

    def test_layer_file_rejects_path_traversal(self, tmp_path):
        root, data = make_valid_v3(tmp_path)
        data["layers"]["A/1278"]["file"] = "../secret.webp"
        valid, reason = _validate_v3(root, data)
        assert valid is False
        assert "is not a safe relative path" in reason

    def test_layer_file_rejects_backslash(self, tmp_path):
        root, data = make_valid_v3(tmp_path)
        data["layers"]["A/1278"]["file"] = r"layers\A\1278.webp"
        valid, reason = _validate_v3(root, data)
        assert valid is False
        assert "is not a safe relative path" in reason

    def test_crop_must_lie_inside_canvas(self, tmp_path):
        root, data = make_valid_v3(tmp_path)
        data["looks"]["a/uniform"]["crop"]["width"] = 2500  # exceeds canvas 1875
        valid, reason = _validate_v3(root, data)
        assert valid is False
        assert "crop lies outside canvas" in reason

    def test_face_rect_must_lie_inside_crop(self, tmp_path):
        root, data = make_valid_v3(tmp_path)
        data["looks"]["a/uniform"]["faceRect"]["width"] = 1000  # crop width is 792
        valid, reason = _validate_v3(root, data)
        assert valid is False
        assert "faceRect lies outside crop" in reason

    def test_head_anchor_must_lie_inside_crop(self, tmp_path):
        root, data = make_valid_v3(tmp_path)
        data["looks"]["a/uniform"]["headAnchor"]["x"] = 900  # crop width is 792
        valid, reason = _validate_v3(root, data)
        assert valid is False
        assert "headAnchor lies outside crop" in reason

    def test_look_key_must_match_pose_and_outfit(self, tmp_path):
        root, data = make_valid_v3(tmp_path)
        data["looks"]["mismatched/uniform"] = data["looks"].pop("a/uniform")
        valid, reason = _validate_v3(root, data)
        assert valid is False
        assert "does not match pose/outfit" in reason

    def test_look_layer_must_match_pose_canvas(self, tmp_path):
        root, data = make_valid_v3(tmp_path)
        data["canvases"]["B"] = {"width": 1000, "height": 1000}
        data["layers"]["B/1"] = {"canvas": "B", "file": "layers/B/1.webp", "rect": {"x": 0, "y": 0, "width": 100, "height": 100}, "z": 0, "opacity": 1}
        data["looks"]["a/uniform"]["body"].append("B/1")
        valid, reason = _validate_v3(root, data)
        assert valid is False
        assert "does not match pose canvas" in reason

    def test_default_look_must_exist(self, tmp_path):
        root, data = make_valid_v3(tmp_path)
        data["defaultPose"] = "nonexistent"
        valid, reason = _validate_v3(root, data)
        assert valid is False
        assert "not found in poses" in reason

    def test_look_default_face_must_be_in_faces(self, tmp_path):
        root, data = make_valid_v3(tmp_path)
        data["looks"]["a/uniform"]["defaultFace"] = "99"
        valid, reason = _validate_v3(root, data)
        assert valid is False
        assert "defaultFace '99' not in look faces" in reason

    def test_every_outfit_must_be_used(self, tmp_path):
        root, data = make_valid_v3(tmp_path)
        data["outfits"]["unused"] = {"name": "未用"}
        valid, reason = _validate_v3(root, data)
        assert valid is False
        assert "outfit 'unused' is not used by any look" in reason

    def test_every_pose_must_be_used(self, tmp_path):
        root, data = make_valid_v3(tmp_path)
        data["poses"]["unused"] = {"name": "未用", "canvas": "A"}
        valid, reason = _validate_v3(root, data)
        assert valid is False
        assert "pose 'unused' is not used by any look" in reason

    def test_blink_requires_eyes_closed_false(self, tmp_path):
        root, data = make_valid_v3(tmp_path)
        # Face 14 has eyesClosed: True; give it blink
        data["looks"]["a/uniform"]["faces"]["14"]["blink"] = {"closed": "14"}
        valid, reason = _validate_v3(root, data)
        assert valid is False
        assert "has blink but eyesClosed is true" in reason

    def test_blink_closed_partner_must_have_eyes_closed_true(self, tmp_path):
        root, data = make_valid_v3(tmp_path)
        # Point blink closed to face 02 which has eyesClosed: False
        data["looks"]["a/uniform"]["faces"]["01"]["blink"]["closed"] = "02"
        valid, reason = _validate_v3(root, data)
        assert valid is False
        assert "closed partner '02' must have eyesClosed: true" in reason

    def test_blink_partner_must_be_in_look_faces(self, tmp_path):
        root, data = make_valid_v3(tmp_path)
        data["looks"]["a/uniform"]["faces"]["01"]["blink"]["half"] = "99"
        valid, reason = _validate_v3(root, data)
        assert valid is False
        assert "partner '99' not in look faces" in reason

    def test_mouth_partner_must_be_in_look_faces(self, tmp_path):
        root, data = make_valid_v3(tmp_path)
        data["looks"]["a/uniform"]["faces"]["01"]["mouth"]["open"] = "99"
        valid, reason = _validate_v3(root, data)
        assert valid is False
        assert "mouth open partner '99' not in look faces" in reason

    def test_combined_frames_key_must_be_valid(self, tmp_path):
        root, data = make_valid_v3(tmp_path)
        data["looks"]["a/uniform"]["faces"]["01"]["combinedFrames"] = {"bad_key": "16"}
        valid, reason = _validate_v3(root, data)
        assert valid is False
        assert "combinedFrames key 'bad_key' malformed" in reason

    def test_combined_frames_target_must_be_in_look_faces(self, tmp_path):
        root, data = make_valid_v3(tmp_path)
        data["looks"]["a/uniform"]["faces"]["01"]["combinedFrames"] = {"half:open": "99"}
        valid, reason = _validate_v3(root, data)
        assert valid is False
        assert "target '99' not in look faces" in reason


# ===========================================================================
# 2. Precedence and Fallback
# ===========================================================================


class TestPrecedenceAndFallback:
    def test_valid_v3_layers_json_wins_over_v2_and_v1(self, tmp_path):
        root, v3_data = make_valid_v3(tmp_path)
        (root / "layers.json").write_text(json.dumps(v3_data), encoding="utf-8")

        v2_data = {"version": 2, "costumes": {"uniform": {"id": "uniform"}}}
        (root / "expressions.json").write_text(json.dumps(v2_data), encoding="utf-8")

        v1_data = {"costumes": {"uniform": {}}}
        (root / "manifest.json").write_text(json.dumps(v1_data), encoding="utf-8")

        loaded = load_portrait_manifest(root)
        assert loaded is not None
        assert loaded.get("version") == 3
        assert "looks" in loaded

    def test_invalid_v3_falls_back_to_v2(self, tmp_path, caplog):
        root, v3_data = make_valid_v3(tmp_path)
        v3_data["version"] = 999  # Invalid!
        (root / "layers.json").write_text(json.dumps(v3_data), encoding="utf-8")

        # Write valid v2
        (root / "uniform").mkdir(parents=True, exist_ok=True)
        (root / "uniform" / "01.png").write_bytes(TINY_PNG)
        v2_data = {
            "version": 2,
            "default_costume": "uniform",
            "costumes": {
                "uniform": {
                    "id": "uniform",
                    "name": "校服",
                    "sprites": {"01": {"file": "01.png", "width": 1, "height": 1}},
                }
            },
            "faces": {"01": {"label": "平常", "primary": "gentle"}},
            "expression_sets": {"gentle": [{"id": "01", "strength": 3}]},
        }
        (root / "expressions.json").write_text(json.dumps(v2_data), encoding="utf-8")

        with caplog.at_level(logging.WARNING):
            loaded = load_portrait_manifest(root)
        assert loaded is not None
        assert "01" in loaded["sprites"]["uniform"]
        assert "layers" not in loaded
        assert "Invalid layers.json" in caplog.text


# ===========================================================================
# 3. Normalized Payload Shape
# ===========================================================================


class TestNormalizedPayloadShape:
    def test_normalized_v3_keeps_v2_keys(self, tmp_path):
        root, data = make_valid_v3(tmp_path)
        normalized = _normalize_v3(root, data)

        assert normalized["enabled"] is True
        assert normalized["version"] == 3
        assert normalized["default_costume"] == "uniform"
        assert normalized["defaultOutfit"] == "uniform"
        assert normalized["default_pose"] == "a"
        assert normalized["defaultPose"] == "a"

        # Costumes list with geometry
        assert isinstance(normalized["costumes"], list)
        assert len(normalized["costumes"]) == 2
        c0 = next(c for c in normalized["costumes"] if c["id"] == "uniform")
        assert c0["name"] == "校服"
        assert c0["crop"] == {"left": 540, "top": 865, "width": 792, "height": 1623}
        assert c0["faceRect"] == {"x": 175, "y": 0, "width": 362, "height": 325}
        assert c0["headAnchor"] == {"x": 356, "y": 2}

        # Faces metadata
        assert "faces" in normalized
        assert "01" in normalized["faces"]
        f01 = normalized["faces"]["01"]
        assert f01["label"] == "平常"
        assert f01["primary"] == "gentle"
        assert f01["weights"] == {"gentle": 3}

        # Expression sets per chat emotion
        assert "expression_sets" in normalized
        assert "gentle" in normalized["expression_sets"]
        assert any(item["id"] == "01" for item in normalized["expression_sets"]["gentle"])

        # Layered v3 data present
        assert "canvases" in normalized
        assert "poses" in normalized
        assert "looks" in normalized
        assert "layers" in normalized
        assert "outfits" in normalized


# ===========================================================================
# 4. Signed Layer URLs and Path Safety
# ===========================================================================


@pytest.fixture
def v3_character_pack(tmp_path, monkeypatch):
    root, data = make_valid_v3(tmp_path)
    pkg_dir = tmp_path / "characters" / "V3Heroine"
    pkg_dir.mkdir(parents=True)
    manifest = {"id": "v3heroine", "name": "V3Heroine"}
    (pkg_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (pkg_dir / "secret.txt").write_text("TOP_SECRET", encoding="utf-8")

    import shutil
    shutil.copytree(root, pkg_dir / "portrait")
    (pkg_dir / "portrait" / "layers.json").write_text(json.dumps(data), encoding="utf-8")

    pkg_obj = SimpleNamespace(folder=pkg_dir, manifest=SimpleNamespace(id="v3heroine", name="V3Heroine"))
    monkeypatch.setattr(
        characters_router,
        "get_character_manager",
        lambda: SimpleNamespace(
            get_character=lambda name: pkg_obj if name in ("V3Heroine", "v3heroine") else None,
            discover_characters=lambda: [pkg_obj],
            sync_with_db=lambda conn: 1,
        ),
    )
    return SimpleNamespace(pkg_dir=pkg_dir, portrait_dir=pkg_dir / "portrait", pkg_obj=pkg_obj)


@pytest.fixture
async def setup_v3_character_db(isolate_test_database, v3_character_pack):
    async with get_db(isolate_test_database) as db_conn:
        profile = await crud.create_voice_profile(
            db_conn,
            VoiceProfileCreate(
                name="V3Heroine",
                gpt_weights_path="v3.ckpt",
                sovits_weights_path="v3.pth",
                ref_audio_path="v3.wav",
                prompt_text="こんにちは",
                prompt_lang="ja",
                text_lang="ja",
            ),
        )
    return profile


class TestSignedLayerUrlsAndPathSafety:
    @pytest.mark.asyncio
    async def test_manifest_rewrites_signed_layer_urls(self, setup_v3_character_db):
        profile = setup_v3_character_db
        app = create_app()
        transport = httpx.ASGITransport(app=app)

        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(f"/api/characters/{profile.id}/portrait")
            assert resp.status_code == 200
            data = resp.json()
            assert data["enabled"] is True
            assert data["version"] == 3

            layer = data["layers"]["A/1278"]
            assert layer["file"].startswith(f"/api/media/portrait/{profile.id}/layers/A/1278.webp?")
            assert "sig=" in layer["file"]
            assert "exp=" in layer["file"]

            # Load the image through the signed URL
            img_resp = await client.get(layer["file"])
            assert img_resp.status_code == 200
            assert img_resp.content == TINY_WEBP
            assert "max-age" in img_resp.headers.get("Cache-Control", "")

    @pytest.mark.asyncio
    async def test_tampered_signature_returns_403(self, setup_v3_character_db):
        profile = setup_v3_character_db
        app = create_app()
        transport = httpx.ASGITransport(app=app)

        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            path = f"/api/media/portrait/{profile.id}/layers/A/1278.webp"
            signed = sign_media_url(path)
            bad_url = signed[:-4] + "0000"
            resp = await client.get(bad_url)
            assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_path_traversal_on_layer_endpoint_rejected(self, setup_v3_character_db):
        profile = setup_v3_character_db
        app = create_app()
        transport = httpx.ASGITransport(app=app)

        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            base = f"/api/media/portrait/{profile.id}"
            exp = compute_bucketed_expiry()

            for target in (
                f"{base}/layers/../../secret.txt",
                f"{base}/layers/..%2F..%2Fsecret.txt",
                f"{base}/layers/A/../../../secret.txt",
                f"{base}/layers/A/secret.txt",
            ):
                sig = generate_media_signature(target, exp, v="")
                url = f"{target}?exp={exp}&sig={sig}"
                resp = await client.get(url)
                assert resp.status_code in (400, 404)
                assert b"TOP_SECRET" not in resp.content


# ===========================================================================
# 5. Acting Menu for v3 Packs
# ===========================================================================


class TestActingMenuV3:
    def test_acting_menu_poses_and_faces_for_outfit(self, tmp_path):
        root, data = make_valid_v3(tmp_path)
        normalized = _normalize_v3(root, data)

        # 1. Outfit 'uniform' has looks for poses 'a' and 'b'
        menu_uniform = build_acting_menu(normalized, "uniform")
        assert menu_uniform["outfit"] == "uniform"
        assert menu_uniform["poses"] == ["a", "b"]
        # Default look for uniform is 'a/uniform'
        assert menu_uniform["faces"] == []  # No label is available in both looks.
        assert menu_uniform["blush"] is True

        # 2. Outfit 'casual' has look only for pose 'a'
        menu_casual = build_acting_menu(normalized, "casual")
        assert menu_casual["outfit"] == "casual"
        assert menu_casual["poses"] == ["a"]
        assert menu_casual["faces"] == ["平常"]
        assert menu_casual["blush"] is False

    def test_sanitize_act_validates_pose_first_then_face_against_look(self, tmp_path):
        root, data = make_valid_v3(tmp_path)
        normalized = _normalize_v3(root, data)
        menu = build_acting_menu(normalized, "uniform")

        # Pose 'b' has face '惊讶', which is NOT in pose 'a'
        act_b = {"pose": "b", "face": "惊讶"}
        clean_b = sanitize_act(act_b, menu, line_index=0, character="测试")
        assert clean_b == {"pose": "b", "face": "惊讶"}

        # Pose 'a' does NOT have face '惊讶'
        act_a_bad_face = {"pose": "a", "face": "惊讶"}
        clean_a = sanitize_act(act_a_bad_face, menu, line_index=1, character="测试")
        assert clean_a == {"pose": "a"}  # '惊讶' dropped for pose 'a'

        # Invalid pose dropped, effective look falls back to current_pose / default_pose
        act_bad_pose = {"pose": "c", "face": "平常"}
        clean_bad = sanitize_act(act_bad_pose, menu, line_index=2, character="测试", current_pose="a")
        assert clean_bad == {"face": "平常"}  # pose 'c' dropped, face '平常' valid for pose 'a'

        # Invalid pose dropped, face from pose 'b' evaluated against pose 'a' is dropped
        act_bad_both = {"pose": "c", "face": "惊讶"}
        clean_bad_both = sanitize_act(act_bad_both, menu, line_index=3, character="测试", current_pose="a")
        assert clean_bad_both == {}

    def test_v1_and_v2_acting_menu_poses_stays_empty(self):
        v2_payload = {
            "version": 2,
            "default_costume": "uniform",
            "costumes": [{"id": "uniform"}],
            "sprites": {"uniform": {"01": {"file": "01.png"}}},
            "faces": {"01": {"label": "平常"}},
        }
        menu = build_acting_menu(v2_payload)
        assert menu["poses"] == []
        assert sanitize_act({"pose": "a", "face": "平常"}, menu, line_index=0, character="测试") == {"face": "平常"}
