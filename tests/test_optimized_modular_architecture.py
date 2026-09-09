"""
Unit & Integration Tests for Galgame2Voice Architecture Optimizations:
1. Decoupled emotion_classifier and streaming_parser modules
2. Backward compatibility of all re-exports from chat_service and services package
3. Audio concatenation buffer optimization and wav parameter preservation
4. Database SQLite pragmas and index coverage verification
5. Low-latency non-blocking queue puts with cancellation safety
"""

import asyncio
import io
import struct
import wave
from pathlib import Path
import pytest
import aiosqlite

from galgame2voice.services.emotion_classifier import (
    EMOTION_KEYWORDS,
    VALID_EMOTIONS,
    EMOTION_NAME_MAP,
    classify_emotion,
)
from galgame2voice.services.streaming_parser import StreamingBilingualParser
from galgame2voice.services.chat_service import (
    ChatService,
    StreamingBilingualParser as ReExportedParser,
    classify_emotion as re_exported_classify,
    EMOTION_KEYWORDS as re_exported_keywords,
    VALID_EMOTIONS as re_exported_valid,
    EMOTION_NAME_MAP as re_exported_map,
)
import galgame2voice.services as services_pkg
from galgame2voice.database.session import configure_connection, get_db, init_db
from galgame2voice.database import crud
from galgame2voice.database.models import MessageCreate


def _generate_test_wav(duration_s: float = 0.1, sample_rate: int = 16000) -> bytes:
    """Helper creating a minimal valid PCM WAV in-memory."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        num_samples = int(sample_rate * duration_s)
        frames = struct.pack(f"<{num_samples}h", *[0] * num_samples)
        w.writeframes(frames)
    return buf.getvalue()


class TestDecoupledEmotionClassifier:
    """Tests for galgame2voice.services.emotion_classifier."""

    def test_direct_import_and_taxonomy_completeness(self):
        assert isinstance(EMOTION_KEYWORDS, dict)
        assert len(EMOTION_KEYWORDS) >= 6
        assert VALID_EMOTIONS == {"gentle", "shy", "happy", "tsundere", "cool", "sad"}
        assert EMOTION_NAME_MAP["傲娇"] == "tsundere"
        assert EMOTION_NAME_MAP["害羞"] == "shy"

    def test_explicit_emotion_precedence(self):
        # Explicit emotion overrides keywords
        res = classify_emotion(chinese="我好难过……", explicit_emotion="happy")
        assert res == "happy"
        # Chinese name mapped
        res_cn = classify_emotion(chinese="随你便", explicit_emotion="傲娇")
        assert res_cn == "tsundere"
        # Invalid explicit falls through to keywords
        res_invalid = classify_emotion(chinese="笨蛋，才没有脸红呢///", explicit_emotion="unknown_emo")
        assert res_invalid in ("tsundere", "shy")

    def test_keyword_classification_rules(self):
        assert classify_emotion(chinese="笨蛋！才不是特意给你做的！别误会！") == "tsundere"
        assert classify_emotion(chinese="……脸红///，别看了") == "shy"
        assert classify_emotion(chinese="太好了！今天真的超级开心！") == "happy"
        assert classify_emotion(chinese="……无聊，随你便。") == "cool"
        assert classify_emotion(chinese="对不起……真的很伤心……呜呜") == "sad"
        assert classify_emotion(chinese="摸摸头，乖哦，请放心吧。") == "gentle"

    def test_fallback_behavior(self):
        assert classify_emotion("", "") == "gentle"
        assert classify_emotion("明天温度二十度左右。", "明日は20度くらいです。") == "gentle"


class TestDecoupledStreamingParser:
    """Tests for galgame2voice.services.streaming_parser."""

    def test_incremental_tokens_and_delimiters(self):
        parser = StreamingBilingualParser()
        raw_chunks = [
            "```json\n",
            '{"chinese": "你好呀',
            '，指挥官！",',
            ' "japanese": "こんにちは、',
            '指揮官！"}\n```',
        ]
        zh_deltas = []
        sentences_collected = []
        for ch in raw_chunks:
            d_zh, s_ja = parser.feed_chunk(ch)
            if d_zh:
                zh_deltas.append(d_zh)
            sentences_collected.extend(s_ja)

        full_zh, full_ja, rem = parser.finalize()
        sentences_collected.extend(rem)

        assert "".join(zh_deltas) == "你好呀，指挥官！"
        assert full_zh == "你好呀，指挥官！"
        assert "指揮官" in full_ja
        assert len(sentences_collected) >= 1

    def test_dynamic_tts_options_extraction_and_clamping(self):
        parser = StreamingBilingualParser()
        chunk = '{"tts": {"speed": 2.5, "temperature": 0.05, "emotion": "tsundere"}, "chinese": "哼！"}'
        parser.feed_chunk(chunk)

        opts = parser.get_dynamic_tts_options(
            base_options={"speed": 1.0, "temperature": 1.0, "ai_adaptive_voice": True},
            adaptive_enabled=True,
        )
        # Clamped to max allowed
        assert opts["speed"] <= 1.5
        assert opts["temperature"] >= 0.3
        assert opts["emotion"] == "tsundere"
        assert opts["ai_adaptive_voice"] is True

        # When adaptive disabled, original base options preserved
        opts_disabled = parser.get_dynamic_tts_options(
            base_options={"speed": 1.0, "temperature": 1.0, "ai_adaptive_voice": True},
            adaptive_enabled=False,
        )
        assert opts_disabled["speed"] == 1.0
        assert opts_disabled["temperature"] == 1.0
        assert opts_disabled["ai_adaptive_voice"] is False


class TestBackwardCompatibilityReExports:
    """Verifies that imports from chat_service and galgame2voice.services remain 100% identical."""

    def test_chat_service_re_exports(self):
        assert ReExportedParser is StreamingBilingualParser
        assert re_exported_classify is classify_emotion
        assert re_exported_keywords is EMOTION_KEYWORDS
        assert re_exported_valid is VALID_EMOTIONS
        assert re_exported_map is EMOTION_NAME_MAP

    def test_services_package_exports(self):
        assert services_pkg.StreamingBilingualParser is StreamingBilingualParser
        assert services_pkg.classify_emotion is classify_emotion
        assert services_pkg.EMOTION_KEYWORDS is EMOTION_KEYWORDS
        assert services_pkg.VALID_EMOTIONS is VALID_EMOTIONS
        assert services_pkg.EMOTION_NAME_MAP is EMOTION_NAME_MAP


class TestAudioConcatenationOptimization:
    """Tests in-memory buffered WAV concatenation in ChatService."""

    def test_concat_wav_files_multi_chunk(self, tmp_path):
        service = ChatService(db_path=tmp_path / "test.db")
        p1 = tmp_path / "c1.wav"
        p2 = tmp_path / "c2.wav"
        out = tmp_path / "sub" / "master.wav"

        b1 = _generate_test_wav(0.1, 16000)
        b2 = _generate_test_wav(0.2, 16000)
        p1.write_bytes(b1)
        p2.write_bytes(b2)

        ok = service._concat_wav_files([str(p1), str(p2)], out)
        assert ok is True
        assert out.exists()

        with wave.open(str(out), "rb") as w:
            assert w.getframerate() == 16000
            assert w.getnchannels() == 1
            # Total duration should be ~0.3s (4800 frames)
            assert w.getnframes() == 4800

    def test_concat_wav_files_empty_and_unreadable_fallback(self, tmp_path):
        service = ChatService(db_path=tmp_path / "test.db")
        out = tmp_path / "master_empty.wav"

        assert service._concat_wav_files([], out) is False
        assert service._concat_wav_files(["", None], out) is False

        # One corrupt and one valid
        corrupt = tmp_path / "corrupt.wav"
        corrupt.write_bytes(b"not_a_wav")
        valid = tmp_path / "valid.wav"
        valid.write_bytes(_generate_test_wav(0.05, 16000))

        ok = service._concat_wav_files([str(corrupt), str(valid)], out)
        assert ok is True
        with wave.open(str(out), "rb") as w:
            assert w.getnframes() == int(16000 * 0.05)


class TestDatabasePragmasAndIndexCoverage:
    """Verifies SQLite performance tuning pragmas and index existence."""

    @pytest.mark.asyncio
    async def test_performance_pragmas_configured(self, tmp_path):
        db_p = tmp_path / "pragma_test.db"
        async with get_db(db_p) as conn:
            mode = (await (await conn.execute("PRAGMA journal_mode;")).fetchone())[0]
            assert str(mode).lower() == "wal"

            timeout = (await (await conn.execute("PRAGMA busy_timeout;")).fetchone())[0]
            assert timeout == 5000

            sync = (await (await conn.execute("PRAGMA synchronous;")).fetchone())[0]
            # synchronous = NORMAL is 1 in SQLite
            assert sync == 1

            cache = (await (await conn.execute("PRAGMA cache_size;")).fetchone())[0]
            assert cache == -64000

            temp = (await (await conn.execute("PRAGMA temp_store;")).fetchone())[0]
            # temp_store = MEMORY is 2 in SQLite
            assert temp == 2

    @pytest.mark.asyncio
    async def test_database_indices_coverage(self, tmp_path):
        db_p = tmp_path / "index_coverage.db"
        await init_db(db_p)

        async with get_db(db_p) as conn:
            cursor = await conn.execute("SELECT name FROM sqlite_master WHERE type='index';")
            rows = await cursor.fetchall()
            index_names = {r[0] for r in rows}

            expected_indices = {
                "idx_messages_session_id",
                "idx_messages_created_at",
                "idx_messages_session_created",
                "idx_sessions_channel",
                "idx_sessions_updated_at",
                "idx_sessions_user_id",
                "idx_sessions_voice_profile",
                "idx_voice_profiles_is_default",
                "idx_user_memories_user_cat",
                "idx_user_memories_char_key",
                "idx_affection_user_char",
                "idx_tts_cache_last_accessed",
                "idx_tts_cache_clean_text",
            }
            for exp in expected_indices:
                assert exp in index_names, f"Expected index {exp} not found in DB schema!"


class TestQueueLowLatencyNonBlocking:
    """Verifies ultra-low latency non-blocking queue insertion."""

    @pytest.mark.asyncio
    async def test_queue_fast_path_and_cancel_responsiveness(self):
        q = asyncio.Queue(maxsize=10)
        cancel_evt = asyncio.Event()

        # Implementation matches _put_with_cancel in chat_service
        async def _put(item: str) -> bool:
            if cancel_evt and cancel_evt.is_set():
                return False
            try:
                q.put_nowait(item)
                return True
            except asyncio.QueueFull:
                pass
            while True:
                if cancel_evt and cancel_evt.is_set():
                    return False
                try:
                    await asyncio.wait_for(q.put(item), timeout=0.05)
                    return True
                except asyncio.TimeoutError:
                    continue

        # Fast path put_nowait executes synchronously
        ok = await _put("item_1")
        assert ok is True
        assert q.qsize() == 1

        # Fill to capacity
        for i in range(2, 11):
            assert await _put(f"item_{i}") is True
        assert q.full() is True

        # Now test responsive cancellation on full queue
        cancel_evt.set()
        ok_after_cancel = await _put("overflow_item")
        assert ok_after_cancel is False
