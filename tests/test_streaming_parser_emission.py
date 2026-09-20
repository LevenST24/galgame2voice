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

import json
import re

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
