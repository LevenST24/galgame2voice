"""
Test Character Standing CG Portrait System (Galgame Sprites & Stage).
Verifies:
1. Manifest resolution and outfit configurations.
2. Character portrait REST API endpoint (/api/characters/{id}/portrait).
3. Static sprite file serving and accessibility.
"""

import pytest
from httpx import ASGITransport, AsyncClient
from galgame2voice.main import app
from galgame2voice.database.session import init_db, get_db
from galgame2voice.services.character_manager import get_character_manager


@pytest.fixture(autouse=True)
async def setup_test_characters():
    await init_db()
    async with get_db() as db:
        mgr = get_character_manager()
        await mgr.sync_with_db(db)


@pytest.mark.asyncio
async def test_character_list_includes_portrait_metadata():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get("/api/characters?user_id=test_user")
        assert res.status_code == 200
        data = res.json()
        assert "characters" in data
        assert data["total"] > 0
        
        # Check if Shiki Natsume has portrait enabled
        natsume_entry = next((c for c in data["characters"] if "夏目" in c["name"] or "natsume" in c["name"].lower()), None)
        assert natsume_entry is not None
        assert "portrait" in natsume_entry
        assert natsume_entry["portrait"] is not None
        assert natsume_entry["portrait"]["character_id"] == "natsume"
        assert "costumes" in natsume_entry["portrait"]
        assert len(natsume_entry["portrait"]["costumes"]) >= 2


@pytest.mark.asyncio
async def test_character_portrait_endpoint():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Find character id
        res = await client.get("/api/characters?user_id=test_user")
        data = res.json()
        natsume_entry = next((c for c in data["characters"] if "夏目" in c["name"] or "natsume" in c["name"].lower()), None)
        assert natsume_entry is not None
        char_id = natsume_entry["id"]

        # Query portrait manifest
        res_portrait = await client.get(f"/api/characters/{char_id}/portrait")
        assert res_portrait.status_code == 200
        portrait_data = res_portrait.json()
        assert portrait_data["enabled"] is True
        assert portrait_data["character_name"] == "四季夏目"
        assert "costumes" in portrait_data
        costume_ids = [c["id"] for c in portrait_data["costumes"]]
        assert "cafe_uniform" in costume_ids
        assert "casual" in costume_ids


@pytest.mark.asyncio
async def test_static_sprite_assets_served():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Verify cafe_uniform/gentle.png is accessible
        res = await client.get("/static/characters/natsume/cafe_uniform/gentle.png")
        assert res.status_code == 200
        assert res.headers["content-type"].startswith("image/png")
        assert len(res.content) > 1000

        # Verify casual/happy.png is accessible
        res_casual = await client.get("/static/characters/natsume/casual/happy.png")
        assert res_casual.status_code == 200
        assert res_casual.headers["content-type"].startswith("image/png")
