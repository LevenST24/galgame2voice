"""
Unit tests for TTS pause, prosody normalization, and speech chunking optimization.
Tests the fixes for unnatural pauses and modal particles at clause endings.
"""

import asyncio
import wave
from pathlib import Path
from typing import Any, cast
import pytest

from galgame2voice.utils.text_splitter import (
    normalize_dialogue_prosody,
    MODAL_PARTICLES_PATTERN,
    split_japanese_sentences,
)
from galgame2voice.services.gpt_sovits_client import GptSovitsClient
from galgame2voice.services.chat_service import ChatService


class TestDialogueProsodyNormalization:
    """Tests prosodic cleanup of dialogue text before TTS synthesis."""

    def test_collapse_multiple_commas(self):
        raw = "あ、、、、そうですね、、、本当ですか、、"
        res = normalize_dialogue_prosody(raw)
        # Duplicate commas collapsed, trailing comma converted to period
        assert "、、" not in res
        assert res == "あ、そうですね、本当ですか。"

    def test_collapse_ellipsis_with_comma(self):
        raw = "うーん……、どうしようかな……，迷うな……,"
        res = normalize_dialogue_prosody(raw)
        assert "……、" not in res
        assert "……，" not in res
        assert "……," not in res
        assert res.endswith("迷うな……")

    def test_collapse_comma_before_terminal_punct(self):
        raw = "本当ですか、！信じられない、？そうですか、。"
        res = normalize_dialogue_prosody(raw)
        assert "、！" not in res
        assert "、？" not in res
        assert "、。" not in res
        assert res == "本当ですか！信じられない？そうですか。"

    def test_remove_leading_and_normalize_trailing_comma(self):
        raw = "、こんにちは、元気ですか、"
        res = normalize_dialogue_prosody(raw)
        assert not res.startswith("、")
        assert res.endswith("。")
        assert res == "こんにちは、元気ですか。"

    def test_empty_and_whitespace_preservation(self):
        assert normalize_dialogue_prosody("") == ""
        assert normalize_dialogue_prosody("   ") == ""


class TestModalParticlesPattern:
    """Verifies modal particles regex correctly detects sentence-ending particles and soft connectors."""

    @pytest.mark.parametrize("clause,expected", [
        ("そうですね", True),
        ("違うよ", True),
        ("わたしわ", True),
        ("いいな", True),
        ("だよね", True),
        ("行かないわよ", True),
        ("知らんけど", True),
        ("好きだから", True),
        ("暑いので", True),
        ("行ったのに", True),
        ("嬉しいなぁ", True),
        ("いいかもねぇ", True),
        ("好啊", True),
        ("是吗", True),
        ("走吧", True),
        ("真棒呀", True),
        ("こんにちは", False),
        ("先生", False),
        ("学校", False),
        ("東京", False),
    ])
    def test_particle_matching(self, clause, expected):
        assert bool(MODAL_PARTICLES_PATTERN.search(clause)) is expected


class TestGptSovitsPayloadFragmentInterval:
    """Verifies GptSovitsClient._build_tts_payload includes fragment_interval and normalizes prosody."""

    def test_payload_includes_fragment_interval_default(self):
        client = GptSovitsClient()
        payload = client._build_tts_payload(
            text="こんにちは、、先生、",
            options={"text_lang": "ja", "ref_audio_path": "dummy.wav", "prompt_text": "dummy", "prompt_lang": "ja"},
        )
        assert payload["fragment_interval"] == 0.3
        # Trailing comma normalized to period so TTS intonation drops naturally
        assert payload["text"] == "こんにちは、先生。"

    def test_payload_includes_custom_fragment_interval(self):
        client = GptSovitsClient()
        payload = client._build_tts_payload(
            text="テストです。",
            options={
                "text_lang": "ja",
                "ref_audio_path": "dummy.wav",
                "prompt_text": "dummy",
                "prompt_lang": "ja",
                "fragment_interval": 0.15,
            },
        )
        assert payload["fragment_interval"] == 0.15


class TestWavConcatWithPauseSilence:
    """Verifies ChatService._concat_wav_files inserts silence frames between chunks when pause_duration > 0."""

    def _write_test_wav(self, path: Path, n_frames: int = 100, rate: int = 16000, sampwidth: int = 2):
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(sampwidth)
            w.setframerate(rate)
            # 16-bit audio: 2 bytes per sample
            w.writeframes(b"\x10\x20" * n_frames)

    def test_concat_wav_zero_pause_duration(self, tmp_path):
        service = ChatService(cast(Any, None), cast(Any, None), cast(Any, None))
        c1 = tmp_path / "c1.wav"
        c2 = tmp_path / "c2.wav"
        out = tmp_path / "out_zero.wav"
        self._write_test_wav(c1, n_frames=100, rate=16000)
        self._write_test_wav(c2, n_frames=100, rate=16000)

        ok = service._concat_wav_files([str(c1), str(c2)], out, pause_duration=0.0)
        assert ok is True
        with wave.open(str(out), "rb") as w:
            assert w.getnframes() == 200

    def test_concat_wav_with_pause_duration(self, tmp_path):
        service = ChatService(cast(Any, None), cast(Any, None), cast(Any, None))
        c1 = tmp_path / "c1.wav"
        c2 = tmp_path / "c2.wav"
        out = tmp_path / "out_paused.wav"
        rate = 16000
        pause_sec = 0.25  # 0.25s * 16000 = 4000 silence frames
        self._write_test_wav(c1, n_frames=100, rate=rate)
        self._write_test_wav(c2, n_frames=100, rate=rate)

        ok = service._concat_wav_files([str(c1), str(c2)], out, pause_duration=pause_sec)
        assert ok is True
        with wave.open(str(out), "rb") as w:
            expected_silence_frames = int(rate * pause_sec)
            assert w.getnframes() == 100 + expected_silence_frames + 100

            # Inspect that silence frames in the middle are all zero bytes
            w.setpos(100)
            silence_bytes = w.readframes(expected_silence_frames)
            assert silence_bytes == b"\x00" * (expected_silence_frames * 2)


class TestChatServiceTextSplitMethodDefault:
    """Verifies that single sentence TTS requests default to cut0 (no comma splitting) to avoid disjoint pauses."""

    @pytest.mark.asyncio
    async def test_stream_chat_defaults_cut0_for_short_sentence(self, tmp_path):
        from unittest.mock import AsyncMock, MagicMock
        from galgame2voice.services.chat_service import ChatService
        from galgame2voice.adapters.base import ChatMessage

        mock_tts = MagicMock()
        captured_options = []

        async def _mock_synth(sentence, options=None, filename_prefix="chunk"):
            captured_options.append(dict(options or {}))
            p = tmp_path / f"{filename_prefix}.wav"
            with wave.open(str(p), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(16000)
                w.writeframes(b"\x00\x00" * 10)
            return f"/audio/{p.name}", p, 20

        mock_tts.synthesize_to_file = AsyncMock(side_effect=_mock_synth)
        mock_tts.audio_dir = tmp_path

        mock_adapter = MagicMock()
        async def _mock_stream(*args, **kwargs):
            yield '{"chinese": "你好", "japanese": "そうですね、私もそう思いますよ。"}'

        mock_adapter.stream_chat = _mock_stream

        service = ChatService(tts_service=mock_tts, db_path=str(tmp_path / "test.db"))
        service._get_active_llm_adapter = AsyncMock(return_value=(mock_adapter, "mock-model", "mock-prov"))
        service._prepare_messages = AsyncMock(return_value=[ChatMessage(role="user", content="hi")])
        service.affection_service = MagicMock()
        service.affection_service.handle_turn_affection = AsyncMock(return_value={"emotion": "gentle"})
        service.metrics_collector = MagicMock()
        service.metrics_collector.estimate_tokens = MagicMock(return_value=10)
        service.metrics_collector.record_metric = AsyncMock(return_value={})

        from galgame2voice.database.session import init_db
        await init_db(str(tmp_path / "test.db"))

        events = []
        async for ev in service.stream_chat("hi", session_id="test_sess"):
            events.append(ev)

        assert len(captured_options) >= 1
        # When user does not specify text_split_method, chunk <= 80 defaults to cut0 (no comma slicing)
        assert captured_options[0].get("text_split_method") == "cut0"

    @pytest.mark.asyncio
    async def test_stream_chat_preserves_user_split_method_override(self, tmp_path):
        from unittest.mock import AsyncMock, MagicMock
        from galgame2voice.services.chat_service import ChatService
        from galgame2voice.adapters.base import ChatMessage

        mock_tts = MagicMock()
        captured_options = []

        async def _mock_synth(sentence, options=None, filename_prefix="chunk"):
            captured_options.append(dict(options or {}))
            p = tmp_path / f"{filename_prefix}.wav"
            with wave.open(str(p), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(16000)
                w.writeframes(b"\x00\x00" * 10)
            return f"/audio/{p.name}", p, 20

        mock_tts.synthesize_to_file = AsyncMock(side_effect=_mock_synth)
        mock_tts.audio_dir = tmp_path

        mock_adapter = MagicMock()
        async def _mock_stream(*args, **kwargs):
            yield '{"chinese": "你好", "japanese": "そうですね、私もそう思いますよ。"}'

        mock_adapter.stream_chat = _mock_stream

        service = ChatService(tts_service=mock_tts, db_path=str(tmp_path / "test.db"))
        service._get_active_llm_adapter = AsyncMock(return_value=(mock_adapter, "mock-model", "mock-prov"))
        service._prepare_messages = AsyncMock(return_value=[ChatMessage(role="user", content="hi")])
        service.affection_service = MagicMock()
        service.affection_service.handle_turn_affection = AsyncMock(return_value={"emotion": "gentle"})
        service.metrics_collector = MagicMock()
        service.metrics_collector.estimate_tokens = MagicMock(return_value=10)
        service.metrics_collector.record_metric = AsyncMock(return_value={})

        from galgame2voice.database.session import init_db
        await init_db(str(tmp_path / "test.db"))

        events = []
        async for ev in service.stream_chat("hi", session_id="test_sess_override", tts_options={"text_split_method": "cut5"}):
            events.append(ev)

        assert len(captured_options) >= 1
        # Explicit user option is respected
        assert captured_options[0].get("text_split_method") == "cut5"


class TestModalParticleSentenceSplitting:
    """Verifies that clauses ending in modal particles are NOT split on soft commas."""

    @pytest.mark.parametrize("text,expected_count", [
        ("そうですね、私もそう思いますよ。", 1),
        ("違うよ、それは誤解だよ。", 1),
        ("わたしわ、いつでもここにいるよ。", 1),
        ("いいな、一緒に行きたいな。", 1),
        ("行かないわよ、危ないもの。", 1),
        ("知らんけど、たぶん大丈夫。", 1),
        ("好きだから、一緒にいよう。", 1),
        ("暑いので、窓を開けましょう。", 1),
        ("行ったのに、誰もいなかったよ。", 1),
        ("嬉しいなぁ、ありがとう。", 1),
        ("いいかもねぇ、そうしよう。", 1),
        ("好啊，我们一起去吧。", 1),
        ("是吗，我怎么不知道。", 1),
        ("走吧，时间差不多了。", 1),
        ("真棒呀，太厉害了。", 1),
        ("どうしようかな……、迷うな……。", 1),
        ("そうですね〜、わかりました。", 1),
        ("いいよー、任せて。", 1),
        ("あのね、実はね、言いたいことがあるの。", 1),
    ])
    def test_modal_particles_prevent_splitting(self, text, expected_count):
        sents = split_japanese_sentences(text, is_first_chunk=True, min_chars=6)
        assert len(sents) == expected_count
        assert "".join(sents) == text


class TestStreamingParserModalParticleHandling:
    """Verifies StreamingBilingualParser does not prematurely emit modal particle clauses."""

    def test_parser_holds_modal_particle_clause_until_sentence_completes(self):
        from galgame2voice.services.streaming_parser import StreamingBilingualParser
        parser = StreamingBilingualParser()

        # Feed chunk 1: ends in modal particle comma
        ch1, ja1 = parser.feed_chunk('{"chinese": "好", "japanese": "そうですね、')
        assert ja1 == [], "Must not prematurely emit modal particle clause"
        assert parser.first_sentence_emitted is False

        # Feed chunk 2: sentence completes
        ch2, ja2 = parser.feed_chunk('私もそう思いますよ。"}')
        assert len(ja2) == 1
        assert ja2[0] == "そうですね、私もそう思いますよ。"
        assert parser.first_sentence_emitted is True

        ch_full, ja_full, rem = parser.finalize()
        assert ch_full == "好"
        assert ja_full == "そうですね、私もそう思いますよ。"
        assert rem == []

    def test_parser_emits_greeting_clause_immediately(self):
        from galgame2voice.services.streaming_parser import StreamingBilingualParser
        parser = StreamingBilingualParser()

        # Feed chunk 1: greeting (non-modal)
        ch1, ja1 = parser.feed_chunk('{"chinese": "你好", "japanese": "こんにちは、')
        assert ja1 == ["こんにちは、"]
        assert parser.first_sentence_emitted is True


class TestTtsServiceProsodySplitDefaults:
    """Verifies TtsService defaults text_split_method to cut0/cut2 when not specified."""

    @pytest.mark.asyncio
    async def test_synthesize_defaults_cut0_for_short_text(self, tmp_path):
        from unittest.mock import AsyncMock, MagicMock
        from galgame2voice.services.tts_service import TtsService

        mock_client = MagicMock()
        captured = []
        async def _mock_synth(text, options=None):
            captured.append(dict(options or {}))
            return b"WAV_DATA"

        mock_client.synthesize = AsyncMock(side_effect=_mock_synth)
        mock_cache = MagicMock()
        mock_cache.db_path = None
        mock_cache.compute_cache_key.return_value = ("key123", "clean", "hash")
        mock_cache.get = AsyncMock(return_value=None)
        mock_cache.put = AsyncMock(return_value=("/audio/test.wav", tmp_path / "test.wav", 8))
        service = TtsService(client=mock_client, audio_dir=tmp_path, cache_manager=mock_cache)

        await service.synthesize("そうですね、私もそう思いますよ。")
        assert len(captured) == 1
        assert captured[0].get("text_split_method") == "cut0"

    @pytest.mark.asyncio
    async def test_synthesize_defaults_cut2_for_long_text(self, tmp_path):
        from unittest.mock import AsyncMock, MagicMock
        from galgame2voice.services.tts_service import TtsService

        mock_client = MagicMock()
        captured = []
        async def _mock_synth(text, options=None):
            captured.append(dict(options or {}))
            return b"WAV_DATA"

        mock_client.synthesize = AsyncMock(side_effect=_mock_synth)
        mock_cache = MagicMock()
        mock_cache.db_path = None
        mock_cache.compute_cache_key.return_value = ("key123", "clean", "hash")
        mock_cache.get = AsyncMock(return_value=None)
        mock_cache.put = AsyncMock(return_value=("/audio/test.wav", tmp_path / "test.wav", 8))
        service = TtsService(client=mock_client, audio_dir=tmp_path, cache_manager=mock_cache)

        long_text = "とても長い文章です。" * 10  # 100 chars > 80
        await service.synthesize(long_text)
        assert len(captured) == 1
        assert captured[0].get("text_split_method") == "cut2"


class TestChatServiceModalParticleSingleChunk:
    """Verifies that dialogue sentences ending with modal particles synthesize as a single chunk."""

    @pytest.mark.asyncio
    async def test_stream_chat_synthesizes_modal_particle_sentence_as_single_chunk(self, tmp_path):
        from unittest.mock import AsyncMock, MagicMock
        from galgame2voice.services.chat_service import ChatService
        from galgame2voice.adapters.base import ChatMessage

        mock_tts = MagicMock()
        synthesized_sentences = []

        async def _mock_synth(sentence, options=None, filename_prefix="chunk"):
            synthesized_sentences.append(sentence)
            p = tmp_path / f"{filename_prefix}.wav"
            with wave.open(str(p), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(16000)
                w.writeframes(b"\x00\x00" * 10)
            return f"/audio/{p.name}", p, 20

        mock_tts.synthesize_to_file = AsyncMock(side_effect=_mock_synth)
        mock_tts.audio_dir = tmp_path

        mock_adapter = MagicMock()
        async def _mock_stream(*args, **kwargs):
            # Stream in two parts: first ends with modal particle comma
            yield '{"chinese": "好的", "japanese": "そうですね、'
            await asyncio.sleep(0.01)
            yield '私もそう思いますよ。"}'

        mock_adapter.stream_chat = _mock_stream

        service = ChatService(tts_service=mock_tts, db_path=str(tmp_path / "test.db"))
        service._get_active_llm_adapter = AsyncMock(return_value=(mock_adapter, "mock-model", "mock-prov"))
        service._prepare_messages = AsyncMock(return_value=[ChatMessage(role="user", content="hi")])
        service.affection_service = MagicMock()
        service.affection_service.handle_turn_affection = AsyncMock(return_value={"emotion": "gentle"})
        service.metrics_collector = MagicMock()
        service.metrics_collector.estimate_tokens = MagicMock(return_value=10)
        service.metrics_collector.record_metric = AsyncMock(return_value={})

        from galgame2voice.database.session import init_db
        await init_db(str(tmp_path / "test.db"))

        events = []
        async for ev in service.stream_chat("hi", session_id="test_modal_sess"):
            events.append(ev)

        # Crucial assertion: exactly 1 chunk was synthesized containing the entire thought
        assert len(synthesized_sentences) == 1
        assert synthesized_sentences[0] == "そうですね、私もそう思いますよ。"


class TestJapaneseSentenceSplitting:
    """Verifies Japanese sentence splitting edge cases (punctuation, whitespace, emojis)."""

    def test_split_japanese_sentences_comprehensive(self):
        text = "おはよう！今日もいい天気ですね。一緒に行きましょう？\n次はこれ！"
        sentences = split_japanese_sentences(text)
        assert len(sentences) == 4
        assert sentences[0] == "おはよう！"
        assert sentences[1] == "今日もいい天气ですね。" or "今日もいい天気ですね。" in sentences[1]
        assert sentences[2] == "一緒に行きましょう？"
        assert sentences[3] == "次はこれ！"

    def test_split_empty_and_whitespace(self):
        assert split_japanese_sentences("") == []
        assert split_japanese_sentences("   \n\n  ") == []

    def test_split_agile_whitespace(self):
        """Blank agile input is empty after strip; tabs are empty on both paths."""
        assert split_japanese_sentences("  \r\n  \t  ", is_first_chunk=True, min_chars=6) == []
        assert split_japanese_sentences("\t\t", is_first_chunk=False) == []
        assert split_japanese_sentences("\t\t", is_first_chunk=True, min_chars=6) == []

    @pytest.mark.parametrize("punct_only", [
        ",", "、", "，", "。", "！", "？", "!", "?",
        ",,,,,,,,", "、、、、、、", "，，，，，，",
        "。。。。。。", "！？！？！？", "!?!?!?",
        "、、，，,,。。。！？!?",
        "   ，，、、。。！？   ",
    ])
    def test_chunking_pure_punctuation(self, punct_only):
        """Punctuation-only text must never crash, never hang, and never lose content."""
        expected_stripped = punct_only.replace(" ", "").replace("\n", "")
        res_default = split_japanese_sentences(punct_only, is_first_chunk=False)
        assert isinstance(res_default, list)
        if expected_stripped:
            assert len(res_default) >= 1
            combined_default = "".join(res_default).replace(" ", "").replace("\n", "")
            assert combined_default == expected_stripped
        else:
            assert res_default == []

        res_agile = split_japanese_sentences(punct_only, is_first_chunk=True, min_chars=6)
        assert isinstance(res_agile, list)
        if expected_stripped:
            assert len(res_agile) >= 1
            combined_agile = "".join(res_agile).replace(" ", "").replace("\n", "")
            assert combined_agile == expected_stripped
        else:
            assert res_agile == []

    def test_chunking_emojis_and_unicode_symbols(self):
        """Emojis, math symbols, and mixed scripts must be preserved safely."""
        emoji_text = "🐱こんにちは！🐶先生、今日はいい天気ですね🎉一緒に出かけましょう？🚀"
        res = split_japanese_sentences(emoji_text, is_first_chunk=True, min_chars=6)
        assert len(res) >= 2
        assert "🐱こんにちは！" in res[0]
        assert "".join(res) == emoji_text

        emoji_clauses = "🍎、🍌、🍇、🍓、🍉、🍒、🍑"
        res_emoji = split_japanese_sentences(emoji_clauses, is_first_chunk=True, min_chars=6)
        assert len(res_emoji) >= 1
        assert "".join(res_emoji) == emoji_clauses

    def test_r1_split_japanese_sentences_clause_pauses(self):
        """Verify split_japanese_sentences splits on clause pauses for chunk 0 when len >= 6."""
        # Japanese comma '、' with len >= 6
        text_ja = "こんにちは、先生、今日はいい天気ですね。"
        sents = split_japanese_sentences(text_ja, is_first_chunk=True, min_chars=6)
        assert len(sents) == 2
        assert sents[0] == "こんにちは、"
        assert sents[1] == "先生、今日はいい天気ですね。"

        # Fullwidth Chinese comma '，' with len >= 6
        text_cn = "初めまして，よろしくお願いします。"
        sents_cn = split_japanese_sentences(text_cn, is_first_chunk=True, min_chars=6)
        assert len(sents_cn) == 2
        assert sents_cn[0] == "初めまして，"
        assert sents_cn[1] == "よろしくお願いします。"

        # Standard ASCII comma ',' with len >= 6
        text_ascii = "Hello world,今日も一日頑張りましょう。"
        sents_ascii = split_japanese_sentences(text_ascii, is_first_chunk=True, min_chars=6)
        assert len(sents_ascii) == 2
        assert sents_ascii[0] == "Hello world,"
        assert sents_ascii[1] == "今日も一日頑張りましょう。"

    def test_r1_split_japanese_sentences_backwards_compatibility(self):
        """Verify is_first_chunk=False strictly preserves clauses and only terminal punctuation splits."""
        text = "こんにちは、先生、今日はいい天気ですね。一緒に出かけましょう？"
        # Strict mode (default / is_first_chunk=False)
        strict_sents = split_japanese_sentences(text, is_first_chunk=False)
        assert len(strict_sents) == 2
        assert strict_sents[0] == "こんにちは、先生、今日はいい天気ですね。"
        assert strict_sents[1] == "一緒に出かけましょう？"

        # Subsequent chunks under is_first_chunk=True preserve terminal punctuation
        sents_all = split_japanese_sentences(text, is_first_chunk=True, min_chars=6)
        assert len(sents_all) == 3
        assert sents_all[0] == "こんにちは、"
        assert sents_all[1] == "先生、今日はいい天気ですね。"
        assert sents_all[2] == "一緒に出かけましょう？"

    def test_r1_split_short_clause_no_premature_split(self):
        """Clause pauses with length < min_chars must NOT split prematurely."""
        # 'はい、' length is 3 < 6 -> must not split on 'はい、'
        short_clause = "はい、先生、今日はいい天気ですね。"
        sents = split_japanese_sentences(short_clause, is_first_chunk=True, min_chars=6)
        # 'はい、先生、' has length 6 -> splits at second comma!
        assert sents[0] == "はい、先生、"
        assert sents[1] == "今日はいい天気ですね。"

        # Terminal punctuation overrides min_chars
        terminal_short = "はい！先生、おはようございます。"
        sents_term = split_japanese_sentences(terminal_short, is_first_chunk=True, min_chars=6)
        assert sents_term[0] == "はい！"


