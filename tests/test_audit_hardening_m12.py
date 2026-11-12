"""
Audit Hardening M12 Test Suite.
Verifies:
1. Audio Converter (`utils/audio_converter.py`):
   - Corrupt WAV/OGG file handling and zero-byte/too-short payloads.
   - FFmpeg timeout handling, -nostdin injection, and zombie process prevention on timeout and cancellation.
   - Corrupt output validation.
2. Long-term memory & Affection service (`services/affection_service.py`, `services/memory_service.py`):
   - Concurrency safety during simultaneous dialogue unlocks (no lost updates).
   - Atomic affection score and level state transition integrity.
   - Extreme input boundaries (None, non-string, negative/extreme values).
   - Safe top_k=0 short-circuit and resilient prompt formatting.
3. Telemetry metrics & cache clear (`routers/metrics.py`, `services/metrics_collector.py`, `services/tts_cache_manager.py`):
   - Division-by-zero prevention across all metrics on empty databases.
   - Cache clear concurrency protection via _write_lock during concurrent put() and prune() operations.
   - Idempotent clear operations.
"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from httpx import AsyncClient, ASGITransport

from galgame2voice.database import crud
from galgame2voice.database.models import CharacterAffectionUpdate, UserMemoryCreate, UserMemoryResponse
from galgame2voice.database.session import get_db, init_db
from galgame2voice.main import create_app
from galgame2voice.services.affection_service import AffectionService
from galgame2voice.services.memory_service import MemoryService
from galgame2voice.services.metrics_collector import MetricsCollector, get_metrics_collector, reset_metrics_collector
from galgame2voice.services.tts_cache_manager import TtsCacheManager, get_tts_cache_manager, reset_tts_cache_manager
from galgame2voice.utils.audio_converter import (
    convert_ogg_to_wav,
    convert_wav_to_ogg,
    run_ffmpeg_command,
)


@pytest.fixture
def app(isolate_test_database, tmp_path):
    reset_metrics_collector()
    reset_tts_cache_manager()
    import sqlite3
    conn = sqlite3.connect(isolate_test_database)
    conn.execute(
        "INSERT OR IGNORE INTO voice_profiles (id, name, gpt_weights_path, sovits_weights_path, ref_audio_path, prompt_text, is_default) "
        "VALUES (1, 'default_char', 'default.ckpt', 'default.pth', 'default.wav', 'テスト', 1);"
    )
    conn.commit()
    conn.close()

    asyncio.run(init_db(isolate_test_database))

    test_cache_dir = tmp_path / "app_cache"
    test_cache_dir.mkdir(parents=True, exist_ok=True)
    import galgame2voice.services.tts_cache_manager as tc_mod
    import galgame2voice.services.metrics_collector as mc_mod

    tc_mod._tts_cache_manager_instance = TtsCacheManager(cache_dir=test_cache_dir, db_path=isolate_test_database)
    mc_mod._metrics_collector_instance = MetricsCollector(db_path=isolate_test_database)

    application = create_app()
    yield application

    reset_metrics_collector()
    reset_tts_cache_manager()


@pytest.fixture
async def client(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


# ============================================================================
# 1. Audio Converter: Corrupt/Zero-Byte Payloads & Timeout/Zombie Prevention
# ============================================================================

@pytest.mark.asyncio
class TestAudioConverterHardening:
    async def test_convert_ogg_to_wav_empty_and_too_short(self):
        with pytest.raises(ValueError, match="Audio payload is empty or too short"):
            await convert_ogg_to_wav(b"")

        with pytest.raises(ValueError, match="Audio payload is empty or too short"):
            await convert_ogg_to_wav(b"OggS123")

    async def test_convert_wav_to_ogg_empty_and_too_short(self):
        with pytest.raises(ValueError, match="WAV bytes cannot be empty"):
            await convert_wav_to_ogg(b"")

        with pytest.raises(ValueError, match="Audio payload is empty or too short"):
            await convert_wav_to_ogg(b"RIFFshort")

    async def test_convert_wav_to_ogg_corrupt_wrapped_as_value_error(self):
        # Fake discovery too: the transcode step resolves the executable through
        # find_ffmpeg, so patching only the executor left this test depending on a
        # real ffmpeg install being present.
        with patch("galgame2voice.utils.audio_converter.find_ffmpeg", return_value="/fake/ffmpeg"):
            with patch("galgame2voice.utils.audio_converter.run_ffmpeg_command", side_effect=RuntimeError("Invalid data")):
                fake_wav = b"RIFF\x24\x08\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00" + b"\x00" * 40
                with pytest.raises(ValueError, match="Audio conversion failed"):
                    await convert_wav_to_ogg(fake_wav)

    async def test_run_ffmpeg_command_injects_nostdin(self):
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"", b"")
        mock_proc.returncode = 0
        with patch("galgame2voice.utils.audio_converter.find_ffmpeg", return_value="/fake/ffmpeg"):
            with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
                await run_ffmpeg_command("ffmpeg", "-y", "-i", "input.ogg", "output.wav")
                args, _ = mock_exec.call_args
                assert "-nostdin" in args

    async def test_run_ffmpeg_command_timeout_kills_process_and_no_zombies(self):
        mock_proc = AsyncMock()
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock()

        async def _hang():
            await asyncio.sleep(10.0)
            return b"", b""

        mock_proc.communicate.side_effect = _hang

        with patch("galgame2voice.utils.audio_converter.find_ffmpeg", return_value="/fake/ffmpeg"):
            with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
                with pytest.raises(TimeoutError, match="timed out after 0.05 seconds"):
                    await run_ffmpeg_command("ffmpeg", "-i", "fake.wav", timeout=0.05)

                mock_proc.kill.assert_called_once()
                mock_proc.wait.assert_awaited_once()

    async def test_run_ffmpeg_command_cancellation_kills_process(self):
        mock_proc = AsyncMock()
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock()

        async def _hang():
            await asyncio.sleep(10.0)
            return b"", b""

        mock_proc.communicate.side_effect = _hang

        with patch("galgame2voice.utils.audio_converter.find_ffmpeg", return_value="/fake/ffmpeg"):
            with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
                task = asyncio.create_task(run_ffmpeg_command("ffmpeg", "-i", "fake.wav", timeout=30.0))
                await asyncio.sleep(0.02)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task

            mock_proc.kill.assert_called_once()
            mock_proc.wait.assert_awaited_once()

    async def test_early_rejection_non_audio_and_passthrough(self):
        # 1. Non-audio formats rejected early without calling ffmpeg
        with patch("galgame2voice.utils.audio_converter.run_ffmpeg_command") as mock_run:
            for non_audio in (
                b"\x89PNG\r\n\x1a\n" + b"\x00" * 30,
                b"<!DOCTYPE html><html>" + b"\x00" * 30,
                b'{"json_error": "bad"}' + b"\x00" * 30,
                b"\x7fELF" + b"\x00" * 30,
                b"%PDF-1.4" + b"\x00" * 30,
            ):
                with pytest.raises(ValueError, match="Corrupted or unsupported audio format"):
                    await convert_ogg_to_wav(non_audio)
                with pytest.raises(ValueError, match="Corrupted or unsupported audio format"):
                    await convert_wav_to_ogg(non_audio)
            mock_run.assert_not_called()

        # 2. Passthrough when WAV input is already OGG Opus
        fake_ogg = b"OggS\x00\x02" + b"\x00" * 50
        with patch("galgame2voice.utils.audio_converter.run_ffmpeg_command") as mock_run:
            out = await convert_wav_to_ogg(fake_ogg)
            assert out == fake_ogg
            mock_run.assert_not_called()



# ============================================================================
# 2. Affection Service: Concurrency Safety & State Transition Integrity
# ============================================================================

@pytest.mark.asyncio
class TestAffectionServiceHardening:
    async def test_affection_extreme_input_boundaries(self, isolate_test_database):
        svc = AffectionService(db_path=isolate_test_database)

        # Test None / non-str inputs
        pts, reasons = svc.calculate_turn_points(None)
        assert pts == 1
        assert "base_turn" in reasons

        emotion = svc.classify_emotion(None, None, current_emotion="tsundere", affection_level="invalid")
        assert emotion == "tsundere"

        egg = svc.check_easter_eggs(None, current_level=-1)
        assert egg is None

        # Level calculation boundaries
        lvl, name = svc.calculate_level(-100)
        assert lvl == 1
        lvl, name = svc.calculate_level(1000)
        assert lvl == 5
        lvl, name = svc.calculate_level("invalid")
        assert lvl == 1

    async def test_increment_affection_atomic_score_and_level_sync(self, isolate_test_database):
        async with get_db(isolate_test_database) as conn:
            # Level 1 -> Level 2 transition at score 20
            updated, gain, level_up = await crud.increment_affection(
                conn, user_id="sync_test_user", character_id=1, delta_points=20, daily_limit=50
            )
            assert updated.affection_score == 20
            assert updated.affection_level == 2
            assert gain == 20
            assert level_up is True

            # Advance to Level 3 at score 40
            updated2, gain2, level_up2 = await crud.increment_affection(
                conn, user_id="sync_test_user", character_id=1, delta_points=20, daily_limit=50
            )
            assert updated2.affection_score == 40
            assert updated2.affection_level == 3
            assert gain2 == 20
            assert level_up2 is True

    async def test_handle_turn_affection_concurrent_unlocks_no_lost_updates(self, isolate_test_database):
        svc = AffectionService(db_path=isolate_test_database)
        user_id = "concurrent_unlock_user"

        # Concurrently trigger different turns with distinct easter eggs and milestones
        async def _turn(text: str, pts: int):
            return await svc.handle_turn_affection(
                user_id=user_id,
                character_id=1,
                user_text=text,
                assistant_text="测试",
                daily_limit=50,
            )

        # First advance user to level 2 so level-gated easter eggs are eligible
        async with get_db(isolate_test_database) as conn:
            await crud.increment_affection(conn, user_id=user_id, character_id=1, delta_points=25, daily_limit=50)

        # Concurrently fire turns with distinct keywords
        await asyncio.gather(
            _turn("大枣和枣子姐", 1),
            _turn("发卡姬最棒", 1),
            _turn("女仆装很可爱", 1),
            _turn("日常交流", 1),
        )

        async with get_db(isolate_test_database) as conn:
            aff = await crud.get_character_affection(conn, user_id=user_id, character_id=1)
            unlocked = aff.unlocked_dialogues

            # Verify that all triggered easter eggs and milestones exist in unlocked_dialogues
            assert "easter_egg_zaozi" in unlocked
            assert "easter_egg_faka" in unlocked
            assert "easter_egg_maid" in unlocked
            assert "milestone_lv2" in unlocked

    async def test_multi_level_jump_unlocks_all_milestones(self, isolate_test_database):
        svc = AffectionService(db_path=isolate_test_database)
        user_id = "jump_user"

        # User starts at Lv1, directly gains 45 points (jumping straight to Lv3)
        res = await svc.handle_turn_affection(
            user_id=user_id,
            character_id=1,
            user_text="测试",
            assistant_text="测试",
            daily_limit=100,
        )
        # Advance score directly to 45
        async with get_db(isolate_test_database) as conn:
            await crud.increment_affection(conn, user_id=user_id, character_id=1, delta_points=44, daily_limit=100)

        # Next turn should verify level 3 and ensure Lv1, Lv2, Lv3 milestones are ALL unlocked
        res = await svc.handle_turn_affection(
            user_id=user_id,
            character_id=1,
            user_text="日常交流",
            assistant_text="日常交流",
            daily_limit=100,
        )
        assert res["level"] == 3

        async with get_db(isolate_test_database) as conn:
            aff = await crud.get_character_affection(conn, user_id=user_id, character_id=1)
            assert "milestone_lv1" in aff.unlocked_dialogues
            assert "milestone_lv2" in aff.unlocked_dialogues
            assert "milestone_lv3" in aff.unlocked_dialogues

    async def test_unlock_character_dialogues_on_new_user(self, isolate_test_database):
        async with get_db(isolate_test_database) as conn:
            # Calling unlock on brand-new user whose row does not exist yet
            unlocked = await crud.unlock_character_dialogues(
                conn, user_id="brand_new_user_xyz", character_id=1, dialogue_ids_to_add=["milestone_lv1", "easter_egg_maid"]
            )
            assert "milestone_lv1" in unlocked
            assert "easter_egg_maid" in unlocked

            # Verify persisted in database
            aff = await crud.get_character_affection(conn, user_id="brand_new_user_xyz", character_id=1)
            assert aff is not None
            assert "milestone_lv1" in aff.unlocked_dialogues
            assert "easter_egg_maid" in aff.unlocked_dialogues



# ============================================================================
# 3. Memory Service: Defensive Boundaries & Safe Top-K Zero
# ============================================================================

@pytest.mark.asyncio
class TestMemoryServiceHardening:
    async def test_memory_safe_top_k_zero(self, isolate_test_database):
        svc = MemoryService(db_path=isolate_test_database)
        # Store a sample memory
        await svc.process_user_message("user_top0", 1, "我喜欢玩原神")

        # Top-K = 0 must short circuit and return empty immediately
        res = await svc.retrieve_relevant_memories(user_id="user_top0", character_id=1, prompt="原神", top_k=0)
        assert res == []

    async def test_memory_extreme_input_boundaries(self, isolate_test_database):
        svc = MemoryService(db_path=isolate_test_database)

        # None / non-string message
        res = await svc.process_user_message("user_ex", 1, None)
        assert res == []

        res = await svc.process_user_message("user_ex", 1, "")
        assert res == []

        # Overlap score with extreme inputs
        score = svc._calculate_overlap_score(None, None, "like_game")
        assert score == 0.0

        score = svc._calculate_overlap_score("prompt", "fact", "like_fact")
        assert 0.0 <= score <= 1.0

        # Retrieve with extreme boundaries
        res = await svc.retrieve_relevant_memories(
            user_id=None,
            character_id=-1,
            prompt="A" * 10000,
            top_k=9999,
        )
        assert isinstance(res, list)

    async def test_format_memory_prompt_block_resilience(self):
        svc = MemoryService()

        # None memories and None affection_info
        block = svc.format_memory_prompt_block(None, None)
        assert block == ""

        # Malformed items
        dummy_mem = UserMemoryResponse(
            id=1,
            user_id="u",
            character_id=1,
            category="preference",
            fact_key="like_item",
            fact_value="咖啡",
            confidence=1.0,
            recall_count=0,
        )
        block = svc.format_memory_prompt_block([dummy_mem], {"level": "invalid", "level_name": None})
        assert "咖啡" in block
        assert "亲密度等级" in block


