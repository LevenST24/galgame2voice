"""P3 settings travel through the actual SQLite backup export/import endpoints."""
import json

import pytest
from httpx import ASGITransport, AsyncClient

from galgame2voice.database.session import get_db, init_db
from galgame2voice.main import create_app
from galgame2voice.services.chat_backup import SESSION_SETTINGS
from galgame2voice.routers.chat import ImportedSession

P3_SETTINGS = {
    "advEnabled": True, "advPlayerName": "旅行者", "advPrimaryLanguage": "ja",
    "advSubtitleLanguage": "zh", "advWindowOpacity": 0, "advTextIntervalMs": 100,
    "advInterruptVoice": False, "advAutoBaseMs": 6000, "advAutoPerCharMs": 200,
    "advAutoMode": "normal", "advOutfit": "uniform_2", "advBlink": True, "advLipSync": True,
    "advBackground": "cafe", "advBgTime": "evening",
}

INVALID_P3 = [
    ("advEnabled", "false"), ("advInterruptVoice", 1),
    ("advPrimaryLanguage", "en"), ("advSubtitleLanguage", "en"), ("advAutoMode", "fast"), ("advReplyFormat", "new"),
    ("advWindowOpacity", -1), ("advWindowOpacity", 101), ("advWindowOpacity", True),
    ("advTextIntervalMs", 1.5), ("advTextIntervalMs", 101),
    ("advAutoBaseMs", 6001), ("advAutoBaseMs", -1), ("advAutoPerCharMs", 201),
    ("advAutoPerCharMs", "45"), ("advPlayerName", ""), ("advPlayerName", "  "),
    ("advPlayerName", 17), ("advPlayerName", "人" * 401),
    ("advPlayerName", "\ufeff"),
    ("advPlayerName", "👩🏽‍💻" * 101), ("advOutfit", "../other"), ("advOutfit", None),
    ("advBlink", "false"), ("advBlink", 1), ("advLipSync", "true"), ("advLipSync", 0),
    ("advBackground", "../cafe"), ("advBackground", "a" * 65), ("advBackground", "咖啡店"),
    ("advBackground", "cafe\n"), ("advBackground", None), ("advBackground", 1),
    ("advBgTime", "dawn"), ("advBgTime", ""), ("advBgTime", None), ("advBgTime", 1),
]


@pytest.mark.parametrize("background", ["", "A_-09", "a" * 64])
@pytest.mark.parametrize("time", ["auto", "day", "evening", "night"])
def test_background_settings_accept_portable_ids_and_all_times(background, time):
    settings = {"advBackground": background, "advBgTime": time}
    assert set(settings) <= SESSION_SETTINGS
    session = ImportedSession(id="s_import_background", title="background", messages=[], settings=settings)
    assert session.settings == settings


@pytest.mark.parametrize("key,value", INVALID_P3)
def test_import_drops_invalid_p3_values_preserving_other_settings(key, value):
    session = ImportedSession(id="s_import_check", title="check", messages=[],
                              settings={key: value, "temperature": 0.7, "custom": {"keep": True}})
    assert key not in session.settings
    assert session.settings["temperature"] == 0.7
    assert session.settings["custom"] == {"keep": True}


@pytest.mark.asyncio
async def test_invalid_p3_export_import_round_trip(tmp_path, monkeypatch):
    db = tmp_path / "invalid-values.db"
    monkeypatch.setenv("GALGAME2VOICE_DB_PATH", str(db))
    await init_db(db)
    invalid = dict(INVALID_P3)
    async with get_db(db) as conn:
        await conn.execute("INSERT INTO sessions (id, title, settings_json) VALUES (?, ?, ?)",
                           ("s_bad", "bad", json.dumps({**invalid, "temperature": 0.9})))
        await conn.commit()
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test") as client:
        settings = (await client.get("/api/chat/export")).json()["sessions"][0]["settings"]
        assert not (set(invalid) & set(settings))
        assert settings["temperature"] == 0.9
        response = await client.post("/api/chat/import", json={"sessions": [{
            "id": "s_import_bad", "title": "bad", "messages": [], "settings": {**invalid, "temperature": 0.9},
        }]})
        assert response.status_code == 200, response.text
        async with get_db(db) as conn:
            row = await (await conn.execute("SELECT settings_json FROM sessions WHERE id='s_import_bad'")).fetchone()
        assert json.loads(row[0]) == {"temperature": 0.9, "voiceProfileId": None}
        restored = (await client.get("/api/chat/export")).json()["sessions"]
        assert next(s for s in restored if s["id"] == "s_import_bad")["settings"] == settings


def test_duplicate_subtitle_is_dropped_at_import_boundary():
    session = ImportedSession(id="s_import_same", title="same", messages=[], settings={
        "advPrimaryLanguage": "ja", "advSubtitleLanguage": "ja", "temperature": 0.6,
    })
    assert session.settings == {"advPrimaryLanguage": "ja", "temperature": 0.6}


@pytest.mark.parametrize("name", ["\u0085", "\u001c"])
def test_name_whitespace_matches_javascript_trim(name):
    session = ImportedSession(id="s_import_space", title="space", messages=[], settings={"advPlayerName": name})
    assert session.settings["advPlayerName"] == name


@pytest.mark.asyncio
@pytest.mark.parametrize("grapheme", ["e\u0301", "👩🏽‍💻", "🇨🇳", "\r\n", "한", "क्ष", "क\u093e"])
async def test_player_name_of_40_graphemes_round_trips(tmp_path, monkeypatch, grapheme):
    name = "P" + grapheme * 39
    db = tmp_path / "graphemes.db"
    monkeypatch.setenv("GALGAME2VOICE_DB_PATH", str(db))
    await init_db(db)
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test") as client:
        response = await client.post("/api/chat/import", json={"sessions": [{
            "id": "s_import_name", "title": "name", "messages": [], "settings": {"advPlayerName": name},
        }]})
        assert response.status_code == 200, response.text
        assert (await client.get("/api/chat/export")).json()["sessions"][0]["settings"]["advPlayerName"] == name


@pytest.mark.asyncio
async def test_all_p3_settings_survive_backup_export_and_import(tmp_path, monkeypatch):
    db = tmp_path / "p3.db"
    monkeypatch.setenv("GALGAME2VOICE_DB_PATH", str(db))
    await init_db(db)
    assert set(P3_SETTINGS) <= SESSION_SETTINGS
    async with get_db(db) as conn:
        await conn.execute("INSERT INTO sessions (id, title, settings_json) VALUES (?, ?, ?)", (
            "s_adv", "ADV", json.dumps({**P3_SETTINGS, "privateKey": "never-export"}),
        ))
        await conn.commit()
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test") as client:
        response = await client.get("/api/chat/export")
        assert response.status_code == 200
        assert "never-export" not in response.text
        session = next(s for s in response.json()["sessions"] if s["id"] == "s_adv")
        for key, value in P3_SETTINGS.items():
            assert session["settings"][key] == value
        session["id"] = "s_import_adv"
        result = await client.post("/api/chat/import", json={"sessions": [session]})
        assert result.status_code == 200, result.text
        restored = (await client.get("/api/chat/export")).json()
        copy = next(s for s in restored["sessions"] if s["id"] == "s_import_adv")
        for key, value in P3_SETTINGS.items():
            assert copy["settings"][key] == value


@pytest.mark.asyncio
@pytest.mark.parametrize("settings", [[], "invalid", 17, None])
async def test_backup_rejects_invalid_settings_container_atomically(tmp_path, monkeypatch, settings):
    db = tmp_path / "invalid.db"
    monkeypatch.setenv("GALGAME2VOICE_DB_PATH", str(db))
    await init_db(db)
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test") as client:
        response = await client.post("/api/chat/import", json={"sessions": [
            {"id": "s_import_valid", "title": "valid", "messages": [], "settings": P3_SETTINGS},
            {"id": "s_import_invalid", "title": "invalid", "messages": [], "settings": settings},
        ]})
        assert response.status_code == 422
        assert (await client.get("/api/chat/export")).json()["sessions"] == []
