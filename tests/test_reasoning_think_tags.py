import pytest

from galgame2voice.services.chat_pipelines.line_scanner import LineScanner
from galgame2voice.services import streaming_parser
from galgame2voice.services.streaming_parser import StreamingBilingualParser
from galgame2voice.services.chat_pipelines.tts_pipeline import TtsStreamPipeline
from galgame2voice.utils.japanese_phonetics import (
    clean_japanese_parentheses,
    normalize_japanese_for_tts,
    strip_reasoning_tags,
)


_REAL_LINES = '{"lines": [{"zh": "正式台词", "ja": "本番のセリフ", "emotion": "happy"}]}'


def _parse_reply(raw_text):
    parser = StreamingBilingualParser()
    parser.feed_chunk(raw_text)
    zh, ja, _ = parser.finalize()
    return zh, ja, parser


def _stream_reply(*chunks):
    parser = StreamingBilingualParser()
    deltas = []
    for chunk in chunks:
        delta, _ = parser.feed_chunk(chunk)
        if delta:
            deltas.append(delta)
    zh, ja, _ = parser.finalize()
    return "".join(deltas), zh, ja, parser


def _scan_reply(*chunks):
    scanner = LineScanner()
    for chunk in chunks:
        scanner.feed(chunk)
    return scanner.finish()


def test_line_scanner_ignores_draft_json_in_think_block():
    text = (
        '<think>Let me format {"lines": [{"zh": "草稿", "ja": "下書き"}]}</think>\n'
        '{"lines": [{"zh": "正式台词", "ja": "本番のセリフ", "emotion": "happy"}]}'
    )
    scanner = LineScanner()
    lines = scanner.feed(text)
    assert len(lines) == 1
    scanned = scanner.finish()

    assert scanned.format == "lines"
    assert scanned.truncated is False
    assert len(scanned.lines) == 1
    assert scanned.lines[0].zh == "正式台词"
    assert scanned.lines[0].ja == "本番のセリフ"
    assert scanned.lines[0].emotion == "happy"


def test_line_scanner_streaming_with_think_block():
    scanned = _scan_reply(
        "<thi",
        'nk>Let me format {"lines": [{"zh": "草稿"}]}',
        "</think>\n",
        _REAL_LINES,
    )

    assert scanned.format == "lines"
    assert len(scanned.lines) == 1
    assert scanned.lines[0].zh == "正式台词"
    assert scanned.lines[0].ja == "本番のセリフ"


def test_streaming_parser_lines_with_think_draft():
    raw_text = (
        '<think>Let me format {"lines": [{"zh": "草稿", "ja": "下書き"}]}</think>\n'
        '{"lines": [{"zh": "正式台词", "ja": "本番のセリフ", "emotion": "happy"}]}'
    )
    zh, ja, parser = _parse_reply(raw_text)
    assert zh == "正式台词"
    assert ja == "本番のセリフ"
    assert parser.line_scanner.format == "lines"
    lines = parser.normalized_lines()
    assert len(lines) == 1
    assert lines[0]["zh"] == "正式台词"
    assert lines[0]["ja"] == "本番のセリフ"


def test_streaming_parser_streaming_lines_with_think_draft():
    emitted_text, zh, ja, _ = _stream_reply(
        "<think>Let me format ",
        '{"lines": [{"zh": "草稿", "ja": "下書き"}]}',
        "</think>\n",
        _REAL_LINES,
    )

    assert "<think>" not in emitted_text
    assert "草稿" not in emitted_text
    assert "正式台词" in emitted_text
    assert zh == "正式台词"
    assert ja == "本番のセリフ"


def test_streaming_parser_prose_with_think_brackets():
    raw_text = "<think>先想一下怎么回答【テスト】</think>笨蛋，别突然凑过来啦！【バカ、急に近づかないでよ！】"
    zh, ja, parser = _parse_reply(raw_text)

    assert "<think>" not in zh
    assert "先想一下" not in zh
    assert "テスト" not in zh
    assert zh == "笨蛋，别突然凑过来啦！"
    assert ja == "バカ、急に近づかないでよ！"


def test_streaming_parser_streaming_prose_with_think_brackets():
    emitted_text, zh, ja, _ = _stream_reply(
        "<think>先想一下",
        "怎么回答【テスト】",
        "</think>",
        "笨蛋，别突然凑过来啦！真是拿你没办法呢！",
        "【バカ、急に近づかないでよ！】",
    )

    assert "<think>" not in emitted_text
    assert "先想一下" not in emitted_text
    assert "テスト" not in emitted_text
    assert "笨蛋，别突然凑过来啦！" in emitted_text
    assert "<think>" not in zh
    assert "先想一下" not in zh
    assert zh == "笨蛋，别突然凑过来啦！真是拿你没办法呢！"
    assert ja == "バカ、急に近づかないでよ！"


def test_unclosed_think_tag_truncation():
    raw_text = "<think>还在思考中，尚未得出结论..."
    parser = StreamingBilingualParser()
    delta, sentences = parser.feed_chunk(raw_text)
    zh, ja, remaining = parser.finalize()

    assert delta == ""
    assert zh == ""
    assert ja == ""
    assert remaining == []
    assert parser.lines_truncated is True


def test_japanese_phonetics_strip_reasoning_tags():
    assert strip_reasoning_tags("<think>思考中</think>こんにちは") == "こんにちは"
    assert strip_reasoning_tags("<thought>どうしよう…</thought>おはよう！") == "おはよう！"
    assert strip_reasoning_tags("<reasoning>reasoning block</reasoning>ありがとう") == "ありがとう"
    assert strip_reasoning_tags("<think>未闭合の思考内容") == ""
    assert strip_reasoning_tags("<think class='test'>属性付き</think>テスト") == "テスト"


def test_normalize_japanese_for_tts_with_think_tags():
    assert normalize_japanese_for_tts("<think>思考中</think>こんにちは") == "こんにちは"
    assert normalize_japanese_for_tts("<thought>どうしよう…</thought>（微笑んで）おはよう！") == "おはよう！"
    assert normalize_japanese_for_tts("<reasoning>reasoning block</reasoning>ありがとう") == "ありがとう"
    assert normalize_japanese_for_tts("<think>未闭合の思考内容") == ""


def test_is_vocal_sentence_with_think_tags():
    assert TtsStreamPipeline.is_vocal_sentence("<think>Let me think about how to respond politely.</think>") is False
    assert TtsStreamPipeline.is_vocal_sentence("<thought>どう返答すべきか…</thought>") is False
    assert TtsStreamPipeline.is_vocal_sentence("<think>思考中ですが文字数制限で途切れた") is False
    assert TtsStreamPipeline.is_vocal_sentence("<think>思考中</think>こんにちは") is True


def test_various_think_tags_case_and_attributes():
    cases = [
        '<THINK>All caps thought</THINK>{"lines": [{"zh": "大写测试", "ja": "大文字"}]}',
        '<thought id="t1" class="reasoning">Thought with attributes</thought>{"lines": [{"zh": "属性测试", "ja": "属性"}]}',
        '<Reasoning>Mixed case</Reasoning>{"lines": [{"zh": "混写测试", "ja": "ミックス"}]}',
        '<think></think>{"lines": [{"zh": "空标签测试", "ja": "空"}]}',
        '<think>one</think>\n<think>two</think>\n{"lines": [{"zh": "多思考块", "ja": "複数"}]}',
    ]
    for text in cases:
        zh, ja, parser = _parse_reply(text)
        assert zh != ""
        assert ja != ""
        assert "<think>" not in zh.lower()
        assert "<thought>" not in zh.lower()
        assert "<reasoning>" not in zh.lower()
        assert parser.line_scanner.format == "lines"
        assert len(parser.normalized_lines()) == 1


def test_single_language_fallback_with_think():
    text = "<think>\n考え中：こんにちはと言おう\n</think>\nこんにちは！"
    zh, ja, parser = _parse_reply(text)
    assert "<think>" not in zh
    assert "考え中" not in zh
    assert zh == "こんにちは！"
    assert ja == "こんにちは！"


def test_closing_tag_with_whitespace_and_prose():
    raw_text = "<think>思考中...</think >笨蛋，别凑过来！【バカ、近づかないで！】"
    zh, ja, parser = _parse_reply(raw_text)
    assert zh == "笨蛋，别凑过来！"
    assert ja == "バカ、近づかないで！"
    assert parser.lines_truncated is False

    # Also test with newline in closing tag
    raw_text_nl = "<think>思考中...</think\n>笨蛋，别凑过来！【バカ、近づかないで！】"
    zh_nl, ja_nl, parser_nl = _parse_reply(raw_text_nl)
    assert zh_nl == "笨蛋，别凑过来！"
    assert ja_nl == "バカ、近づかないで！"
    assert parser_nl.lines_truncated is False


def test_line_scanner_long_attributes_exceeding_64_chars():
    long_attr_tag = "<think model='deepseek-r1-distill-qwen-32b-custom-very-long-attribute-name-here-1234567890'>"
    res = _scan_reply(
        f'{long_attr_tag}Let me draft {{"lines": [{{"zh": "草稿", "ja": "下書き"}}]}}</think>\n'
        f'{_REAL_LINES}'
    )
    assert res.format == "lines"
    assert len(res.lines) == 1
    assert res.lines[0].zh == "正式台词"
    assert res.lines[0].ja == "本番のセリフ"
    assert res.truncated is False


def test_nested_thinking_tags_in_scanner_and_parser():
    text = (
        '<think>Outer thought <thought>nested thought with {"lines": [{"zh": "draft1"}]}</thought> '
        'more outer thought with {"lines": [{"zh": "draft2"}]}</think>\n'
        '{"lines": [{"zh": "真正的台词", "ja": "本当のセリフ", "emotion": "gentle"}]}'
    )
    res = _scan_reply(text)
    assert res.format == "lines"
    assert len(res.lines) == 1
    assert res.lines[0].zh == "真正的台词"
    assert res.lines[0].ja == "本当のセリフ"

    # Nested in prose parser
    prose = "<think>外层 <thought>内层</thought> 外层结束</think>你好呀！【こんにちは！】"
    zh, ja, parser = _parse_reply(prose)
    assert zh == "你好呀！"
    assert ja == "こんにちは！"
    assert parser.lines_truncated is False


def test_unclosed_opening_tag_without_closing_angle_bracket():
    for tag in ("<think", "<thought", "<reasoning", "<think attr='abc'", "<thi"):
        assert strip_reasoning_tags(tag) == ""
        assert normalize_japanese_for_tts(tag) == ""
        assert TtsStreamPipeline.is_vocal_sentence(tag) is False


def test_orphaned_closing_tag_handling():
    assert strip_reasoning_tags("</think>こんにちは") == "こんにちは"
    assert strip_reasoning_tags("</thought >おはよう") == "おはよう"
    assert normalize_japanese_for_tts("</think>こんにちは") == "こんにちは"

    zh, ja, parser = _parse_reply("</think>你好【こんにちは】")
    assert zh == "你好"
    assert ja == "こんにちは"
    assert parser.lines_truncated is False


def test_closing_tag_with_whitespace_normalize_and_vocal():
    assert clean_japanese_parentheses("<think>foo</think >こんにちは") == "こんにちは"
    assert normalize_japanese_for_tts("<think>foo</think >こんにちは") == "こんにちは"
    assert normalize_japanese_for_tts("<think>foo</think\n>こんにちは") == "こんにちは"
    assert TtsStreamPipeline.is_vocal_sentence("<think>foo</think >こんにちは") is True


def test_self_closing_think_tags_in_scanner_and_parser():
    variants = [
        '<think/>{"lines": [{"zh": "你好", "ja": "こんにちは"}]}',
        '<think />{"lines": [{"zh": "你好", "ja": "こんにちは"}]}',
        '<thought id="empty"/>{"lines": [{"zh": "你好", "ja": "こんにちは"}]}',
        '<reasoning />{"lines": [{"zh": "你好", "ja": "こんにちは"}]}',
    ]
    for text in variants:
        scanned = _scan_reply(text)
        assert scanned.format == "lines"
        assert scanned.truncated is False
        assert len(scanned.lines) == 1
        assert scanned.lines[0].zh == "你好"
        assert scanned.lines[0].ja == "こんにちは"

        zh, ja, parser = _parse_reply(text)
        assert zh == "你好"
        assert ja == "こんにちは"
        assert parser.lines_truncated is False


def test_self_closing_nested_inside_think_block():
    text = (
        '<think>外层思考开始 <thought id="t1" /> 外层思考结束</think>\n'
        '{"lines": [{"zh": "正式台词", "ja": "本番セリフ"}]}'
    )
    scanned = _scan_reply(text)
    assert scanned.format == "lines"
    assert scanned.truncated is False
    assert len(scanned.lines) == 1
    assert scanned.lines[0].zh == "正式台词"

    zh, ja, parser = _parse_reply(text)
    assert zh == "正式台词"
    assert parser.lines_truncated is False


def test_self_closing_tags_in_phonetics_and_prose():
    for tag in ("<think/>", "<think />", "<thought id='x'/>", "<reasoning />"):
        assert strip_reasoning_tags(f"{tag}こんにちは") == "こんにちは"
        assert normalize_japanese_for_tts(f"{tag}こんにちは") == "こんにちは"
        assert clean_japanese_parentheses(f"{tag}こんにちは") == "こんにちは"
        assert TtsStreamPipeline.is_vocal_sentence(tag) is False
        assert TtsStreamPipeline.is_vocal_sentence(f"{tag}こんにちは") is True

    zh, ja, parser = _parse_reply("<think/>笨蛋！【バカ！】")
    assert zh == "笨蛋！"
    assert ja == "バカ！"
    assert parser.lines_truncated is False


def test_non_tag_trailing_angle_bracket():
    assert strip_reasoning_tags("温度 <") == "温度 <"
    assert strip_reasoning_tags("1 < 2") == "1 < 2"

    zh, ja, parser = _parse_reply("温度 < 30℃")
    assert "30℃" in zh
    assert parser.lines_truncated is False


def test_line_scanner_ignores_draft_json_in_thinking_tag_split_across_chunks():
    scanned = _scan_reply("<thinki", 'ng>Let me format {"lines": [{"zh": "草稿"}]}', "</thinki", "ng>\n", _REAL_LINES)

    assert scanned.truncated is False
    assert [line.zh for line in scanned.lines] == ["正式台词"]


def test_streaming_parser_strips_thinking_tag_from_deltas_and_result():
    emitted, zh, ja, parser = _stream_reply(
        "<thinking>Let me format ", '{"lines": [{"zh": "草稿", "ja": "下書き"}]}', "</thinking>\n", _REAL_LINES
    )

    assert "草稿" not in emitted and "thinking" not in emitted
    assert zh == "正式台词"
    assert ja == "本番のセリフ"
    assert [line["zh"] for line in parser.normalized_lines()] == ["正式台词"]


def test_strip_reasoning_tags_removes_thinking_block():
    assert strip_reasoning_tags("<thinking>草稿</thinking>你好").strip() == "你好"
    assert strip_reasoning_tags("<Thinking mode='x'>草稿</THINKING>你好").strip() == "你好"
    assert strip_reasoning_tags("<thinking>未闭合的思考内容") == ""
    assert strip_reasoning_tags("你好<thinking>草稿</thinking>世界") == "你好世界"
    assert strip_reasoning_tags("<thinking/>你好") == "你好"
    assert strip_reasoning_tags("<thinking>只有思考</thinking>") == ""


def test_long_think_block_is_not_rescanned_on_every_chunk(monkeypatch):
    """A model that streams a long <think> block before its reply used to make feed_chunk rescan the whole
    buffer on every chunk, so parsing time grew with the square of the thinking length."""
    scanned = []
    real = streaming_parser._RE_THINK_BLOCK_UNCLOSED

    class CountingPattern:
        def search(self, text):
            scanned.append(len(text))
            return real.search(text)

    monkeypatch.setattr(streaming_parser, "_RE_THINK_BLOCK_UNCLOSED", CountingPattern())
    parser = StreamingBilingualParser()
    parser.feed_chunk("<think>")
    for i in range(1500):
        parser.feed_chunk(f"第{i}步，先想一想。")
    parser.feed_chunk("</think>")
    parser.feed_chunk('{"lines":[{"zh":"你好","ja":"こんにちは","emotion":"happy"}]}')

    assert len(scanned) < 10
    assert parser.chinese_extracted == "你好"


@pytest.mark.parametrize(
    "text",
    [
        '<think>a > b 的时候</think>{"zh":"好的","ja":"はい"}',
        "<think>先想想</thought>还在想</think>你好",
        "<THINK>草稿</Think>后面的话",
        "<<think>x</think>",
        '<think a=">">内容</think>正文',
        "前面的话<think>没有闭合",
    ],
)
def test_think_block_shortcut_gives_the_same_output_as_rescanning(monkeypatch, text):
    def feed(step):
        parser = StreamingBilingualParser()
        return [parser.feed_chunk(text[i : i + step]) for i in range(0, len(text), step)], parser.finalize()

    real = streaming_parser._strip_think_buffer

    def never_frozen(buf, is_final=False):
        effective, inside, _ = real(buf, is_final)
        return effective, inside, False

    for step in (1, 2, 3, 7):
        shortcut = feed(step)
        with monkeypatch.context() as m:
            m.setattr(streaming_parser, "_strip_think_buffer", never_frozen)
            rescanned = feed(step)
        assert shortcut == rescanned, step
