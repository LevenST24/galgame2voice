"""
Unit and regression tests for comprehensive optimizations:
1. Modal particle detection expansion (colloquial Japanese endings & Chinese particles)
2. Prosody normalization (whitespace before punctuation stripping)
3. Bracketed emotion extraction (extract_bracketed_emotion)
4. Lenticular and square bracket stage direction stripping (clean_japanese_parentheses)
5. StreamingBilingualParser bracketed emotion auto-detection
"""

import pytest

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


class TestAffectionStagePromptInjection:
    """Verifies MemoryService formats 4-stage progressive affection model guidance."""

    def test_stage_1_guidance_low_score(self):
        from galgame2voice.services.memory_service import MemoryService
        svc = MemoryService()
        block = svc.format_memory_prompt_block(
            memories=None,
            affection_info={"score": 10, "level": 1, "level_name": "初识/生疏", "emotion": "normal"}
        )
        assert "阶段一（0-20分【初识相识】）" in block
        assert "初识/生疏" in block

    def test_stage_3_guidance_high_score(self):
        from galgame2voice.services.memory_service import MemoryService
        svc = MemoryService()
        block = svc.format_memory_prompt_block(
            memories=None,
            affection_info={"score": 65, "level": 4, "level_name": "亲密/依赖", "emotion": "shy"}
        )
        assert "阶段三（51-80分【心动共鸣】）" in block
        assert "亲密/依赖" in block

    def test_stage_4_guidance_max_score(self):
        from galgame2voice.services.memory_service import MemoryService
        svc = MemoryService()
        block = svc.format_memory_prompt_block(
            memories=None,
            affection_info={"score": 95, "level": 5, "level_name": "恋慕/誓约", "emotion": "gentle"}
        )
        assert "阶段四（81-100分【恋慕誓约】）" in block


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


class TestAffectionServiceSynonymAndCaseTolerance:
    """Verifies AffectionService handles whitespace and case variations in explicit_emotion."""

    @pytest.mark.asyncio
    async def test_explicit_emotion_normalization(self, tmp_path):
        from galgame2voice.services.affection_service import AffectionService
        from galgame2voice.database.session import init_db

        svc = AffectionService(db_path=tmp_path / "aff_test.db")
        await init_db(svc.db_path)

        res = await svc.handle_turn_affection(
            user_id="u1",
            character_id=1,
            user_text="你好",
            assistant_text="你好啊",
            explicit_emotion="  Happy  ",
        )
        assert res["emotion"] == "happy"

        res2 = await svc.handle_turn_affection(
            user_id="u1",
            character_id=1,
            user_text="你好",
            assistant_text="你好啊",
            explicit_emotion="傲娇 ",
        )
        assert res2["emotion"] == "tsundere"


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


