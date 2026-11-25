"""The fast first TTS chunk must not be cut at the comma inside a number.

"1,000円" used to be voiced as two clips ("…1," and "000円です。") when the model answered
in the legacy {"chinese","japanese"} JSON or plain text instead of per-line output.
"""
import json

import pytest

from galgame2voice.services.streaming_parser import StreamingBilingualParser
from galgame2voice.utils.text_splitter import split_japanese_sentences


@pytest.mark.parametrize("ja", [
    "今月の家賃は1,000円です。",
    "全部で12,800円になります。",
    "お会計は１，５００円です。",
])
def test_first_chunk_never_splits_inside_a_number(ja):
    chunks = split_japanese_sentences(ja, is_first_chunk=True)
    assert chunks == [ja], chunks


def test_streamed_legacy_reply_sends_whole_price_to_tts():
    reply = json.dumps({"chinese": "这个月房租是一千日元。", "japanese": "今月の家賃は1,000円です。",
                        "emotion": "gentle"}, ensure_ascii=False)
    parser = StreamingBilingualParser()
    sentences = []
    for i in range(0, len(reply), 3):
        sentences += parser.feed_chunk(reply[i:i + 3])[1]
    sentences += parser.finalize()[2]
    assert sentences == ["今月の家賃は1,000円です。"], sentences


def test_comma_after_a_word_still_ends_the_first_chunk():
    text = "そういえば、明日の予定はどうなってる？"
    assert split_japanese_sentences(text, is_first_chunk=True) == ["そういえば、", "明日の予定はどうなってる？"]


def test_comma_between_a_number_and_a_word_still_splits():
    text = "部屋は全部で3つ、それぞれ広さが違うんですよ。"
    chunks = split_japanese_sentences(text, is_first_chunk=True)
    assert chunks[0] == "部屋は全部で3つ、"
