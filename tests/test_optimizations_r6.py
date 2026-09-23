"""
Unit and Regression Tests for System-Wide Optimizations (R6).
Verifies:
1. Database schema initialization creates optimal compound indexes:
   - idx_sessions_updated_at
   - idx_messages_role_session
   - idx_token_usage_provider_tokens
2. SQLite query plans utilize indexes for updated_at sorting.
3. Frontend built static distribution contains the complete Galgame Character Stage.
"""

from pathlib import Path
import aiosqlite
import pytest

from galgame2voice.database.crud import init_schema_and_seeds


@pytest.mark.asyncio
async def test_database_indexes_created(tmp_path):
    """Verifies that all performance indexes are created on init_schema_and_seeds."""
    test_db = str(tmp_path / "test_indexes.db")
    async with aiosqlite.connect(test_db) as conn:
        await init_schema_and_seeds(conn)

        cursor = await conn.execute("SELECT name, tbl_name FROM sqlite_master WHERE type = 'index';")
        rows = await cursor.fetchall()
        indexes = dict(rows)

        assert "idx_sessions_updated_at" in indexes
        assert indexes["idx_sessions_updated_at"] == "sessions"

        assert "idx_messages_role_session" in indexes
        assert indexes["idx_messages_role_session"] == "messages"

        assert "idx_token_usage_provider_tokens" in indexes
        assert indexes["idx_token_usage_provider_tokens"] == "token_usage_metrics"

        assert "idx_messages_session_id_desc" in indexes
        assert indexes["idx_messages_session_id_desc"] == "messages"


@pytest.mark.asyncio
async def test_session_ordering_uses_index(tmp_path):
    """Verifies that EXPLAIN QUERY PLAN shows index usage on sessions(updated_at)."""
    test_db = str(tmp_path / "test_query_plan.db")
    async with aiosqlite.connect(test_db) as conn:
        await init_schema_and_seeds(conn)

        cursor = await conn.execute("EXPLAIN QUERY PLAN SELECT * FROM sessions ORDER BY updated_at DESC;")
        plan_rows = await cursor.fetchall()
        plan_str = " ".join(r["detail"] for r in plan_rows)
        # Should use idx_sessions_updated_at and NOT require temporary B-tree filesort
        assert "idx_sessions_updated_at" in plan_str
        assert "B-TREE FOR ORDER BY" not in plan_str.upper()


@pytest.mark.asyncio
async def test_recent_messages_ordering_uses_index(tmp_path):
    """Verifies that EXPLAIN QUERY PLAN shows index usage on messages(session_id, id DESC)."""
    from galgame2voice.database import crud
    test_db = str(tmp_path / "test_messages_query_plan.db")
    async with aiosqlite.connect(test_db) as conn:
        await init_schema_and_seeds(conn)

        cursor = await conn.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM (SELECT * FROM messages INDEXED BY idx_messages_session_id_desc WHERE session_id = ? ORDER BY id DESC LIMIT ?) ORDER BY id ASC;",
            ("sess_test", 10),
        )
        plan_rows = await cursor.fetchall()
        plan_str = " ".join(r["detail"] for r in plan_rows)
        assert "idx_messages_session_id_desc" in plan_str

        # Verify crud.get_recent_messages executes cleanly with the index
        recent = await crud.get_recent_messages(conn, "sess_test", 10)
        assert recent == []


def test_frontend_distribution_stage_markup():
    """Verifies that the compiled static index.html and frontend contain the character stage."""
    project_root = Path(__file__).resolve().parent.parent
    static_html = project_root / "galgame2voice" / "static" / "index.html"
    assert static_html.is_file(), "Static index.html must exist in backend distribution"

    content = static_html.read_text(encoding="utf-8")
    assert 'id="characterStage"' in content
    assert 'id="portraitToggleBtn"' in content
    assert 'id="portraitImgA"' in content
    assert 'id="portraitImgB"' in content
    assert 'id="stageDialogueCard"' in content
    assert 'id="stageCostumeBar"' in content

    # Check that assets directory exists and contains bundled JS/CSS
    assets_dir = project_root / "galgame2voice" / "static" / "assets"
    assert assets_dir.is_dir(), "Static assets directory must exist"
    asset_files = [f.name for f in assets_dir.iterdir()]
    assert any(f.endswith(".js") for f in asset_files), "Bundled JS asset must exist"
    assert any(f.endswith(".css") for f in asset_files), "Bundled CSS asset must exist"
