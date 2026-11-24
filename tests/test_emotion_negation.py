"""The keyword fallback must not read a negated or flipped keyword as that emotion.
Its result picks the line's reference audio, sprite face and prosody."""
import json

import pytest

from galgame2voice.services.emotion_classifier import classify_emotion, detect_emotion_cue
from galgame2voice.services.streaming_parser import StreamingBilingualParser


@pytest.mark.parametrize(
    "zh, ja",
    [
        ("我一点也不开心。", "全然嬉しくない。"),
        ("你这样我很不高兴！", ""),
        ("我不太开心。", ""),
        ("", "嬉しくない。"),
    ],
)
def test_negated_happy_is_not_happy(zh, ja):
    assert classify_emotion(zh, ja) != "happy"


@pytest.mark.parametrize("zh", ["她苦笑了一下。", "他冷笑着说：滚。", "别嘲笑我。"])
def test_bitter_or_cold_smile_is_not_happy(zh):
    assert classify_emotion(zh, "") != "happy"


def test_cold_smile_reads_as_cool():
    assert classify_emotion("他冷笑着说。", "") == "cool"


def test_gentle_smile_is_gentle():
    assert classify_emotion("她微笑着看你。", "") == "gentle"


@pytest.mark.parametrize("zh", ["别难过了。", "我不难过。", "别生气嘛。"])
def test_comforting_lines_are_not_read_as_the_negated_emotion(zh):
    assert classify_emotion(zh, "") not in {"sad", "angry"}


def test_unhappy_reads_as_sad():
    assert classify_emotion("我好不开心。", "") == "sad"


@pytest.mark.parametrize(
    "zh, expected",
    [("太好了，我好开心！", "happy"), ("哈哈，真好笑。", "happy"), ("我好难过。", "sad"), ("我生气了！", "angry")],
)
def test_plain_keywords_still_match(zh, expected):
    assert classify_emotion(zh, "") == expected


def test_keyword_counts_when_one_occurrence_is_affirmed():
    assert detect_emotion_cue("不是不开心，是真的开心。") is not None
    assert detect_emotion_cue("别哭，我很开心。") == "happy"


def test_streamed_line_without_emotion_is_not_happy_when_negated():
    parser = StreamingBilingualParser()
    reply = {"lines": [{"zh": "我一点也不高兴。", "ja": "全然嬉しくない。"}]}
    parser.feed_chunk(json.dumps(reply, ensure_ascii=False))
    parser.finalize()
    assert parser.normalized_lines()[0]["emotion"] != "happy"
