"""
Unit tests for TTS pause, prosody normalization, and speech chunking optimization.
Tests the fixes for unnatural pauses and modal particles at clause endings.
"""

import array
import asyncio
import io
import struct
import wave
from pathlib import Path
from typing import Any, cast
import pytest

from galgame2voice.utils.text_splitter import (
    is_short_salutation,
    normalize_dialogue_prosody,
    MODAL_PARTICLES_PATTERN,
    split_japanese_sentences,
)
from galgame2voice.services.gpt_sovits_client import GptSovitsClient
from galgame2voice.services.chat_service import ChatService


def _bool_cases(true_items: tuple[str, ...], false_items: tuple[str, ...] = ()) -> list[tuple[str, bool]]:
    """Expand clause tuples into (clause, expected_bool) parametrize pairs."""
    return [(c, True) for c in true_items] + [(c, False) for c in false_items]


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

    @pytest.mark.parametrize("clause,expected", _bool_cases(
        (
            "そうですね", "違うよ", "わたしわ", "いいな", "だよね", "行かないわよ",
            "知らんけど", "好きだから", "暑いので", "行ったのに", "嬉しいなぁ", "いいかもねぇ",
            "好啊", "是吗", "走吧", "真棒呀",
        ),
        ("こんにちは", "先生", "学校", "東京"),
    ))
    def test_particle_matching(self, clause, expected):
        assert bool(MODAL_PARTICLES_PATTERN.search(clause)) is expected


class TestGptSovitsPayloadFragmentInterval:
    """Verifies GptSovitsClient._build_tts_payload includes fragment_interval and normalizes prosody."""

    def _build_payload(self, text: str, **extra_options):
        options = {"text_lang": "ja", "ref_audio_path": "dummy.wav", "prompt_text": "dummy", "prompt_lang": "ja"}
        options.update(extra_options)
        return GptSovitsClient()._build_tts_payload(text=text, options=options)

    def test_payload_includes_fragment_interval_default(self):
        payload = self._build_payload("こんにちは、、先生、")
        assert payload["fragment_interval"] == 0.3
        # Trailing comma normalized to period so TTS intonation drops naturally
        assert payload["text"] == "こんにちは、先生。"

    def test_payload_includes_custom_fragment_interval(self):
        payload = self._build_payload("テストです。", fragment_interval=0.15)
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

    def _concat_two_wavs(self, tmp_path: Path, out_name: str, pause_duration: float, rate: int = 16000):
        service = ChatService(cast(Any, None), cast(Any, None), cast(Any, None))
        c1, c2, out = tmp_path / "c1.wav", tmp_path / "c2.wav", tmp_path / out_name
        self._write_test_wav(c1, n_frames=100, rate=rate)
        self._write_test_wav(c2, n_frames=100, rate=rate)
        ok = service._concat_wav_files([str(c1), str(c2)], out, pause_duration=pause_duration)
        return ok, out

    def test_concat_wav_zero_pause_duration(self, tmp_path):
        ok, out = self._concat_two_wavs(tmp_path, "out_zero.wav", pause_duration=0.0)
        assert ok is True
        with wave.open(str(out), "rb") as w:
            assert w.getnframes() == 200

    def test_concat_wav_with_pause_duration(self, tmp_path):
        rate = 16000
        pause_sec = 0.25  # 0.25s * 16000 = 4000 silence frames
        ok, out = self._concat_two_wavs(tmp_path, "out_paused.wav", pause_duration=pause_sec, rate=rate)
        assert ok is True
        with wave.open(str(out), "rb") as w:
            expected_silence_frames = int(rate * pause_sec)
            assert w.getnframes() == 100 + expected_silence_frames + 100

            # Inspect that silence frames in the middle are all zero bytes
            w.setpos(100)
            silence_bytes = w.readframes(expected_silence_frames)
            assert silence_bytes == b"\x00" * (expected_silence_frames * 2)


async def _run_mock_tts_stream_chat(tmp_path, stream_chunks, *, session_id, tts_options=None):
    """Run ChatService.stream_chat with a mocked TTS/LLM adapter and record synth inputs."""
    from unittest.mock import AsyncMock, MagicMock
    from galgame2voice.adapters.base import ChatMessage
    from galgame2voice.database.session import init_db

    mock_tts = MagicMock()
    captured_options = []
    synthesized_sentences = []

    async def _mock_synth(sentence, options=None, filename_prefix="chunk"):
        captured_options.append(dict(options or {}))
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
        for chunk, delay in stream_chunks:
            if delay:
                await asyncio.sleep(delay)
            yield chunk

    mock_adapter.stream_chat = _mock_stream

    service = ChatService(tts_service=mock_tts, db_path=str(tmp_path / "test.db"))
    service._get_active_llm_adapter = AsyncMock(return_value=(mock_adapter, "mock-model", "mock-prov"))
    service._prepare_messages = AsyncMock(return_value=[ChatMessage(role="user", content="hi")])
    service.affection_service = MagicMock()
    service.affection_service.handle_turn_affection = AsyncMock(return_value={"emotion": "gentle"})
    service.metrics_collector = MagicMock()
    service.metrics_collector.estimate_tokens = MagicMock(return_value=10)
    service.metrics_collector.record_metric = AsyncMock(return_value={})

    await init_db(str(tmp_path / "test.db"))

    stream_kwargs = {"session_id": session_id}
    if tts_options is not None:
        stream_kwargs["tts_options"] = tts_options
    events = []
    async for ev in service.stream_chat("hi", **stream_kwargs):
        events.append(ev)
    return captured_options, synthesized_sentences


class TestChatServiceTextSplitMethodDefault:
    """Verifies that single sentence TTS requests default to cut0 (no comma splitting) to avoid disjoint pauses."""

    @pytest.mark.asyncio
    async def test_stream_chat_defaults_cut0_for_short_sentence(self, tmp_path):
        captured_options, _ = await _run_mock_tts_stream_chat(
            tmp_path,
            [('{"chinese": "你好", "japanese": "そうですね、私もそう思いますよ。"}', 0)],
            session_id="test_sess",
        )

        assert len(captured_options) >= 1
        # When user does not specify text_split_method, chunk <= 80 defaults to cut0 (no comma slicing)
        assert captured_options[0].get("text_split_method") == "cut0"

    @pytest.mark.asyncio
    async def test_stream_chat_preserves_user_split_method_override(self, tmp_path):
        captured_options, _ = await _run_mock_tts_stream_chat(
            tmp_path,
            [('{"chinese": "你好", "japanese": "そうですね、私もそう思いますよ。"}', 0)],
            session_id="test_sess_override",
            tts_options={"text_split_method": "cut5"},
        )

        assert len(captured_options) >= 1
        # Explicit user option is respected
        assert captured_options[0].get("text_split_method") == "cut5"


class TestModalParticleSentenceSplitting:
    """Verifies that clauses ending in modal particles are NOT split on soft commas."""

    @pytest.mark.parametrize("text,expected_count", [
        (t, 1) for t in (
            "そうですね、私もそう思いますよ。", "違うよ、それは誤解だよ。",
            "わたしわ、いつでもここにいるよ。", "いいな、一緒に行きたいな。",
            "行かないわよ、危ないもの。", "知らんけど、たぶん大丈夫。",
            "好きだから、一緒にいよう。", "暑いので、窓を開けましょう。",
            "行ったのに、誰もいなかったよ。", "嬉しいなぁ、ありがとう。",
            "いいかもねぇ、そうしよう。", "好啊，我们一起去吧。",
            "是吗，我怎么不知道。", "走吧，时间差不多了。",
            "真棒呀，太厉害了。", "どうしようかな……、迷うな……。",
            "そうですね〜、わかりました。", "いいよー、任せて。",
            "あのね、実はね、言いたいことがあるの。",
        )
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


class TestChatServiceModalParticleSingleChunk:
    """Verifies that dialogue sentences ending with modal particles synthesize as a single chunk."""

    @pytest.mark.asyncio
    async def test_stream_chat_synthesizes_modal_particle_sentence_as_single_chunk(self, tmp_path):
        _, synthesized_sentences = await _run_mock_tts_stream_chat(
            tmp_path,
            [
                ('{"chinese": "好的", "japanese": "そうですね、', 0),
                ('私もそう思いますよ。"}', 0.01),
            ],
            session_id="test_modal_sess",
        )

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


class TestExpandedModalParticles:
    """Verifies expanded colloquial modal particles and clause boundary detection."""

    @pytest.mark.parametrize("clause,expected", _bool_cases(
        (
            "本当でしょ", "明日は晴れでしょう", "可愛いじゃん", "バカってば", "待っててば",
            "元気かい", "どうしたんだい", "好きだもんね", "行くもんよ", "そうなんだ",
            "好きなんだから", "楽しみなんです", "寂しいんだもん", "好呗", "走嘞",
            "对哒", "好的滴", "可爱喵", "痛嗷", "行咯",
        ),
        ("こんにちは", "秋葉原", "東京", "先生", "学校", "約束"),
    ))
    def test_particle_matching(self, clause, expected):
        assert bool(MODAL_PARTICLES_PATTERN.search(clause)) is expected

    def test_clause_boundary_preserves_colloquial_particles(self):
        """Agile first chunk splitter must not break on commas following colloquial particles."""
        text = "可愛いじゃん、先生もそう思うでしょ？"
        chunks = split_japanese_sentences(text, is_first_chunk=True, min_chars=4)
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

    @pytest.mark.parametrize("clause,expected", _bool_cases((
        "妾は叢雨じゃ", "我が主殿のじゃ", "そうじゃな", "元気でおるの",
        "お疲れ様っす", "大丈夫っすよ", "そうっすね", "行きますから",
        "私の秘密のよ", "好きなんだよ", "好哒", "好嘞", "行啦",
    )))
    def test_character_particles(self, clause, expected):
        assert bool(MODAL_PARTICLES_PATTERN.search(clause)) is expected

    def test_murasame_speech_preserves_clause_boundary(self):
        text = "妾は叢雨じゃ、よろしく頼むのじゃ。"
        chunks = split_japanese_sentences(text, is_first_chunk=True, min_chars=4)
        assert len(chunks) == 1
        assert chunks[0] == "妾は叢雨じゃ、よろしく頼むのじゃ。"


def test_is_short_salutation():
    assert is_short_salutation("はい、")
    assert is_short_salutation("ええ、")
    assert is_short_salutation("うん、")
    assert is_short_salutation("あの、")
    assert is_short_salutation("そうですね、")
    assert is_short_salutation("大丈夫、")
    assert not is_short_salutation("今日はとてもいい天気ですね。")
    assert not is_short_salutation("")


class TestAudioBoundaryZeroPop:
    """Verifies micro-fade boundary smoothing on concatenated WAV chunks."""

    def test_micro_fade_boundary_zero_pop(self, tmp_path):
        service = ChatService(db_path=tmp_path / "test.db")
        c1 = tmp_path / "chunk1.wav"
        c2 = tmp_path / "chunk2.wav"
        out = tmp_path / "out_smooth.wav"

        rate = 16000
        for p in (c1, c2):
            with wave.open(str(p), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(rate)
                samples = array.array("h", [10000] * 200)
                w.writeframes(samples.tobytes())

        ok = service._concat_wav_files([str(c1), str(c2)], out, pause_duration=0.0)
        assert ok is True
        assert out.exists()

        with wave.open(str(out), "rb") as w:
            assert w.getnframes() == 400
            raw = w.readframes(400)
            samples = array.array("h")
            samples.frombytes(raw)

            assert samples[0] == 0
            assert samples[199] == 0
            assert samples[200] == 0
            assert samples[399] == 0
            assert samples[100] == 10000


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

        corrupt = tmp_path / "corrupt.wav"
        corrupt.write_bytes(b"not_a_wav")
        valid = tmp_path / "valid.wav"
        valid.write_bytes(_generate_test_wav(0.05, 16000))

        ok = service._concat_wav_files([str(corrupt), str(valid)], out)
        assert ok is True
        with wave.open(str(out), "rb") as w:
            assert w.getnframes() == int(16000 * 0.05)

    def test_concat_wav_files_mismatched_sample_rate_skipped(self, tmp_path):
        """Verifies _concat_wav_files skips chunks with mismatched sample rate/channels rather than corrupting audio."""
        service = ChatService(db_path=tmp_path / "test.db")
        c1 = tmp_path / "chunk1_16k.wav"
        c2 = tmp_path / "chunk2_24k_mismatched.wav"
        c3 = tmp_path / "chunk3_16k.wav"
        out = tmp_path / "out_clean.wav"

        c1.write_bytes(_generate_test_wav(0.1, 16000))
        c2.write_bytes(_generate_test_wav(0.1, 24000))
        c3.write_bytes(_generate_test_wav(0.2, 16000))

        ok = service._concat_wav_files([str(c1), str(c2), str(c3)], out)
        assert ok is True
        assert out.exists()

        with wave.open(str(out), "rb") as w:
            assert w.getframerate() == 16000
            assert w.getnchannels() == 1
            assert w.getnframes() == int(16000 * 0.3)


