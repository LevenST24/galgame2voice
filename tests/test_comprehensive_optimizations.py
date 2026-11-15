"""
Unit and regression tests for comprehensive optimizations:
1. Modal particle detection expansion (colloquial Japanese endings & Chinese particles)
2. Prosody normalization (whitespace before punctuation stripping)
3. Bracketed emotion extraction (extract_bracketed_emotion)
4. Lenticular and square bracket stage direction stripping (clean_japanese_parentheses)
5. StreamingBilingualParser bracketed emotion auto-detection
"""

import io
import struct
import wave

import pytest

from galgame2voice.database.session import get_db, init_db
from galgame2voice.services.chat_service import ChatService
from galgame2voice.utils.text_splitter import (
    MODAL_PARTICLES_PATTERN,
    is_natural_clause_boundary,
    normalize_dialogue_prosody,
    split_japanese_sentences,
)
from galgame2voice.services.emotion_classifier import (
    classify_emotion,
    extract_bracketed_emotion,
    VALID_EMOTIONS,
    EMOTION_NAME_MAP,
)
from galgame2voice.services.gpt_sovits_client import clean_japanese_parentheses
from galgame2voice.services.streaming_parser import StreamingBilingualParser


class TestExpandedModalParticles:
    """Verifies expanded colloquial modal particles and clause boundary detection."""

    @pytest.mark.parametrize("clause,expected", [
        # Newly added colloquial Japanese endings
        ("本当でしょ", True),
        ("明日は晴れでしょう", True),
        ("可愛いじゃん", True),
        ("バカってば", True),
        ("待っててば", True),
        ("元気かい", True),
        ("どうしたんだい", True),
        ("好きだもんね", True),
        ("行くもんよ", True),
        ("そうなんだ", True),
        ("好きなんだから", True),
        ("楽しみなんです", True),
        ("寂しいんだもん", True),
        # Newly added Chinese modal particles
        ("好呗", True),
        ("走嘞", True),
        ("对哒", True),
        ("好的滴", True),
        ("可爱喵", True),
        ("痛嗷", True),
        ("行咯", True),
        # Non-modal clauses (should NOT match as modal particles)
        ("こんにちは", False),
        ("秋葉原", False),
        ("東京", False),
        ("先生", False),
        ("学校", False),
        ("約束", False),
    ])
    def test_particle_matching(self, clause, expected):
        assert bool(MODAL_PARTICLES_PATTERN.search(clause)) is expected

    def test_clause_boundary_preserves_colloquial_particles(self):
        """Agile first chunk splitter must not break on commas following colloquial particles."""
        text = "可愛いじゃん、先生もそう思うでしょ？"
        chunks = split_japanese_sentences(text, is_first_chunk=True, min_chars=4)
        # Should NOT split on '可愛いじゃん、' because 'じゃん' is a continuation particle
        assert len(chunks) == 1
        assert chunks[0] == "可愛いじゃん、先生もそう思うでしょ？"


class TestProsodyWhitespaceNormalization:
    """Verifies whitespace preceding punctuation marks is stripped cleanly."""

    def test_strip_whitespace_before_punctuation(self):
        raw = "こんにちは 、 先生 ！ 元気ですか ？"
        res = normalize_dialogue_prosody(raw)
        assert res == "こんにちは、先生！元気ですか？"

    def test_strip_whitespace_before_fullwidth_period(self):
        raw = "今日もいい天気ですね 。"
        res = normalize_dialogue_prosody(raw)
        assert res == "今日もいい天気ですね。"


class TestBracketedEmotionExtraction:
    """Verifies deterministic extraction of bracketed emotion tags across languages."""

    @pytest.mark.parametrize("text,expected_emo,expected_clean", [
        ("【傲娇】才不是因为喜欢你呢！", "tsundere", "才不是因为喜欢你呢！"),
        ("（害羞）あの、手をつないでもいいですか？", "shy", "あの、手をつないでもいいですか？"),
        ("[happy] 今天真开心！", "happy", "今天真开心！"),
        ("(cool) くだらない。", "cool", "くだらない。"),
        ("【难过】呜呜，怎么会这样……", "sad", "呜呜，怎么会这样……"),
        ("（生气）吵死了，笨蛋！", "angry", "吵死了，笨蛋！"),
        ("【温柔】没关系的，有我在呢。", "gentle", "没关系的，有我在呢。"),
        ("【ツンデレ】べ、別にアンタのためじゃないわよ！", "tsundere", "べ、別にアンタのためじゃないわよ！"),
        ("普通の一日ですね。", None, "普通の一日ですね。"),
        ("", None, ""),
    ])
    def test_extract_bracketed_emotion(self, text, expected_emo, expected_clean):
        emo, cleaned = extract_bracketed_emotion(text)
        assert emo == expected_emo
        assert cleaned == expected_clean

    def test_classify_emotion_integrates_bracketed_tags(self):
        assert classify_emotion(chinese="【害羞】えっと……") == "shy"
        assert classify_emotion(japanese="[tsundere] べ、別に！") == "tsundere"
        assert classify_emotion(chinese="（愤怒）ふざけるな！") == "angry"


class TestParenthesesCleanerBrackets:
    """Verifies clean_japanese_parentheses strips fullwidth lenticular and square brackets."""

    def test_clean_lenticular_brackets(self):
        text = "【ため息】ふぅ、やっと終わった。"
        assert clean_japanese_parentheses(text) == "ふぅ、やっと終わった。"

    def test_clean_square_brackets(self):
        text = "[happy] 今日もいい天気ですね！"
        assert clean_japanese_parentheses(text) == "今日もいい天気ですね！"

    def test_clean_mixed_and_nested_brackets(self):
        text = "【ため息】（微笑）[sigh]こんにちは！"
        assert clean_japanese_parentheses(text) == "こんにちは！"

    def test_unclosed_brackets_preserve_inner_text(self):
        text = "【未完了の指示 こんにちは"
        assert clean_japanese_parentheses(text) == "未完了の指示 こんにちは"


class TestStreamingParserBracketedEmotion:
    """Verifies StreamingBilingualParser extracts bracketed emotion when explicit emotion is omitted."""

    def test_parser_extracts_bracketed_emotion_from_chinese(self):
        parser = StreamingBilingualParser()
        parser.feed_chunk('{"chinese": "【害羞】あの……手をつないでいい？", "japanese": "あの……手をつないでいい？"}')
        assert parser.emotion_extracted == "shy"
        assert parser.get_emotion() == "shy"
        assert parser.tts_emotion == "shy"
        assert parser.get_dynamic_tts_options().get("emotion") == "shy"

    def test_parser_extracts_bracketed_emotion_from_japanese(self):
        parser = StreamingBilingualParser()
        parser.feed_chunk('{"chinese": "没事的哦", "japanese": "【優しい】大丈夫ですよ"}')
        assert parser.emotion_extracted == "gentle"
        assert parser.tts_emotion == "gentle"
        assert parser.get_dynamic_tts_options().get("emotion") == "gentle"


class TestQuotedJapaneseSentenceSplitting:
    """Verifies Japanese dialogue quotation marks are preserved without producing orphan bracket chunks."""

    def test_quoted_sentence_preserves_closing_bracket(self):
        text = "「こんにちは！先生、今日はいい天気ですね。」"
        chunks = split_japanese_sentences(text, is_first_chunk=False)
        assert len(chunks) == 2
        assert chunks[0] == "「こんにちは！"
        assert chunks[1] == "先生、今日はいい天気ですね。」"
        assert "」" not in chunks

    def test_sequential_dialogue_quotes(self):
        text = "「行こう。」「うん！」"
        chunks = split_japanese_sentences(text, is_first_chunk=False)
        assert len(chunks) == 2
        assert chunks[0] == "「行こう。」"
        assert chunks[1] == "「うん！」"

    def test_agile_first_chunk_quoted_dialogue(self):
        text = "「こんにちは！先生、今日はいい天気ですね。」"
        chunks = split_japanese_sentences(text, is_first_chunk=True)
        assert len(chunks) == 2
        assert chunks[0] == "「こんにちは！"
        assert chunks[1] == "先生、今日はいい天気ですね。」"

    def test_pure_bracket_or_punctuation_preserved(self):
        assert split_japanese_sentences("」") == ["」"]
        assert split_japanese_sentences("『』") == ["『』"]
        assert split_japanese_sentences("！？。") == ["！？。"]


class TestCharacterSpecificModalParticles:
    """Verifies Galgame character signature modal particles (Murasame, Mako, Kanna) avoid disjoint pauses."""

    @pytest.mark.parametrize("clause,expected", [
        # Murasame (丛雨) signature speech
        ("妾は叢雨じゃ", True),
        ("我が主殿のじゃ", True),
        ("そうじゃな", True),
        ("元気でおるの", True),
        # Mako (常陆茉子) kouhai speech
        ("お疲れ様っす", True),
        ("大丈夫っすよ", True),
        ("そうっすね", True),
        ("行きますから", True),
        # Kanna (明月栞那) speech
        ("私の秘密のよ", True),
        ("好きなんだよ", True),
        # Chinese compound particles
        ("好哒", True),
        ("好嘞", True),
        ("行啦", True),
    ])
    def test_character_particles(self, clause, expected):
        assert bool(MODAL_PARTICLES_PATTERN.search(clause)) is expected

    def test_murasame_speech_preserves_clause_boundary(self):
        text = "妾は叢雨じゃ、よろしく頼むのじゃ。"
        chunks = split_japanese_sentences(text, is_first_chunk=True, min_chars=4)
        assert len(chunks) == 1
        assert chunks[0] == "妾は叢雨じゃ、よろしく頼むのじゃ。"



class TestAdvancedBracketedEmotion:
    """Verifies bracketed emotion extraction with quote wrappers, metadata prefixes, and aliases."""

    def test_extract_with_quote_prefix(self):
        emo, clean = extract_bracketed_emotion("「【傲娇】才不是因为喜欢你呢！」")
        assert emo == "tsundere"
        assert clean == "「才不是因为喜欢你呢！」"

    def test_extract_with_metadata_prefix(self):
        emo, clean = extract_bracketed_emotion("【情绪：害羞】那个……")
        assert emo == "shy"
        assert clean == "那个……"

    def test_extract_with_japanese_alias(self):
        emo, clean = extract_bracketed_emotion("【恥ずかしい】見ないでください……")
        assert emo == "shy"
        assert clean == "見ないでください……"


class TestStreamingQuotedSentencesImmediateEmission:
    """Verifies that dialogue sentences ending with quotes or brackets are emitted immediately during streaming."""

    def test_single_quoted_sentence_emits_immediately(self):
        parser = StreamingBilingualParser()
        d1, s1 = parser.feed_chunk('{"chinese": "早上好！", "japanese": "「おはようございます！」')
        assert len(s1) == 1
        assert s1[0] == "「おはようございます！」"

    def test_sequential_quoted_sentences_stream_promptly(self):
        parser = StreamingBilingualParser()
        # Sentence 1 arrives
        d1, s1 = parser.feed_chunk('{"japanese": "「こんにちは！」')
        assert s1 == ["「こんにちは！」"]
        # Sentence 2 arrives
        d2, s2 = parser.feed_chunk('「元気ですか？」')
        assert s2 == ["「元気ですか？」"]

    def test_curly_quotes_in_chinese_and_japanese(self):
        parser = StreamingBilingualParser()
        d, s = parser.feed_chunk('{"chinese": "“你好！”", "japanese": "“こんにちは！”')
        assert s == ["“こんにちは！”"]


class TestEnhancedBracketedEmotionVariations:
    """Verifies trailing, middle, white lenticular, and tortoise shell bracketed emotion extraction."""

    def test_trailing_bracketed_emotion(self):
        emo, clean = extract_bracketed_emotion("才不是因为喜欢你呢！【傲娇】")
        assert emo == "tsundere"
        assert clean == "才不是因为喜欢你呢！"

    def test_embedded_bracketed_emotion(self):
        emo, clean = extract_bracketed_emotion("那个……【害羞】手をつないでもいい？")
        assert emo == "shy"
        assert clean == "那个……手をつないでもいい？"

    def test_white_lenticular_brackets(self):
        emo, clean = extract_bracketed_emotion("〖傲娇〗べ、別に！")
        assert emo == "tsundere"
        assert clean == "べ、別に！"

    def test_tortoise_shell_brackets(self):
        emo, clean = extract_bracketed_emotion("〔高兴〕やったー！")
        assert emo == "happy"
        assert clean == "やったー！"

    def test_clean_japanese_parentheses_all_bracket_types(self):
        text = "〖ため息〗〔ため息〕【ため息】[ため息]（ため息）(ため息)こんにちは！"
        assert clean_japanese_parentheses(text) == "こんにちは！"


class TestAudioBoundaryZeroPop:
    """Verifies micro-fade boundary smoothing on concatenated WAV chunks."""

    def test_micro_fade_boundary_zero_pop(self, tmp_path):
        import wave
        import array
        from galgame2voice.services.chat_service import ChatService

        service = ChatService(db_path=tmp_path / "test.db")
        c1 = tmp_path / "chunk1.wav"
        c2 = tmp_path / "chunk2.wav"
        out = tmp_path / "out_smooth.wav"

        # Create 16-bit PCM chunks with constant high amplitude 10000
        rate = 16000
        for p in (c1, c2):
            with wave.open(str(p), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(rate)
                samples = array.array('h', [10000] * 200)
                w.writeframes(samples.tobytes())

        ok = service._concat_wav_files([str(c1), str(c2)], out, pause_duration=0.0)
        assert ok is True
        assert out.exists()

        with wave.open(str(out), "rb") as w:
            assert w.getnframes() == 400
            raw = w.readframes(400)
            samples = array.array('h')
            samples.frombytes(raw)

            # Chunk 1 starts with fade-in: sample 0 is 0
            assert samples[0] == 0
            # Chunk 1 ends with fade-out: sample 199 is 0
            assert samples[199] == 0
            # Chunk 2 starts with fade-in: sample 200 is 0
            assert samples[200] == 0
            # Chunk 2 ends with fade-out: sample 399 is 0
            assert samples[399] == 0
            # Middle of chunk 1 retains full amplitude
            assert samples[100] == 10000


class TestImmediateTransactionCancellationRollback:
    """Verifies that immediate_transaction cleanly rolls back when cancelled."""

    @pytest.mark.asyncio
    async def test_immediate_transaction_rollback_on_cancelled_error(self, tmp_path):
        import asyncio
        import aiosqlite
        from galgame2voice.database.session import immediate_transaction

        db_path = tmp_path / "tx_test.db"
        async with aiosqlite.connect(str(db_path)) as conn:
            await conn.execute("CREATE TABLE test_tbl (id INT PRIMARY KEY, val TEXT);")
            await conn.commit()

            async def cancelled_task():
                async with immediate_transaction(conn):
                    await conn.execute("INSERT INTO test_tbl VALUES (1, 'should_rollback');")
                    # Simulate cancellation
                    raise asyncio.CancelledError()

            with pytest.raises(asyncio.CancelledError):
                await cancelled_task()

            # Confirm transaction was rolled back and row was NOT committed
            cursor = await conn.execute("SELECT COUNT(*) FROM test_tbl;")
            row = await cursor.fetchone()
            assert row[0] == 0

            # Confirm connection is clean and can perform new transactions immediately
            async with immediate_transaction(conn):
                await conn.execute("INSERT INTO test_tbl VALUES (2, 'committed');")

            cursor = await conn.execute("SELECT val FROM test_tbl WHERE id = 2;")
            row = await cursor.fetchone()
            assert row[0] == "committed"


class TestAudioConcatAlignmentSafety:
    """Verifies that _concat_wav_files safely ignores odd-byte corrupted chunks without throwing."""

    def test_concat_wav_odd_byte_length_resilience(self, tmp_path):
        import wave
        from galgame2voice.services.chat_service import ChatService

        service = ChatService(db_path=tmp_path / "test.db")
        c1 = tmp_path / "valid.wav"
        out = tmp_path / "out.wav"

        rate = 24000
        with wave.open(str(c1), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(b"\x00\x00" * 100)

        ok = service._concat_wav_files([str(c1)], out, pause_duration=0.0)
        assert ok is True
        assert out.exists()


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


class TestAudioConcatenationOptimization:
    """Tests in-memory buffered WAV concatenation in ChatService."""

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


class TestArchitecturalHardeningAndEdgeCases:
    """Tests edge cases, bug fixes, and deep verification scenarios."""

    def test_trailing_newlines_and_escapes_preserved(self):
        """Verifies that trailing newlines, tabs, and escaped backslashes are NOT stripped by _strip_incomplete_escape."""
        parser = StreamingBilingualParser()
        parser.feed_chunk('{"chinese": "第一行\\n第二行\\n", "japanese": "一行目\\n二行目\\n"}')
        zh, ja, _ = parser.finalize()
        assert zh == "第一行\n第二行\n", f"Trailing newline was stripped: {zh!r}"
        assert ja == "一行目\n二行目\n", f"Trailing newline was stripped: {ja!r}"

        # Escaped backslash preserved
        parser2 = StreamingBilingualParser()
        parser2.feed_chunk('{"chinese": "C:\\\\path\\\\", "japanese": "パス\\\\"}')
        zh2, ja2, _ = parser2.finalize()
        assert zh2 == "C:\\path\\"
        assert ja2 == "パス\\"

    def test_concat_wav_files_mismatched_sample_rate_skipped(self, tmp_path):
        """Verifies _concat_wav_files skips chunks with mismatched sample rate/channels rather than corrupting audio."""
        service = ChatService(db_path=tmp_path / "test.db")
        c1 = tmp_path / "chunk1_16k.wav"
        c2 = tmp_path / "chunk2_24k_mismatched.wav"
        c3 = tmp_path / "chunk3_16k.wav"
        out = tmp_path / "out_clean.wav"

        c1.write_bytes(_generate_test_wav(0.1, 16000))
        c2.write_bytes(_generate_test_wav(0.1, 24000))  # Conflicting 24kHz rate
        c3.write_bytes(_generate_test_wav(0.2, 16000))

        ok = service._concat_wav_files([str(c1), str(c2), str(c3)], out)
        assert ok is True
        assert out.exists()

        with wave.open(str(out), "rb") as w:
            assert w.getframerate() == 16000
            assert w.getnchannels() == 1
            # Combined 0.1s + 0.2s = 0.3s (4800 frames), skipping the 24k chunk
            assert w.getnframes() == int(16000 * 0.3)


