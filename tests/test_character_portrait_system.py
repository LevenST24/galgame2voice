"""
Test Character Standing CG Portrait System (galgame sprites & stage).

The portrait system is driven by the character package's numbered face
differentials (portrait/expressions.json), so these tests cover manifest
resolution, the emotion pools that drive per-sentence drift, and sprite serving.
"""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from galgame2voice.main import app
from galgame2voice.database import crud
from galgame2voice.database.models import VoiceProfileCreate
from galgame2voice.database.session import init_db, get_db
from galgame2voice.routers import characters as characters_router
from galgame2voice.schemas.character_manifest import clean_character_work
from galgame2voice.services import character_manager as character_manager_module
from galgame2voice.services.character_manager import get_character_manager, get_character_work

CHAT_EMOTIONS = {"gentle", "happy", "shy", "tsundere", "cool", "sad", "angry"}


@pytest.fixture(autouse=True)
async def setup_test_characters(request):
    marker = request.node.get_closest_marker("requires_character_assets")
    if marker is not None:
        mgr = get_character_manager()
        pkg = mgr.get_character("四季夏目")
        if not pkg or not (pkg.folder / "portrait" / "expressions.json").is_file():
            pytest.skip("四季夏目 numbered differential expressions.json asset not present")
    await init_db()
    async with get_db() as db:
        mgr = get_character_manager()
        await mgr.sync_with_db(db)


async def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _natsume_id(client) -> int:
    res = await client.get("/api/characters?user_id=test_user")
    assert res.status_code == 200
    entry = next(
        (c for c in res.json()["characters"] if "夏目" in c["name"] or "natsume" in c["name"].lower()),
        None,
    )
    assert entry is not None
    return entry["id"]


@pytest.mark.requires_character_assets
@pytest.mark.asyncio
async def test_character_list_includes_portrait_metadata():
    async with await _client() as client:
        res = await client.get("/api/characters?user_id=test_user")
        data = res.json()
        assert data["total"] > 0

        natsume_entry = next(
            (c for c in data["characters"] if "夏目" in c["name"] or "natsume" in c["name"].lower()),
            None,
        )
        assert natsume_entry is not None
        portrait = natsume_entry["portrait"]
        assert portrait is not None
        assert portrait["character_id"] == "natsume"
        assert len(portrait["costumes"]) >= 1


@pytest.mark.requires_character_assets
@pytest.mark.asyncio
async def test_portrait_payload_is_numbered_face_differentials():
    async with await _client() as client:
        char_id = await _natsume_id(client)
        res = await client.get(f"/api/characters/{char_id}/portrait")
        assert res.status_code == 200
        portrait = res.json()

        assert portrait["enabled"] is True
        assert portrait["character_name"] == "四季夏目"

        costume_ids = [c["id"] for c in portrait["costumes"]]
        assert portrait["default_costume"] in costume_ids

        # Every face carries the part decomposition the drift picker needs.
        faces = portrait["faces"]
        assert faces
        for face_id, meta in faces.items():
            assert meta["primary"]
            assert meta["label"]
            assert isinstance(meta["parts"], list) and meta["parts"]
            assert "blush" in meta

        # Each emotion the chat pipeline can request owns a non-empty pool.
        pools = portrait["expression_sets"]
        assert CHAT_EMOTIONS <= set(pools)
        for emotion in CHAT_EMOTIONS:
            assert pools[emotion], f"emotion pool {emotion} is empty"
            for entry in pools[emotion]:
                assert entry["id"] in faces, f"{emotion} pool references unknown face {entry['id']}"

        # Sprites are indexed by face number and served from the package endpoint.
        for costume_id in costume_ids:
            sprites = portrait["sprites"][costume_id]
            assert sprites
            for face_id, sprite in sprites.items():
                assert sprite["file"] == (
                    f"/api/characters/{char_id}/portrait/file/{costume_id}/{face_id}.webp"
                )


@pytest.mark.requires_character_assets
@pytest.mark.asyncio
async def test_face_and_blush_sprites_are_served():
    async with await _client() as client:
        char_id = await _natsume_id(client)
        portrait = (await client.get(f"/api/characters/{char_id}/portrait")).json()
        costume_id = portrait["default_costume"]
        face_id = portrait["expression_sets"]["gentle"][0]["id"]

        res = await client.get(f"/api/characters/{char_id}/portrait/file/{costume_id}/{face_id}.webp")
        assert res.status_code == 200
        assert len(res.content) > 1000

        blush = portrait["faces"][face_id].get("blush")
        if blush:
            res_blush = await client.get(
                f"/api/characters/{char_id}/portrait/file/{costume_id}/{blush}.webp"
            )
            assert res_blush.status_code == 200


@pytest.mark.asyncio
async def test_sprite_path_traversal_is_rejected(tmp_path, monkeypatch):
    """Sprite requests stay inside the package's portrait folder (no installed package needed)."""
    package = tmp_path / "演示角色"
    (package / "portrait" / "casual").mkdir(parents=True)
    (package / "portrait" / "casual" / "gentle.png").write_bytes(b"sprite")
    (package / "portrait" / "expressions.json").write_text("{}", encoding="utf-8")
    (package / "secret.txt").write_text("package secret", encoding="utf-8")
    found = SimpleNamespace(folder=package, manifest=SimpleNamespace(id="demo", name="演示角色"))
    monkeypatch.setattr(characters_router, "get_character_manager", lambda: SimpleNamespace(
        get_character=lambda name: found if name == "演示角色" else None,
    ))
    async with get_db() as conn:
        await crud.get_settings_raw(conn)
        profile = await crud.create_voice_profile(conn, VoiceProfileCreate(
            name="演示角色", gpt_weights_path="demo.ckpt", sovits_weights_path="demo.pth",
            ref_audio_path="demo.wav", prompt_text="テスト", prompt_lang="ja", text_lang="ja",
        ))

    async with await _client() as client:
        base = f"/api/characters/{profile.id}/portrait/file"
        served = await client.get(f"{base}/casual/gentle.png")
        assert served.status_code == 200 and served.content == b"sprite"
        for path in ("..%2F/expressions.json", "casual/..%2F..%2Fsecret.txt", "..%5C..%5Csecret.txt/x"):
            res = await client.get(f"{base}/{path}")
            assert res.status_code in (400, 404), path
            assert b"package secret" not in res.content

    # The handler itself, whatever the URL normalisation in front of it.
    for costume, file_name in (("..", "secret.txt"), ("casual", ".."), ("casual", "../../secret.txt"), ("casual", r"..\..\secret.txt")):
        with pytest.raises(HTTPException) as rejected:
            await characters_router.get_character_portrait_file(profile.id, costume, file_name)
        assert rejected.value.status_code in (400, 404)


def _fake_package(tmp_path, name, work, *, is_valid=True):
    """A discovered character package exposing the manifest fields the API reads."""
    folder = tmp_path / name
    (folder / "portrait").mkdir(parents=True)
    manifest = SimpleNamespace(id=f"id_{name}", name=name, work=work)
    return SimpleNamespace(folder=folder, manifest=manifest, is_valid=is_valid)


def _patch_manager(monkeypatch, packages):
    by_name = {pkg.manifest.name: pkg for pkg in packages}

    def fake_manager():
        return SimpleNamespace(get_character=lambda name: by_name.get(name))

    monkeypatch.setattr(characters_router, "get_character_manager", fake_manager)
    monkeypatch.setattr(character_manager_module, "get_character_manager", fake_manager)


async def _make_profile(name, weights):
    async with get_db() as conn:
        await crud.get_settings_raw(conn)
        return await crud.create_voice_profile(conn, VoiceProfileCreate(
            name=name, gpt_weights_path=weights + ".ckpt", sovits_weights_path=weights + ".pth",
            ref_audio_path=weights + ".wav", prompt_text="テスト", prompt_lang="ja", text_lang="ja",
        ))


@pytest.mark.asyncio
async def test_character_work_is_exposed_on_the_profile_apis(tmp_path, monkeypatch):
    """A character's declared background work surfaces on every endpoint the frontend reads."""
    pkg = _fake_package(tmp_path, "作品角色", "cafe_stella")
    _patch_manager(monkeypatch, [pkg])
    profile = await _make_profile("作品角色", "work_role")

    async with await _client() as client:
        entry = next(c for c in (await client.get("/api/characters?user_id=test_user")).json()["characters"]
                     if c["id"] == profile.id)
        assert entry["work"] == "cafe_stella"
        prof = next(p for p in (await client.get("/api/voice/profiles")).json()["profiles"]
                    if p["id"] == profile.id)
        assert prof["work"] == "cafe_stella"


@pytest.mark.asyncio
async def test_character_reports_null_work_when_absent_or_invalid(tmp_path, monkeypatch):
    invalid = _fake_package(tmp_path, "无作品角色", "../etc")
    _patch_manager(monkeypatch, [invalid])
    profile = await _make_profile("无作品角色", "no_work")

    async with await _client() as client:
        entry = next(c for c in (await client.get("/api/characters?user_id=test_user")).json()["characters"]
                     if c["id"] == profile.id)
        assert entry["work"] is None
        prof = next(p for p in (await client.get("/api/voice/profiles")).json()["profiles"]
                    if p["id"] == profile.id)
        assert prof["work"] is None


def test_resolve_character_portrait_payload_carries_work(tmp_path, monkeypatch):
    pkg = _fake_package(tmp_path, "作品角色", "cafe_stella")
    _patch_manager(monkeypatch, [pkg])
    monkeypatch.setattr(characters_router, "load_portrait_manifest", lambda folder: {"costumes": []})
    assert characters_router._resolve_character_portrait("作品角色")["work"] == "cafe_stella"


def test_get_character_work_skips_unknown_and_invalid_packages(tmp_path, monkeypatch):
    _patch_manager(monkeypatch, [
        _fake_package(tmp_path, "有作品", "cafe_stella"),
        _fake_package(tmp_path, "坏包", "cafe_stella", is_valid=False),
        _fake_package(tmp_path, "坏作品", "../etc"),
    ])
    assert get_character_work("有作品") == "cafe_stella"
    assert [get_character_work(name) for name in ("坏包", "坏作品", "不存在", "")] == [None] * 4


@pytest.mark.parametrize("raw,expected", [
    ("cafe_stella", "cafe_stella"), ("A_-09", "A_-09"), ("a" * 64, "a" * 64),
    (None, None), ("", None), ("../cafe", None), ("cafe/day", None),
    ("咖啡店", None), ("a" * 65, None), ("cafe\n", None), (7, None),
])
def test_clean_character_work_only_accepts_pack_ids(raw, expected):
    assert clean_character_work(raw) == expected
