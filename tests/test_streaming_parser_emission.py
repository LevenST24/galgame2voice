"""
Regression tests for StreamingBilingualParser Japanese sentence emission.

Covers the invariant that broke during the streaming-cursor refactor: the
sentence splitter runs with ``is_first_chunk=True`` until the first sentence has
been emitted and with ``is_first_chunk=False`` afterwards, and those two modes
produce DIFFERENT boundaries for the same text (agile mode may cut on a clause
comma, e.g. ``["こんにちは、", "先生、…ですね。"]``, while the strict mode merges
them into ``["こんにちは、先生、…ですね。"]``).

A sentence-index cursor therefore reports "nothing new" on the mode flip and
silently DROPS the tail of an already-started sentence. These tests pin the
no-loss / no-duplication guarantee of the monotone character cursor.
"""

import codecs
import json
import re
import pytest
from galgame2voice.services.streaming_parser import StreamingBilingualParser

def _squeeze(text: str) -> str:
    """Removes all whitespace so newline normalization is not mistaken for data loss."""
    return re.sub(r"\s+", "", text)


JAPANESE_CASES = [
    # Agile clause split fires on the first chunk, strict mode merges it back.
    "こんにちは、先生、今日はいい天気ですね。一緒に出かけましょう？",
    # Plain multi-sentence dialogue.
    "おはよう。今日もいい天気ですね。一緒に遊びましょう！",
    # Single sentence, no early clause boundary.
    "私はあなたのことがとても好きです。",
    # Newline separated dialogue with closing brackets.
    "第一行。\n第二行！\n第三行？",
]


def _feed_in_chunks(payload: str, chunk_count: int):
    """Feeds payload to a fresh parser split into chunk_count slices."""
    parser = StreamingBilingualParser()
    size = max(1, len(payload) // chunk_count)
    emitted = []
    for i in range(0, len(payload), size):
        _, sentences = parser.feed_chunk(payload[i:i + size])
        emitted.extend(sentences)
    _, japanese, tail = parser.finalize()
    emitted.extend(tail)
    return emitted, japanese


def test_streaming_emission_is_lossless_and_duplicate_free():
    """Every chunking of the same stream must emit each sentence exactly once."""
    for japanese in JAPANESE_CASES:
        payload = json.dumps({"chinese": "你好呀", "japanese": japanese}, ensure_ascii=False)
        for chunk_count in range(1, 9):
            emitted, extracted = _feed_in_chunks(payload, chunk_count)
            assert extracted == japanese, f"parser lost Japanese text: {extracted!r}"
            # The splitter normalizes whitespace between sentences (newlines are
            # dropped from the emitted units), so compare content modulo whitespace.
            assert _squeeze("".join(emitted)) == _squeeze(japanese), (
                f"lost text for {japanese!r} with {chunk_count} chunk(s): emitted={emitted!r}"
            )
            assert len(emitted) == len(set(emitted)), (
                f"duplicated sentences for {japanese!r} with {chunk_count} chunk(s): {emitted!r}"
            )


def test_agile_clause_boundary_is_not_re_emitted_after_mode_flip():
    """
    Regression: the agile first-chunk clause split (こんにちは、) must not swallow
    the remainder of the sentence once the parser switches to strict splitting.
    """
    first = '{"chinese": "你好", "japanese": "こんにちは、先生、今日はいい天気ですね'
    second = '。"}'

    parser = StreamingBilingualParser()
    _, round1 = parser.feed_chunk(first)
    _, round2 = parser.feed_chunk(second)
    _, japanese, tail = parser.finalize()
    emitted = round1 + round2 + tail

    assert round1 == ["こんにちは、"], f"agile clause split regressed: {round1!r}"
    assert "".join(emitted) == japanese == "こんにちは、先生、今日はいい天気ですね。", (
        f"tail of the sentence was dropped: emitted={emitted!r}"
    )
    assert len(emitted) == len(set(emitted)), f"duplicated sentences: {emitted!r}"


def test_r1_streaming_bilingual_parser_agile_first_chunk():
    """Verify StreamingBilingualParser emits chunk 0 immediately upon clause pause."""
    parser = StreamingBilingualParser()
    # Feed chunk 0 containing clause pause with length >= 6
    ch_delta1, ja_sents1 = parser.feed_chunk('{"chinese": "你好", "japanese": "こんにちは、')
    assert ja_sents1 == ["こんにちは、"]
    assert parser.first_sentence_emitted is True

    # Feed chunk 1 with intermediate clause pause: should NOT split on clause pause because chunk 0 already emitted
    ch_delta2, ja_sents2 = parser.feed_chunk('先生、今日はいい天気ですね。')
    assert ja_sents2 == ["先生、今日はいい天気ですね。"]

    ch_full, ja_full, rem = parser.finalize()
    assert ch_full == "你好"
    assert ja_full == "こんにちは、先生、今日はいい天気ですね。"
    assert rem == []


def test_no_emission_before_a_sentence_is_confirmed():
    """A partial sentence without terminal punctuation must not be emitted early."""
    parser = StreamingBilingualParser()
    _, sentences = parser.feed_chunk('{"chinese": "你好", "japanese": "まだ途中')
    assert sentences == [], f"emitted an unconfirmed sentence: {sentences!r}"


def test_emission_after_stream_returns_full_sentence_set():
    """The concatenation of all emitted sentences equals the final Japanese text."""
    japanese = "そうですね、私もそう思いますよ。でも、少しだけ不安です。"
    payload = json.dumps({"chinese": "是啊", "japanese": japanese}, ensure_ascii=False)

    parser = StreamingBilingualParser()
    emitted = []
    for char in payload:
        _, sentences = parser.feed_chunk(char)
        emitted.extend(sentences)
    _, final_ja, tail = parser.finalize()
    emitted.extend(tail)

    assert final_ja == japanese
    assert "".join(emitted) == japanese
    assert len(emitted) == len(set(emitted))


LINES = [
    {"index": 900, "zh": '你好，"小猫"。\\路径\n下一句😀', "ja": 'こんにちは、猫です。次です。😀',
     "emotion": "shy", "act": {"face": "smile", "motion": "nod", "blush": False}},
    {"zh": "再见。", "ja": "さようなら。", "emotion": "cool", "act": {"fx": None}},
]
ZH = "".join(line["zh"] for line in LINES)
JA = "".join(line["ja"] for line in LINES)


def reply(lines=LINES, *, ascii=False):
    return json.dumps({"tts": {"speed": 1.05, "temp": 0.95, "emotion": "gentle"}, "lines": lines}, ensure_ascii=ascii)


def test_multibyte_transport_boundaries_and_no_partial_line_deltas():
    raw = reply().encode("utf-8")
    for split in range(len(raw) + 1):
        parser = StreamingBilingualParser()
        decoder = codecs.getincrementaldecoder("utf-8")()
        deltas = []
        for chunk in (raw[:split], raw[split:]):
            delta, sentences = parser.feed_chunk(decoder.decode(chunk))
            deltas.append(delta)
            assert sentences == []
            assert parser.chinese_extracted in ("", LINES[0]["zh"], ZH)
        assert parser.finalize() == (ZH, JA, [])
        assert "".join(deltas) == ZH


def test_truncation_at_every_character_retains_only_closed_lines():
    first = json.dumps(LINES[0], ensure_ascii=False)
    second = json.dumps(LINES[1], ensure_ascii=False)
    raw = '{"lines":[' + first + ',' + second + ']}'
    ends = [len('{"lines":[' + first), len(raw) - 2]
    for end in range(len('{"lines"'), len(raw) + 1):
        parser = StreamingBilingualParser()
        shown = ""
        for char in raw[:end]:
            delta, sentences = parser.feed_chunk(char)
            shown += delta
            assert sentences == []
        expected = LINES[:sum(end >= boundary for boundary in ends)]
        zh, ja, remaining = parser.finalize()
        assert (zh, ja, remaining) == ("".join(x["zh"] for x in expected), "".join(x["ja"] for x in expected), [])
        assert shown == zh
        assert parser.lines_truncated == (end < len(raw))


@pytest.mark.parametrize("lines", [LINES, [LINES[0]], [{"zh": "只有中文。", "ja": ""}],
                                      [{"zh": "", "ja": "日本語だけ。"}], []])
def test_finalize_aggregates_without_fabricating_a_missing_language(lines):
    parser = StreamingBilingualParser()
    parser.feed_chunk("前言\n```json\n" + reply(lines) + "\n```尾声")
    zh, ja, _ = parser.finalize()
    assert (zh, ja) == ("".join(x["zh"] for x in lines), "".join(x["ja"] for x in lines))
    assert len(parser.normalized_lines()) == len(lines)


def test_parser_escaped_quotes_and_newlines():
    parser = StreamingBilingualParser()
    chunks = [
        '{"chinese": "她说：\\"你好！\\"\\n新行。", ',
        '"japanese": "彼女は「こんにちは！」と言った。\\n新しい行。"}'
    ]
    for c in chunks:
        parser.feed_chunk(c)
    ch, ja, _ = parser.finalize()
    assert '她说："你好！"' in ch
    assert "\n新行。" in ch
    assert "彼女は「こんにちは！」と言った。" in ja


def test_empty_and_corrupt_json_payloads():
    parser = StreamingBilingualParser()
    parser.feed_chunk("{")
    ch, ja, _ = parser.finalize()
    assert isinstance(ch, str)
    assert isinstance(ja, str)


def test_parser_unclosed_trailing_backslash():
    """Trailing backslashes and partial escape sequences must not crash parser."""
    parser = StreamingBilingualParser()
    parser.feed_chunk('{"chinese": "测试\\')
    ch, _ = parser.feed_chunk('u4f60好", "japanese": "テスト\\')
    _, _ = parser.feed_chunk('"}\\\\')
    ch_full, ja_full, _ = parser.finalize()
    assert "测试" in ch_full
    assert "テスト" in ja_full
