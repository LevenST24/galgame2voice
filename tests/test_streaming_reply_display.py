"""
The streamed reply must match the reply that is saved.

The web client builds the chat bubble (and its local history) from the
``delta_chinese`` events alone, while the database stores the parser's final
text. These tests stream replies through the same pipeline the chat stream
uses, in every chunk size, and compare the two.
"""

import json

import pytest

from galgame2voice.services.chat_pipelines.text_pipeline import TextSegmentationPipeline

REPLY_JSON = json.dumps(
    {
        "tts": {"speed": 1.0, "temp": 0.9, "emotion": "happy"},
        "chinese": "你好呀，今天过得怎么样？",
        "japanese": "こんにちは。今日はどうだった？",
    },
    ensure_ascii=False,
)
CHUNK_SIZES = (1, 2, 3, 5, 8, 13)


def _stream(text: str, chunk_size: int):
    """Returns (shown, saved_chinese, saved_japanese, voiced_sentences)."""
    pipe = TextSegmentationPipeline()
    shown = ""
    voiced: list[str] = []
    for i in range(0, len(text), chunk_size):
        delta, sentences, _ = pipe.feed_token(text[i:i + chunk_size])
        shown += delta
        voiced += sentences
    chinese, japanese, rest, tail, _ = pipe.finalize()
    return shown + rest, chinese, japanese, voiced + tail


@pytest.mark.parametrize("chunk_size", CHUNK_SIZES)
def test_plain_text_reply_is_streamed_and_saved_in_full(chunk_size):
    reply = "你好呀！今天过得怎么样？我一直在等你回来呢，快来陪我聊聊天吧。"
    shown, chinese, _, voiced = _stream(reply, chunk_size)
    assert chinese == reply
    assert shown == reply
    assert "".join(voiced) == reply


@pytest.mark.parametrize("chunk_size", CHUNK_SIZES)
def test_short_preamble_before_json_never_shows_raw_json(chunk_size):
    shown, chinese, japanese, _ = _stream("好的～\n```json\n" + REPLY_JSON + "\n```", chunk_size)
    assert chinese == "你好呀，今天过得怎么样？"
    assert japanese == "こんにちは。今日はどうだった？"
    assert shown == chinese


@pytest.mark.parametrize("chunk_size", CHUNK_SIZES)
def test_long_preamble_is_followed_by_the_whole_reply(chunk_size):
    preamble = "好的，我会按照你要求的格式来回复："
    shown, chinese, _, _ = _stream(preamble + "\n" + REPLY_JSON, chunk_size)
    assert chinese == "你好呀，今天过得怎么样？"
    assert shown == chinese
    assert preamble not in shown
    assert "{" not in shown


@pytest.mark.parametrize("chunk_size", CHUNK_SIZES)
@pytest.mark.parametrize(
    "reply",
    [
        "中文：你好呀，今天过得怎么样？\n日文：こんにちは、今日はどうだった？",
        "Chinese: 你好呀，今天过得怎么样？\nJapanese: こんにちは、今日はどうだった？",
    ],
)
def test_japanese_label_does_not_leak_into_the_chinese_bubble(reply, chunk_size):
    shown, chinese, japanese, voiced = _stream(reply, chunk_size)
    assert chinese == "你好呀，今天过得怎么样？"
    assert shown == chinese
    assert japanese == "こんにちは、今日はどうだった？"
    assert "".join(voiced) == japanese


@pytest.mark.parametrize("chunk_size", CHUNK_SIZES)
def test_chinese_ending_like_a_label_is_still_shown(chunk_size):
    reply = "中文：我们约好下个星期日"
    shown, chinese, _, _ = _stream(reply, chunk_size)
    assert chinese == "我们约好下个星期日"
    assert shown == chinese


@pytest.mark.parametrize("chunk_size", CHUNK_SIZES)
def test_bracketed_japanese_in_plain_text_is_voiced_not_shown(chunk_size):
    reply = "你好呀，今天天气真好【こんにちは、いい天気ですね。】我们出去玩吧，好不好呀？"
    shown, chinese, japanese, _ = _stream(reply, chunk_size)
    assert chinese == "你好呀，今天天气真好我们出去玩吧，好不好呀？"
    assert shown == chinese
    assert japanese == "こんにちは、いい天気ですね。"


@pytest.mark.parametrize("chunk_size", CHUNK_SIZES)
def test_plain_text_with_a_brace_is_kept_whole(chunk_size):
    reply = "我今天学了一个新的颜文字{^_^}，送给你，希望你每天都开心快乐哦！"
    shown, chinese, _, _ = _stream(reply, chunk_size)
    assert chinese == reply
    assert shown == reply


def test_null_chinese_field_does_not_break_finalize():
    shown, chinese, japanese, voiced = _stream('{"chinese": null, "japanese": "こんにちは。"}', 4)
    assert chinese == ""
    assert shown == ""
    assert japanese == "こんにちは。"
    assert voiced == ["こんにちは。"]


@pytest.mark.parametrize("chunk_size", CHUNK_SIZES)
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('{"chinese": "", "japanese": ""}', ("", "")),
        ("就是一句普通的中文回复，没有按格式输出。", ("就是一句普通的中文回复，没有按格式输出。",) * 2),
        (REPLY_JSON, ("你好呀，今天过得怎么样？", "こんにちは。今日はどうだった？")),
    ],
)
def test_streamed_reply_never_shows_or_saves_raw_json(raw, expected, chunk_size):
    shown, chinese, japanese, _ = _stream(raw, chunk_size)
    assert (chinese, japanese) == expected
    assert shown == chinese


LINES = [
    {"index": 900, "zh": '你好，"小猫"。\\路径\n下一句😀', "ja": 'こんにちは、猫です。次です。😀',
     "emotion": "shy", "act": {"face": "smile", "motion": "nod", "blush": False}},
    {"zh": "再见。", "ja": "さようなら。", "emotion": "cool", "act": {"fx": None}},
]
ZH = "".join(line["zh"] for line in LINES)
JA = "".join(line["ja"] for line in LINES)


def reply(lines=LINES, *, ascii=False):
    return json.dumps({"tts": {"speed": 1.05, "temp": 0.95, "emotion": "gentle"}, "lines": lines}, ensure_ascii=ascii)


@pytest.mark.parametrize("ascii", [False, True])
@pytest.mark.parametrize(("prefix", "suffix"), [
    ("", ""), ("```json\n", "\n```"), ("好的～\n", ""), ("说明\n```json\n", "\n```\n尾声不属于回复"),
])
def test_side_channel_at_every_split(ascii, prefix, suffix):
    raw = prefix + reply(ascii=ascii) + suffix
    for split in range(len(raw) + 1):
        pipe = TextSegmentationPipeline()
        shown = ""
        for token in (raw[:split], raw[split:]):
            delta, sentences, _ = pipe.feed_token(token)
            shown += delta
            assert sentences == []
        zh, ja, rest, sentences, _ = pipe.finalize()
        assert (zh, ja, shown + rest, sentences) == (ZH, JA, ZH, [])
        assert [line["index"] for line in pipe.parser.normalized_lines()] == [0, 1]
        assert pipe.parser.normalized_lines()[0]["act"] == LINES[0]["act"]


@pytest.mark.parametrize("chunk_size", CHUNK_SIZES)
@pytest.mark.parametrize(
    "raw",
    [
        "给你一段代码：\n```python\nd = {\"name\": \"test\"}\nprint(d)\n```\n试试看吧！",
        "配置写成 {\"host\": \"127.0.0.1\", \"port\": 8080}，改完保存即可。",
        "{\"host\": \"127.0.0.1\", \"port\": 8080}",
    ],
)
def test_plain_text_with_embedded_non_reply_json_is_preserved(raw, chunk_size):
    shown, chinese, japanese, _ = _stream(raw, chunk_size)
    assert chinese
    assert shown == chinese
    assert japanese == chinese
    assert "test" in chinese or "8080" in chinese
