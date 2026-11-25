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


@pytest.mark.parametrize("text, expected_zh, expected_ja", [
    ("今天真开心！【今日はとても楽しいですね！】", "今天真开心！", "今日はとても楽しいですね！"),
    ("你真温柔呢！【あなたは本当に優しいですね！】", "你真温柔呢！", "あなたは本当に優しいですね！"),
])
def test_japanese_block_containing_emotion_word_is_not_dropped(text, expected_zh, expected_ja):
    chinese, japanese, spoken, _ = _feed(text)
    assert chinese == expected_zh
    assert japanese == expected_ja
    assert expected_ja in "".join(spoken)


@pytest.mark.parametrize("text, expected_ja", [
    ("笨蛋(´・ω・｀)【バカ、近づかないで！】", "バカ、近づかないで！"),
    ("今天去メイド咖啡厅啦！【今日はメイドカフェに行ったよ！】", "今日はメイドカフェに行ったよ！"),
])
def test_middle_dot_or_loanword_in_chinese_does_not_drop_japanese_block(text, expected_ja):
    _, japanese, spoken, _ = _feed(text)
    assert japanese == expected_ja
    assert expected_ja in "".join(spoken)


@pytest.mark.parametrize("text, dropped_tag, expected_emo", [
    ("【傲娇】才不是呢！【ため息】", "ため息", "tsundere"),
    ("（害羞）那、那个……【ツンデレ】", "ツンデレ", "shy"),
    ("<think>思考</think>（害羞）呜呜，怎么会这样……我明明那么努力了。【ツンデレ】", "ツンデレ", "shy"),
])
def test_extra_kana_emotion_tag_is_never_voiced(text, dropped_tag, expected_emo):
    _, japanese, spoken, emotion = _feed(text)
    assert dropped_tag not in japanese
    assert dropped_tag not in "".join(spoken)
    assert emotion == expected_emo
