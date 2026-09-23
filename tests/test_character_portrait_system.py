"""
Test Character Standing CG Portrait System (galgame sprites & stage).

The portrait system is driven by the character package's numbered face
differentials (portrait/expressions.json), so these tests cover manifest
resolution, the emotion pools that drive per-sentence drift, and sprite serving.
"""

import pytest
from httpx import ASGITransport, AsyncClient
from galgame2voice.main import app
from galgame2voice.database.session import init_db, get_db
from galgame2voice.services.character_manager import get_character_manager

CHAT_EMOTIONS = {"gentle", "happy", "shy", "tsundere", "cool", "sad", "angry"}


@pytest.fixture(autouse=True)
async def setup_test_characters():
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
async def test_sprite_path_traversal_is_rejected():
    async with await _client() as client:
        char_id = await _natsume_id(client)
        res = await client.get(
            f"/api/characters/{char_id}/portrait/file/..%2F/expressions.json"
        )
        assert res.status_code in (400, 404)
