"""A lines-mode reply that writes its emotion in Chinese keeps it (生气 -> angry),
matching the legacy parser, instead of falling back to the keyword scan."""
import json

import pytest

from galgame2voice.services.chat_pipelines.line_scanner import validate_line
from galgame2voice.services.streaming_parser import StreamingBilingualParser


@pytest.mark.parametrize(
    ("label", "expected"),
    [("生气", "angry"), ("开心", "happy"), ("害羞", "shy"), ("傲娇", "tsundere"), (" 难过 ", "sad")],
)
def test_validate_line_maps_emotion_aliases(label, expected):
    line, issues = validate_line({"zh": "你到底去哪了？", "ja": "どこに行ってたの？", "emotion": label})
    assert line is not None
    assert line.emotion == expected
    assert not any("emotion" in issue for issue in issues)


def test_streamed_line_keeps_chinese_emotion():
    parser = StreamingBilingualParser()
    reply = {"lines": [{"zh": "你到底去哪了？", "ja": "どこに行ってたの？", "emotion": "生气"}]}
    parser.feed_chunk(json.dumps(reply, ensure_ascii=False))
    parser.finalize()
    assert parser.normalized_lines()[0]["emotion"] == "angry"


def test_unknown_emotion_is_still_rejected():
    line, issues = validate_line({"zh": "测试", "emotion": "unknown_emo"})
    assert line is not None
    assert line.emotion is None
    assert any("emotion" in issue for issue in issues)
