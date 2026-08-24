"""
Tests for SQLite Config Store, CRUD operations, Secret Masking, and Security Controls.
Covers Tier 1 (Feature Coverage), Tier 2 (Boundary, Injection, Concurrency, and Secret Masking),
and Database CRUD operations on models & session.
"""

import os
import asyncio
import json
import sqlite3
import tempfile
import pytest
import aiosqlite

from galgame2voice.database.session import get_db, init_db
from galgame2voice.database.crud import (
    mask_api_key, is_masked_key,
    get_settings, get_settings_raw, update_settings, verify_console_token,
    list_providers, get_provider, get_provider_raw, get_active_provider, get_active_provider_raw,
    create_provider, update_provider, set_active_provider, delete_provider,
    list_voice_profiles, get_voice_profile, get_active_voice_profile,
    create_voice_profile, update_voice_profile, set_active_voice_profile, delete_voice_profile,
    get_or_create_session, get_session, list_sessions, delete_session, clear_session_messages,
    add_message, get_recent_messages, count_session_messages
)
from galgame2voice.database.models import (
    SettingsUpdate, ProviderCreate, ProviderUpdate,
    VoiceProfileCreate, VoiceProfileUpdate,
    SessionCreate, SessionUpdate,
    MessageCreate
)


def mask_secret(secret: str) -> str:
    """Helper to mask API keys and bot tokens (e.g. sk-****cdef)."""
    return mask_api_key(secret)


# ============================================================================
# Tier 1: Config Store & CRUD Primary Tests (Table-level & Schema)
# ============================================================================

class TestConfigStoreTier1:
    """Tier 1: Basic table initialization, CRUD for settings, voice profiles, and providers."""

    @pytest.mark.asyncio
    async def test_schema_initialization(self, temp_db_path):
        """Verifies database tables exist after schema init."""
        async with aiosqlite.connect(temp_db_path) as db:
            async with db.execute("SELECT name FROM sqlite_master WHERE type='table';") as cursor:
                rows = await cursor.fetchall()
                table_names = {row[0] for row in rows}
                assert "settings" in table_names
                assert "voice_profiles" in table_names
                assert "providers" in table_names
                assert "session_messages" in table_names

    @pytest.mark.asyncio
    async def test_settings_set_and_get(self, temp_db_path):
        """Verifies setting key-value pairs and retrieving them."""
        async with aiosqlite.connect(temp_db_path) as db:
            await db.execute("INSERT OR REPLACE INTO settings (key, value, description) VALUES (?, ?, ?)",
                             ("gpt_sovits_url", "http://127.0.0.1:9880", "GPT-SoVITS API URL"))
            await db.commit()

            async with db.execute("SELECT value, description FROM settings WHERE key = ?", ("gpt_sovits_url",)) as cursor:
                row = await cursor.fetchone()
                assert row is not None
                assert row[0] == "http://127.0.0.1:9880"
                assert row[1] == "GPT-SoVITS API URL"

    @pytest.mark.asyncio
    async def test_settings_update_existing_key(self, temp_db_path):
        """Verifies updating an existing configuration key overwrites its value."""
        async with aiosqlite.connect(temp_db_path) as db:
            await db.execute("INSERT INTO settings (key, value) VALUES (?, ?)", ("audio_speed", "1.0"))
            await db.commit()

            await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", ("audio_speed", "1.2"))
            await db.commit()

            async with db.execute("SELECT value FROM settings WHERE key = ?", ("audio_speed",)) as cursor:
                row = await cursor.fetchone()
                assert row[0] == "1.2"

    @pytest.mark.asyncio
    async def test_voice_profile_crud(self, temp_db_path, sample_voice_profile):
        """Verifies full lifecycle of voice profiles: Create, Read, Update, Delete."""
        async with aiosqlite.connect(temp_db_path) as db:
            # Create
            cursor = await db.execute("""
                INSERT INTO voice_profiles (name, gpt_weights_path, sovits_weights_path, refer_audio_path, refer_text, refer_language, is_default)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                sample_voice_profile["name"],
                sample_voice_profile["gpt_weights_path"],
                sample_voice_profile["sovits_weights_path"],
                sample_voice_profile["refer_audio_path"],
                sample_voice_profile["refer_text"],
                sample_voice_profile["refer_language"],
                sample_voice_profile["is_default"]
            ))
            await db.commit()
            profile_id = cursor.lastrowid
            assert profile_id > 0

            # Read
            async with db.execute("SELECT name, refer_text, is_default FROM voice_profiles WHERE id = ?", (profile_id,)) as cursor:
                row = await cursor.fetchone()
                assert row[0] == "Arona"
                assert row[1] == "先生、今日もよろしくお願いしますね！"
                assert row[2] == 1

            # Update
            await db.execute("UPDATE voice_profiles SET refer_text = ? WHERE id = ?", ("先生、お疲れ様です！", profile_id))
            await db.commit()

            async with db.execute("SELECT refer_text FROM voice_profiles WHERE id = ?", (profile_id,)) as cursor:
                row = await cursor.fetchone()
                assert row[0] == "先生、お疲れ様です！"

            # Delete
            await db.execute("DELETE FROM voice_profiles WHERE id = ?", (profile_id,))
            await db.commit()

            async with db.execute("SELECT id FROM voice_profiles WHERE id = ?", (profile_id,)) as cursor:
                row = await cursor.fetchone()
                assert row is None

    @pytest.mark.asyncio
    async def test_provider_crud(self, temp_db_path, sample_provider_config):
        """Verifies creating, querying, and updating provider configs."""
        async with aiosqlite.connect(temp_db_path) as db:
            await db.execute("""
                INSERT INTO providers (id, provider_type, api_key, base_url, model, is_active, extra_config)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                sample_provider_config["id"],
                sample_provider_config["provider_type"],
                sample_provider_config["api_key"],
                sample_provider_config["base_url"],
                sample_provider_config["model"],
                sample_provider_config["is_active"],
                sample_provider_config["extra_config"]
            ))
            await db.commit()

            async with db.execute("SELECT provider_type, model, is_active FROM providers WHERE id = ?", (sample_provider_config["id"],)) as cursor:
                row = await cursor.fetchone()
                assert row is not None
                assert row[0] == "openai"
                assert row[1] == "gpt-4o"
                assert row[2] == 1

    @pytest.mark.asyncio
    async def test_session_messages_append_and_query(self, temp_db_path):
        """Verifies appending multi-turn conversation messages and fetching them in order."""
        async with aiosqlite.connect(temp_db_path) as db:
            session_id = "test-session-101"
            await db.execute("""
                INSERT INTO session_messages (session_id, role, content_chinese, content_japanese, raw_content)
                VALUES (?, ?, ?, ?, ?)
            """, (session_id, "user", "你好", "こんにちは", "你好"))
            await db.execute("""
                INSERT INTO session_messages (session_id, role, content_chinese, content_japanese, raw_content)
                VALUES (?, ?, ?, ?, ?)
            """, (session_id, "assistant", "你好！很高兴见到你。", "こんにちは！お会いできて嬉しいです。", '{"chinese":"你好！很高兴见到你。","japanese":"こんにちは！お会いできて嬉しいです。"}'))
            await db.commit()

            async with db.execute("SELECT role, content_chinese, content_japanese FROM session_messages WHERE session_id = ? ORDER BY id ASC", (session_id,)) as cursor:
                rows = await cursor.fetchall()
                assert len(rows) == 2
                assert rows[0][0] == "user"
                assert rows[1][0] == "assistant"
                assert rows[1][1] == "你好！很高兴见到你。"
                assert rows[1][2] == "こんにちは！お会いできて嬉しいです。"


# ============================================================================
# Tier 2: Boundary, Injection, Security, and Concurrency Tests
# ============================================================================

class TestConfigStoreTier2:
    """Tier 2: Edge cases, Secret Masking, SQL Injection, and Concurrency."""

    @pytest.mark.asyncio
    async def test_secret_masking_integrity(self, temp_db_path):
        """Verifies API keys are masked when returned for display, but remain unmasked in storage."""
        raw_key = "sk-proj-998877665544332211aabbccddeeff"
        async with aiosqlite.connect(temp_db_path) as db:
            await db.execute("INSERT INTO providers (id, provider_type, api_key, model) VALUES (?, ?, ?, ?)",
                             ("openai_main", "openai", raw_key, "gpt-4o"))
            await db.commit()

            # Internal backend query (unmasked)
            async with db.execute("SELECT api_key FROM providers WHERE id = ?", ("openai_main",)) as cursor:
                row = await cursor.fetchone()
                assert row[0] == raw_key

            # Web Console display query (masked)
            masked = mask_secret(row[0])
            assert masked == "sk-****eeff"
            assert raw_key not in masked
            assert "****" in masked

    @pytest.mark.asyncio
    async def test_mask_secret_edge_cases(self):
        """Verifies mask_secret handles empty, short, and long keys safely."""
        assert mask_secret("") == ""
        assert mask_secret("short") == "********"
        assert mask_secret("12345678") == "********"
        assert mask_secret("sk-123456789") == "sk-****6789"

    @pytest.mark.asyncio
    async def test_sql_injection_resistance(self, temp_db_path):
        """Verifies parameterized queries prevent SQL injection payloads in key, value, and profile names."""
        malicious_key = "'; DROP TABLE settings; --"
        malicious_value = "' OR '1'='1"
        malicious_profile_name = "Arona'); DELETE FROM voice_profiles; --"

        async with aiosqlite.connect(temp_db_path) as db:
            # Parameterized insert into settings
            await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (malicious_key, malicious_value))
            await db.commit()

            # Verify settings table was NOT dropped
            async with db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='settings';") as cursor:
                row = await cursor.fetchone()
                assert row is not None

            # Verify the exact malicious key was stored literally
            async with db.execute("SELECT value FROM settings WHERE key = ?", (malicious_key,)) as cursor:
                row = await cursor.fetchone()
                assert row[0] == malicious_value

            # Parameterized insert into voice_profiles
            await db.execute("""
                INSERT INTO voice_profiles (name, gpt_weights_path, sovits_weights_path, refer_audio_path, refer_text)
                VALUES (?, ?, ?, ?, ?)
            """, (malicious_profile_name, "w.ckpt", "w.pth", "r.wav", "text"))
            await db.commit()

            # Verify profile exists and table is intact
            async with db.execute("SELECT name FROM voice_profiles WHERE name = ?", (malicious_profile_name,)) as cursor:
                row = await cursor.fetchone()
                assert row[0] == malicious_profile_name

    @pytest.mark.asyncio
    async def test_voice_profile_unique_name_constraint(self, temp_db_path, sample_voice_profile):
        """Verifies inserting a voice profile with duplicate name raises sqlite3.IntegrityError."""
        async with aiosqlite.connect(temp_db_path) as db:
            await db.execute("""
                INSERT INTO voice_profiles (name, gpt_weights_path, sovits_weights_path, refer_audio_path, refer_text)
                VALUES (?, ?, ?, ?, ?)
            """, (sample_voice_profile["name"], "w.ckpt", "w.pth", "r.wav", "text"))
            await db.commit()

            with pytest.raises(sqlite3.IntegrityError):
                await db.execute("""
                    INSERT INTO voice_profiles (name, gpt_weights_path, sovits_weights_path, refer_audio_path, refer_text)
                    VALUES (?, ?, ?, ?, ?)
                """, (sample_voice_profile["name"], "w2.ckpt", "w2.pth", "r2.wav", "text2"))
                await db.commit()

    @pytest.mark.asyncio
    async def test_concurrent_writes_wal_mode(self, temp_db_path):
        """Verifies database handles multiple concurrent async write operations without locking failure."""
        async def insert_worker(worker_id: int):
            async with aiosqlite.connect(temp_db_path, timeout=10.0) as db:
                for i in range(10):
                    await db.execute(
                        "INSERT INTO session_messages (session_id, role, content_chinese) VALUES (?, ?, ?)",
                        (f"session-worker-{worker_id}", "user", f"message-{i}")
                    )
                    await db.commit()

        # Run 5 concurrent workers
        tasks = [insert_worker(i) for i in range(5)]
        await asyncio.gather(*tasks)

        async with aiosqlite.connect(temp_db_path) as db:
            async with db.execute("SELECT COUNT(*) FROM session_messages") as cursor:
                row = await cursor.fetchone()
                assert row[0] == 50

    @pytest.mark.asyncio
    async def test_special_characters_and_emoji_support(self, temp_db_path):
        """Verifies UTF-8 multilingual characters, Japanese kanji/kana, emojis, and control chars."""
        special_text = "日本語テスト：『こんにちは！』🤖✨🌸\n\t\\\"'特殊文字"
        async with aiosqlite.connect(temp_db_path) as db:
            await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", ("character_prompt", special_text))
            await db.commit()

            async with db.execute("SELECT value FROM settings WHERE key = ?", ("character_prompt",)) as cursor:
                row = await cursor.fetchone()
                assert row[0] == special_text

    @pytest.mark.asyncio
    async def test_json_extra_config_parsing(self, temp_db_path):
        """Verifies extra_config JSON serialization and deserialization."""
        config_data = {
            "top_k": 15,
            "top_p": 0.95,
            "temperature": 0.85,
            "custom_headers": {"X-Custom-Auth": "secret-token"}
        }
        async with aiosqlite.connect(temp_db_path) as db:
            await db.execute("INSERT INTO providers (id, provider_type, api_key, model, extra_config) VALUES (?, ?, ?, ?, ?)",
                             ("custom_p1", "custom", "key", "model-x", json.dumps(config_data)))
            await db.commit()

            async with db.execute("SELECT extra_config FROM providers WHERE id = ?", ("custom_p1",)) as cursor:
                row = await cursor.fetchone()
                parsed = json.loads(row[0])
                assert parsed["top_k"] == 15
                assert parsed["custom_headers"]["X-Custom-Auth"] == "secret-token"

    @pytest.mark.asyncio
    async def test_session_history_deletion_and_isolation(self, temp_db_path):
        """Verifies clearing messages for one session does not affect other sessions."""
        async with aiosqlite.connect(temp_db_path) as db:
            await db.execute("INSERT INTO session_messages (session_id, role, content_chinese) VALUES (?, ?, ?)",
                             ("session-A", "user", "msg A1"))
            await db.execute("INSERT INTO session_messages (session_id, role, content_chinese) VALUES (?, ?, ?)",
                             ("session-B", "user", "msg B1"))
            await db.commit()

            # Delete session-A only
            await db.execute("DELETE FROM session_messages WHERE session_id = ?", ("session-A",))
            await db.commit()

            # Check session-A is empty
            async with db.execute("SELECT COUNT(*) FROM session_messages WHERE session_id = ?", ("session-A",)) as cursor:
                row = await cursor.fetchone()
                assert row[0] == 0

            # Check session-B is intact
            async with db.execute("SELECT COUNT(*) FROM session_messages WHERE session_id = ?", ("session-B",)) as cursor:
                row = await cursor.fetchone()
                assert row[0] == 1

    @pytest.mark.parametrize("key_name,default_val", [
        ("gpt_sovits_url", "http://127.0.0.1:9880"),
        ("telegram_bot_token", "123456789:ABCDEF"),
        ("audio_speed", "1.0"),
        ("top_k", "15"),
        ("temperature", "0.8"),
        ("chat_history_limit", "20"),
        ("streaming_mode", "true"),
    ])
    @pytest.mark.asyncio
    async def test_all_setting_keys_persistence(self, temp_db_path, key_name, default_val):
        async with aiosqlite.connect(temp_db_path) as db:
            await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key_name, default_val))
            await db.commit()
            async with db.execute("SELECT value FROM settings WHERE key = ?", (key_name,)) as cursor:
                row = await cursor.fetchone()
                assert row[0] == default_val

    @pytest.mark.parametrize("channel", ["web", "telegram", "api", "cli"])
    @pytest.mark.asyncio
    async def test_session_channel_tagging(self, temp_db_path, channel):
        async with aiosqlite.connect(temp_db_path) as db:
            sess_id = f"{channel}-session-1"
            await db.execute("INSERT INTO session_messages (session_id, role, content_chinese) VALUES (?, ?, ?)",
                             (sess_id, "user", f"Hello via {channel}"))
            await db.commit()
            async with db.execute("SELECT COUNT(*) FROM session_messages WHERE session_id = ?", (sess_id,)) as cursor:
                row = await cursor.fetchone()
                assert row[0] == 1



# ============================================================================
# Tier 3: High-Level Async CRUD & Seed Persistence Verification
# ============================================================================

class TestConfigStoreCrud:
    """Tests for galgame2voice.database.crud functions and seed initialization."""

    @pytest.mark.asyncio
    async def test_init_db_and_seeds(self):
        """Verifies init_db initializes the full relational schema with default seeds."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_file = f.name

        try:
            await init_db(db_file)
            async with get_db(db_file) as conn:
                # Check Settings
                settings = await get_settings(conn)
                assert settings.active_provider_id in ("gemini", "deepseek", "openai")
                assert settings.active_voice_profile_id == 1
                assert len(settings.console_token) > 10

                # Check Voice Profiles
                profiles = await list_voice_profiles(conn)
                assert len(profiles) >= 1
                assert "四季夏目" in profiles[0].name
                assert profiles[0].is_default is True

                # Check Providers
                providers = await list_providers(conn)
                assert len(providers) == 8
                active_p = await get_active_provider(conn)
                assert active_p is not None
                assert active_p.id in ("gemini", "deepseek", "openai")
        finally:
            if os.path.exists(db_file):
                os.remove(db_file)

    @pytest.mark.asyncio
    async def test_settings_update_with_key_retention(self):
        """Verifies update_settings retains existing secrets when masked string is passed."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_file = f.name

        try:
            await init_db(db_file)
            async with get_db(db_file) as conn:
                # Set initial secret
                await update_settings(conn, SettingsUpdate(telegram_bot_token="123456789:ABCdefgh-secrettoken1234567890"))
                raw = await get_settings_raw(conn)
                assert raw.telegram_bot_token == "123456789:ABCdefgh-secrettoken1234567890"

                # Update speed_factor but submit masked token placeholder
                masked_resp = await get_settings(conn, mask=True)
                assert is_masked_key(masked_resp.telegram_bot_token)

                await update_settings(conn, SettingsUpdate(
                    speed_factor=1.25,
                    telegram_bot_token=masked_resp.telegram_bot_token
                ))

                # Verify secret was not overwritten with the mask
                raw2 = await get_settings_raw(conn)
                assert raw2.speed_factor == 1.25
                assert raw2.telegram_bot_token == "123456789:ABCdefgh-secrettoken1234567890"

                # Verify console token
                assert await verify_console_token(conn, raw2.console_token) is True
                assert await verify_console_token(conn, "wrong-token") is False
        finally:
            if os.path.exists(db_file):
                os.remove(db_file)

    @pytest.mark.asyncio
    async def test_provider_crud_operations(self):
        """Verifies Provider CRUD, active provider switching, and masking retention."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_file = f.name

        try:
            await init_db(db_file)
            async with get_db(db_file) as conn:
                # Create custom provider
                new_p = await create_provider(conn, ProviderCreate(
                    id="my_llm",
                    name="My Custom LLM",
                    api_base_url="https://api.myllm.com/v1",
                    api_key="sk-secret1234567890abcdef",
                    chat_model="my-model-v1",
                    is_active=False
                ))
                assert new_p.id == "my_llm"
                assert new_p.api_key == "sk-****cdef"

                # Set active
                await set_active_provider(conn, "my_llm")
                active = await get_active_provider(conn)
                assert active.id == "my_llm"

                # Update model without changing masked key
                await update_provider(conn, "my_llm", ProviderUpdate(
                    chat_model="my-model-v2",
                    api_key="sk-****cdef"
                ))
                raw_p = await get_provider_raw(conn, "my_llm")
                assert raw_p.chat_model == "my-model-v2"
                assert raw_p.api_key == "sk-secret1234567890abcdef"

                # Delete
                assert await delete_provider(conn, "my_llm") is True
                assert await get_provider(conn, "my_llm") is None
        finally:
            if os.path.exists(db_file):
                os.remove(db_file)

    @pytest.mark.asyncio
    async def test_voice_profile_crud_operations(self):
        """Verifies Voice Profile CRUD and active profile switching."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_file = f.name

        try:
            await init_db(db_file)
            async with get_db(db_file) as conn:
                # Create
                created = await create_voice_profile(conn, VoiceProfileCreate(
                    name="Test Girl",
                    description="A test character",
                    gpt_weights_path="weights/test.ckpt",
                    sovits_weights_path="weights/test.pth",
                    ref_audio_path="ref/test.wav",
                    prompt_text="テストテキストです",
                    prompt_lang="ja",
                    text_lang="ja",
                    system_prompt="You are Test Girl"
                ))
                assert created.id > 1
                assert created.name == "Test Girl"

                # Switch active
                assert await set_active_voice_profile(conn, created.id) is True
                active = await get_active_voice_profile(conn)
                assert active.id == created.id

                # Update
                updated = await update_voice_profile(conn, created.id, VoiceProfileUpdate(description="Updated desc"))
                assert updated.description == "Updated desc"

                # Delete
                assert await delete_voice_profile(conn, created.id) is True
                assert await get_voice_profile(conn, created.id) is None
        finally:
            if os.path.exists(db_file):
                os.remove(db_file)

    @pytest.mark.asyncio
    async def test_session_and_message_history(self):
        """Verifies session auto-creation, message appending, sliding window query, and cleanup."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_file = f.name

        try:
            await init_db(db_file)
            async with get_db(db_file) as conn:
                session_id = "web-sess-001"
                sess = await get_or_create_session(conn, session_id, channel="web")
                assert sess.id == session_id

                # Append 15 messages
                for i in range(15):
                    await add_message(conn, MessageCreate(
                        session_id=session_id,
                        role="user" if i % 2 == 0 else "assistant",
                        content_chinese=f"对话_{i}",
                        content_japanese=f"対話_{i}",
                        latency_ms=100 + i
                    ))

                assert await count_session_messages(conn, session_id) == 15

                # Query recent 10 messages (sliding window)
                recent = await get_recent_messages(conn, session_id, limit=10)
                assert len(recent) == 10
                # Should be chronological order (5 to 14)
                assert recent[0].content_chinese == "对话_5"
                assert recent[-1].content_chinese == "对话_14"

                # Clear messages
                assert await clear_session_messages(conn, session_id) is True
                assert await count_session_messages(conn, session_id) == 0

                # Delete session
                assert await delete_session(conn, session_id) is True
                assert await get_session(conn, session_id) is None
        finally:
            if os.path.exists(db_file):
                os.remove(db_file)
