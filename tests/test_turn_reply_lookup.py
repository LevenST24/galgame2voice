"""Unit and integration tests for turn reply status lookup endpoint."""

import asyncio
import os
import sqlite3
import pytest
from httpx import ASGITransport, AsyncClient

from galgame2voice.database import crud
from galgame2voice.database.migrations import CURRENT_SCHEMA_VERSION, run_schema_migrations
from galgame2voice.database.models import MessageCreate
from galgame2voice.database.session import get_db, init_db
from galgame2voice.main import create_app


@pytest.fixture
async def lookup_db(tmp_path):
    db_path = tmp_path / "lookup_test.db"
    await init_db(db_path)
    os.environ["GALGAME2VOICE_DB_PATH"] = str(db_path)
    yield db_path
    os.environ.pop("GALGAME2VOICE_DB_PATH", None)


@pytest.mark.asyncio
async def test_lookup_status_ready_and_pending(lookup_db):
    app = create_app()
    transport = ASGITransport(app=app)

    async with get_db(lookup_db) as conn:
        user_msg = await crud.add_message(conn, MessageCreate(
            session_id="sess_1", role="user", content_chinese="你好"
        ))

    async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as client:
        # 1. Pending: user exists, assistant message not committed yet
        resp = await client.get(f"/api/chat/turn-reply?session_id=sess_1&user_message_id={user_msg.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "pending"
        assert data["session_id"] == "sess_1"
        assert data["user_message_id"] == user_msg.id
        assert "message_id" not in data

        # 2. Ready: commit assistant message linked to user_msg.id
        async with get_db(lookup_db) as conn:
            asst_msg = await crud.add_message(conn, MessageCreate(
                session_id="sess_1", role="assistant", content_chinese="回答",
                reply_to_message_id=user_msg.id
            ))

        resp_ready = await client.get(f"/api/chat/turn-reply?session_id=sess_1&user_message_id={user_msg.id}")
        assert resp_ready.status_code == 200
        data_ready = resp_ready.json()
        assert data_ready["status"] == "ready"
        assert data_ready["session_id"] == "sess_1"
        assert data_ready["user_message_id"] == user_msg.id
        assert data_ready["message_id"] == asst_msg.id


@pytest.mark.asyncio
async def test_lookup_status_gone(lookup_db):
    app = create_app()
    transport = ASGITransport(app=app)

    async with get_db(lookup_db) as conn:
        user_msg = await crud.add_message(conn, MessageCreate(
            session_id="sess_gone", role="user", content_chinese="删除测试"
        ))

    async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as client:
        # Delete the user message
        async with get_db(lookup_db) as conn:
            await crud.delete_message(conn, user_msg.id)

        resp = await client.get(f"/api/chat/turn-reply?session_id=sess_gone&user_message_id={user_msg.id}")
        assert resp.status_code == 200
        assert resp.json()["status"] == "gone"

        # Non-existent ID is gone
        resp_nonexist = await client.get("/api/chat/turn-reply?session_id=sess_gone&user_message_id=999999")
        assert resp_nonexist.status_code == 200
        assert resp_nonexist.json()["status"] == "gone"


@pytest.mark.asyncio
async def test_lookup_cross_session_isolation(lookup_db):
    """
    Querying session B with a valid user message ID from session A must return gone
    and must not leak existence or identity of records across sessions.
    """
    app = create_app()
    transport = ASGITransport(app=app)

    async with get_db(lookup_db) as conn:
        user_msg_a = await crud.add_message(conn, MessageCreate(
            session_id="session_A", role="user", content_chinese="隐私提问"
        ))
        await crud.add_message(conn, MessageCreate(
            session_id="session_A", role="assistant", content_chinese="隐私回答",
            reply_to_message_id=user_msg_a.id
        ))

    async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as client:
        resp = await client.get(f"/api/chat/turn-reply?session_id=session_B&user_message_id={user_msg_a.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "gone"
        assert data["session_id"] == "session_B"
        assert "message_id" not in data


@pytest.mark.asyncio
async def test_lookup_status_ambiguous(lookup_db):
    """
    If multiple assistant replies link to the same user_message_id,
    the server must return ambiguous and NEVER arbitrarily pick one.
    """
    app = create_app()
    transport = ASGITransport(app=app)

    async with get_db(lookup_db) as conn:
        user_msg = await crud.add_message(conn, MessageCreate(
            session_id="sess_ambig", role="user", content_chinese="分叉测试"
        ))
        await crud.add_message(conn, MessageCreate(
            session_id="sess_ambig", role="assistant", content_chinese="回复1",
            reply_to_message_id=user_msg.id
        ))
        await crud.add_message(conn, MessageCreate(
            session_id="sess_ambig", role="assistant", content_chinese="回复2",
            reply_to_message_id=user_msg.id
        ))

    async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as client:
        resp = await client.get(f"/api/chat/turn-reply?session_id=sess_ambig&user_message_id={user_msg.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ambiguous"
        assert data["session_id"] == "sess_ambig"
        assert data["user_message_id"] == user_msg.id
        assert "message_id" not in data


@pytest.mark.asyncio
async def test_lookup_validation_and_errors(lookup_db):
    app = create_app()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as client:
        # Empty session_id
        r1 = await client.get("/api/chat/turn-reply?session_id=&user_message_id=1")
        assert r1.status_code == 422

        # Whitespace session_id
        r2 = await client.get("/api/chat/turn-reply?session_id=%20%20%20&user_message_id=1")
        assert r2.status_code == 422

        # Invalid user_message_id (<= 0)
        r3 = await client.get("/api/chat/turn-reply?session_id=valid&user_message_id=0")
        assert r3.status_code == 422

        r4 = await client.get("/api/chat/turn-reply?session_id=valid&user_message_id=-5")
        assert r4.status_code == 422


@pytest.mark.asyncio
async def test_lookup_unavailable_on_locked_db(lookup_db):
    """
    When an exclusive lock blocks read access,
    the lookup returns 'unavailable' fast without waiting 5 seconds.
    """
    app = create_app()
    transport = ASGITransport(app=app)

    sync_conn = sqlite3.connect(str(lookup_db), timeout=0.0)
    try:
        sync_conn.execute("PRAGMA locking_mode = EXCLUSIVE;")
        sync_conn.execute("BEGIN EXCLUSIVE;")

        async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as client:
            resp = await asyncio.wait_for(
                client.get("/api/chat/turn-reply?session_id=sess_lock&user_message_id=1"),
                timeout=2.0,  # Must complete far below default 5s busy timeout
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "unavailable"
            assert data["session_id"] == "sess_lock"
            assert data["user_message_id"] == 1
    finally:
        sync_conn.rollback()
        sync_conn.close()


@pytest.mark.asyncio
async def test_migration_idempotency_and_schema_version(lookup_db):
    """
    Schema version remains 7, migrations can run repeatedly without altering existing rows.
    """
    async with get_db(lookup_db) as conn:
        v1 = await run_schema_migrations(conn)
        assert v1 == CURRENT_SCHEMA_VERSION == 7
        # Re-run migration
        v2 = await run_schema_migrations(conn)
        assert v2 == CURRENT_SCHEMA_VERSION == 7

        # Ensure session exists for foreign key constraint
        await crud.get_or_create_session(conn, "legacy")
        # Check existing row compatibility with NULL reply_to_message_id
        await conn.execute("INSERT INTO messages (session_id, role, content_chinese) VALUES ('legacy', 'assistant', '旧数据');")
        rows = await (await conn.execute("SELECT reply_to_message_id FROM messages WHERE session_id='legacy';")).fetchall()
        assert rows[0][0] is None


@pytest.mark.asyncio
async def test_legacy_v0_migration_without_reply_to_column(tmp_path):
    """
    Gap 2 verification:
    A legacy database with user_version=0 having an old messages table without
    reply_to_message_id must upgrade successfully without crashing on premature
    index creation in v1.
    """
    v0_path = tmp_path / "legacy_v0.db"
    # Create legacy table manually without reply_to_message_id
    sync_conn = sqlite3.connect(str(v0_path))
    sync_conn.execute("""
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            channel TEXT NOT NULL DEFAULT 'web',
            user_id TEXT NOT NULL DEFAULT '',
            voice_profile_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    sync_conn.execute("""
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            role TEXT NOT NULL,
            content_chinese TEXT NOT NULL,
            content_japanese TEXT NOT NULL DEFAULT '',
            audio_url TEXT NOT NULL DEFAULT '',
            latency_ms INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    sync_conn.execute("PRAGMA user_version = 0;")
    sync_conn.commit()
    sync_conn.close()

    # Now run migrations through our migration runner
    async with get_db(v0_path) as conn:
        version = await run_schema_migrations(conn)
        assert version == CURRENT_SCHEMA_VERSION == 7

        # Confirm reply_to_message_id column and index were successfully added
        cols = {row["name"] for row in await (await conn.execute("PRAGMA table_info(messages);")).fetchall()}
        assert "reply_to_message_id" in cols
        indices = {row["name"] for row in await (await conn.execute("PRAGMA index_list(messages);")).fetchall()}
        assert "idx_messages_session_reply_to" in indices


@pytest.mark.asyncio
async def test_sequential_status_transition_ready_to_gone_on_clear(lookup_db):
    """
    Sequential state transition verification:
    With user and assistant messages committed, lookup initially returns 'ready'.
    After clear_session_messages completes, subsequent lookup returns 'gone'.
    (Note: This test asserts sequential state transition; cross-statement race
    prevention is guaranteed statically by the single-SQL LEFT JOIN rather than
    an automated concurrency barrier test.)
    """
    async with get_db(lookup_db) as conn:
        user_msg = await crud.add_message(conn, MessageCreate(
            session_id="sess_seq", role="user", content_chinese="顺序测试"
        ))
        asst_msg = await crud.add_message(conn, MessageCreate(
            session_id="sess_seq", role="assistant", content_chinese="回答",
            reply_to_message_id=user_msg.id
        ))

        # Before clear: ready
        lookup_before = await crud.lookup_turn_reply(conn, "sess_seq", user_msg.id)
        assert lookup_before["status"] == "ready"
        assert lookup_before["message_id"] == asst_msg.id

        # Clear session messages
        await crud.clear_session_messages(conn, "sess_seq")

        # After clear: gone
        lookup_after = await crud.lookup_turn_reply(conn, "sess_seq", user_msg.id)
        assert lookup_after["status"] == "gone"
