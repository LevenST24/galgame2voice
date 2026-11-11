"""Contract and legacy-data compatibility tests after Telegram bot removal."""

import importlib.util

import pytest
from httpx import ASGITransport, AsyncClient

from galgame2voice.database.session import get_db, init_db
from galgame2voice.main import create_app


def test_telegram_modules_are_removed():
    """Deleted Telegram bot and token-sanitization modules must not be importable."""
    tg_spec = importlib.util.find_spec("galgame2voice.telegram_bot")
    if tg_spec is not None:
        assert tg_spec.origin is None
        for submodule in ("bot", "handlers", "console_menus", "proxy"):
            assert importlib.util.find_spec(f"galgame2voice.telegram_bot.{submodule}") is None
    assert importlib.util.find_spec("galgame2voice.utils.text_sanitize") is None


@pytest.mark.asyncio
async def test_telegram_routes_are_removed():
    """No registered FastAPI route may expose Telegram paths, and /api/telegram/test returns 404."""
    app = create_app()
    route_paths = [getattr(route, "path", "") for route in app.routes]
    assert all("telegram" not in path.lower() for path in route_paths)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/telegram/test", json={"token": "123456:ABCDEF"})
        assert resp.status_code == 404


@pytest.mark.asyncio
async def test_legacy_telegram_sqlite_data_and_backup_compatibility(isolate_test_database):
    """Existing SQLite Telegram columns/sessions and legacy chat backups remain compatible."""
    await init_db(isolate_test_database)

    async with get_db(isolate_test_database) as conn:
        await conn.execute(
            "UPDATE settings SET telegram_enabled = 1, telegram_bot_token = '123456:ABCDEF' WHERE id = 1;"
        )
        await conn.execute(
            "INSERT INTO sessions (id, channel, title) VALUES ('tg_legacy_1', 'telegram', '旧TG对话');"
        )
        await conn.execute(
            "INSERT INTO messages (session_id, role, content_chinese, content_japanese) "
            "VALUES ('tg_legacy_1', 'assistant', '你好，这是旧Telegram记录', 'こんにちは');"
        )
        await conn.commit()

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 1. GET /api/config omits telegram_* keys without wiping SQLite columns
        get_cfg = await client.get("/api/config")
        assert get_cfg.status_code == 200
        cfg_settings = get_cfg.json()["settings"]
        assert not any(key.startswith("telegram_") for key in cfg_settings)

        async with get_db(isolate_test_database) as conn:
            row = await (
                await conn.execute(
                    "SELECT telegram_enabled, telegram_bot_token FROM settings WHERE id = 1;"
                )
            ).fetchone()
            assert row["telegram_enabled"] == 1
            assert row["telegram_bot_token"] == "123456:ABCDEF"

        # 2. POST /api/config ignores legacy telegram_* keys and updates valid settings
        post_cfg = await client.post(
            "/api/config",
            json={
                "settings": {
                    "speed_factor": 1.1,
                    "telegram_enabled": True,
                    "telegram_bot_token": "x",
                }
            },
        )
        assert post_cfg.status_code == 200
        updated_settings = post_cfg.json()["settings"]
        assert updated_settings["speed_factor"] == 1.1
        assert not any(key.startswith("telegram_") for key in updated_settings)

        async with get_db(isolate_test_database) as conn:
            row_after = await (
                await conn.execute(
                    "SELECT telegram_enabled, telegram_bot_token FROM settings WHERE id = 1;"
                )
            ).fetchone()
            assert row_after["telegram_enabled"] == 1
            assert row_after["telegram_bot_token"] == "123456:ABCDEF"

        # 3. GET /api/chat/sessions still lists legacy Telegram sessions
        sess_resp = await client.get("/api/chat/sessions")
        assert sess_resp.status_code == 200
        sessions = sess_resp.json()["sessions"]
        legacy_session = next((s for s in sessions if s["id"] == "tg_legacy_1"), None)
        assert legacy_session is not None
        assert legacy_session["title"] == "旧TG对话"
        assert legacy_session["message_count"] == 1

        # 4. POST /api/chat/import accepts legacy backup payloads carrying channel="telegram" and unknown telegram_* keys
        import_resp = await client.post(
            "/api/chat/import",
            json={
                "format": "galgame2voice.chat",
                "version": 1,
                "global": {
                    "telegram_enabled": True,
                    "telegram_bot_token": "123456:SECRET",
                },
                "sessions": [
                    {
                        "id": "s_import_tg_backup_1",
                        "orig_id": "tg_backup_1",
                        "channel": "telegram",
                        "telegram_bot_username": "legacy_bot",
                        "title": "旧TG备份导入",
                        "settings": {
                            "temperature": 0.7,
                            "telegram_chat_id": "123456",
                            "telegram_admin_ids": "123456",
                        },
                        "messages": [
                            {
                                "role": "assistant",
                                "content": "旧备份消息",
                                "japanese": "古いメッセージ",
                            }
                        ],
                    }
                ],
            },
        )
        assert import_resp.status_code == 200, import_resp.text
        assert import_resp.json() == {"status": "ok", "count": 1}

        exported = await client.get("/api/chat/export")
        assert exported.status_code == 200
        exported_sessions = {s["id"]: s for s in exported.json()["sessions"]}
        assert "tg_legacy_1" in exported_sessions
        assert "s_import_tg_backup_1" in exported_sessions
        assert not any(
            k.startswith("telegram_")
            for k in exported_sessions["s_import_tg_backup_1"]["settings"]
        )
