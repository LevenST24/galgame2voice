"""A plain-text reply (the model ignored the JSON format) never voices its 【emotion】 tag."""

import pytest

from galgame2voice.services.streaming_parser import StreamingBilingualParser


def _feed(text: str) -> tuple[str, str, list[str], str | None]:
    parser = StreamingBilingualParser()
    spoken: list[str] = []
    for char in text:
        _, sentences = parser.feed_chunk(char)
        spoken += sentences
    chinese, japanese, rest = parser.finalize()
    return chinese, japanese, spoken + rest, parser.emotion_extracted


@pytest.mark.parametrize("text, tag", [
    ("【难过】呜呜，怎么会这样……我明明那么努力了。", "难过"),
    ("【ツンデレ】べ、別にアンタのためじゃないわよ！勘違いしないでよね。", "ツンデレ"),
    ("【开心】こんにちは！今日はいい天気ですね。", "开心"),
])
def test_a_leading_emotion_tag_is_not_the_voice_line(text, tag):
    _, japanese, spoken, _ = _feed(text)
    assert japanese != tag
    assert tag not in "".join(spoken)


def test_a_japanese_reply_after_a_tag_is_voiced():
    _, japanese, spoken, emotion = _feed("【开心】こんにちは！今日はいい天気ですね。")
    assert "こんにちは" in japanese and "今日はいい天気" in "".join(spoken)
    assert emotion == "happy"


def test_every_japanese_block_is_voiced_in_order():
    chinese, japanese, spoken, _ = _feed("你好呀！【こんにちは！】今天天气真好。【今日はいい天気。】")
    assert "".join(spoken).index("こんにちは") < "".join(spoken).index("今日はいい天気")
    assert "【" not in chinese and "你好呀" in chinese
