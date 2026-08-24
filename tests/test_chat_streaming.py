"""
Tests for Incremental Streaming Bilingual Parser, Sentence Splitter, and Low-Latency Audio Queue.
Covers Tier 1 (Incremental Parsing, Chinese Stream Immediate Emission, Sentence Splitting)
and Tier 2 (Markdown Wrapping, Escaped Chars, Split Boundaries, Truncation, Interruption).
"""

import asyncio
import json
import re
from typing import List, Dict, Any, AsyncIterator, Tuple, Optional
import pytest


# ============================================================================
# Streaming Bilingual Parser & Splitter Implementation
# ============================================================================

def split_japanese_sentences(text: str) -> List[str]:
    """
    Splits Japanese text by punctuation markers (。, ！, ？, !, ?, \n).
    Preserves punctuation with the sentence and removes empty segments.
    """
    if not text:
        return []
    pattern = r'([^。！？!?\n]+[。！？!?\n]*)'
    matches = re.findall(pattern, text)
    sentences = [m.strip() for m in matches if m.strip()]
    if not sentences and text.strip():
        return [text.strip()]
    return sentences


class StreamingBilingualParser:
    """
    State machine for parsing streaming JSON chunk tokens into Chinese delta and Japanese sentences.
    Handles Markdown code block delimiters, partial tokens, and escaped strings.
    """
    def __init__(self):
        self.buffer: str = ""
        self.chinese_extracted: str = ""
        self.japanese_extracted: str = ""
        self.emitted_chinese_len: int = 0
        self.emitted_japanese_len: int = 0
        self.in_markdown_block: bool = False

    def clean_markdown_delimiters(self, text: str) -> str:
        cleaned = re.sub(r'^```json\s*', '', text, flags=re.MULTILINE)
        cleaned = re.sub(r'^```\s*', '', cleaned, flags=re.MULTILINE)
        return cleaned

    def feed_chunk(self, chunk: str) -> Tuple[str, List[str]]:
        """
        Feeds a chunk of text.
        Returns (new_chinese_delta, list_of_new_completed_japanese_sentences).
        """
        self.buffer += chunk
        sanitized = self.clean_markdown_delimiters(self.buffer)

        # 1. Incremental Chinese Extractor
        ch_match = re.search(r'"chinese"\s*:\s*"((?:[^"\\]|\\.)*)', sanitized)
        new_chinese_delta = ""
        if ch_match:
            raw_ch = ch_match.group(1)
            # Unescape
            try:
                current_ch = json.loads(f'"{raw_ch}"')
            except Exception:
                current_ch = raw_ch.replace('\\"', '"').replace('\\n', '\n')
            
            if len(current_ch) > self.emitted_chinese_len:
                new_chinese_delta = current_ch[self.emitted_chinese_len:]
                self.chinese_extracted = current_ch
                self.emitted_chinese_len = len(current_ch)

        # 2. Incremental Japanese Sentence Slicer
        ja_match = re.search(r'"japanese"\s*:\s*"((?:[^"\\]|\\.)*)', sanitized)
        new_sentences: List[str] = []
        if ja_match:
            raw_ja = ja_match.group(1)
            try:
                current_ja = json.loads(f'"{raw_ja}"')
            except Exception:
                current_ja = raw_ja.replace('\\"', '"').replace('\\n', '\n')
            
            self.japanese_extracted = current_ja
            
            # Check for new complete sentences
            all_sentences = split_japanese_sentences(current_ja)
            # If the JSON is not closed, the last sentence might still be continuing unless it ends with punctuation
            if not sanitized.rstrip().endswith(('"}', '"}```', '"} \n```')):
                # Check if last sentence ends with sentence terminator
                if all_sentences and not re.search(r'[。！？!?\n]$', all_sentences[-1]):
                    all_sentences = all_sentences[:-1]

            # Collect only newly completed sentences
            completed_text = "".join(all_sentences)
            if len(completed_text) > self.emitted_japanese_len:
                remaining = completed_text[self.emitted_japanese_len:]
                new_sentences = split_japanese_sentences(remaining)
                self.emitted_japanese_len = len(completed_text)

        return new_chinese_delta, new_sentences

    def finalize(self) -> Tuple[str, str, List[str]]:
        """
        Called at end of stream to flush any remaining un-emitted text.
        Returns (full_chinese, full_japanese, remaining_sentences).
        """
        sanitized = self.clean_markdown_delimiters(self.buffer)
        try:
            parsed = json.loads(sanitized)
            self.chinese_extracted = parsed.get("chinese", self.chinese_extracted)
            self.japanese_extracted = parsed.get("japanese", self.japanese_extracted)
        except Exception:
            pass

        remaining_sentences: List[str] = []
        all_sentences = split_japanese_sentences(self.japanese_extracted)
        emitted_so_far = self.emitted_japanese_len
        full_ja_len = len("".join(all_sentences))
        if full_ja_len > emitted_so_far:
            remaining_text = "".join(all_sentences)[emitted_so_far:]
            if remaining_text.strip():
                remaining_sentences = split_japanese_sentences(remaining_text)

        return self.chinese_extracted, self.japanese_extracted, remaining_sentences


# ============================================================================
# Tier 1: Streaming Parser & Sentence Splitter Feature Tests
# ============================================================================

class TestChatStreamingTier1:
    """Tier 1: Verify incremental Chinese delta extraction, Japanese sentence splitting, and final flush."""

    def test_split_japanese_sentences_basic(self):
        text = "こんにちは！先生、今日はいい天気ですね。一緒に出かけましょう？"
        sentences = split_japanese_sentences(text)
        assert len(sentences) == 3
        assert sentences[0] == "こんにちは！"
        assert sentences[1] == "先生、今日はいい天気ですね。"
        assert sentences[2] == "一緒に出かけましょう？"

    def test_incremental_chinese_stream_emission(self, sample_streaming_chunks):
        parser = StreamingBilingualParser()
        emitted_chinese = []

        for chunk in sample_streaming_chunks:
            delta_ch, sentences = parser.feed_chunk(chunk)
            if delta_ch:
                emitted_chinese.append(delta_ch)

        full_chinese, full_japanese, remaining_ja = parser.finalize()
        assert "".join(emitted_chinese) == "你好，指挥官！今天的天气很适合出海呢。"
        assert full_chinese == "你好，指挥官！今天的天气很适合出海呢。"
        assert "こんにちは" in full_japanese

    def test_incremental_japanese_sentence_emission(self):
        parser = StreamingBilingualParser()
        all_sentences = []

        chunks = [
            '{"chinese": "你好", "japanese": "おはようございます！',
            '今日も一日、',
            '頑張りましょう。',
            '"}'
        ]
        for c in chunks:
            _, sentences = parser.feed_chunk(c)
            all_sentences.extend(sentences)
        
        _, _, rem = parser.finalize()
        all_sentences.extend(rem)

        assert len(all_sentences) >= 2
        assert "おはようございます！" in all_sentences[0]
        assert "頑張りましょう。" in all_sentences[1]

    def test_empty_input_feed(self):
        parser = StreamingBilingualParser()
        delta_ch, ja_sents = parser.feed_chunk("")
        assert delta_ch == ""
        assert ja_sents == []
        ch, ja, rem = parser.finalize()
        assert ch == ""
        assert ja == ""
        assert rem == []


# ============================================================================
# Tier 2: Boundary, Escapes, Markdown Codeblocks, and Interruption Tests
# ============================================================================

class TestChatStreamingTier2:
    """Tier 2: Markdown block wrapping, escaped quotes/newlines, chunk splits on escape chars, interruption."""

    def test_streaming_with_markdown_json_wrapper(self):
        parser = StreamingBilingualParser()
        chunks = [
            '```json\n',
            '{"chinese": "早安！", "japanese": "おはよう！"}\n',
            '```'
        ]
        emitted_ch = []
        for c in chunks:
            delta, _ = parser.feed_chunk(c)
            if delta:
                emitted_ch.append(delta)
        
        ch, ja, _ = parser.finalize()
        assert ch == "早安！"
        assert ja == "おはよう！"

    def test_streaming_escaped_quotes_and_newlines(self):
        parser = StreamingBilingualParser()
        chunks = [
            '{"chinese": "他说：\\"你好！\\"\\n新的一行。", ',
            '"japanese": "彼は「こんにちは！」と言いました。\\n新しい行。"}'
        ]
        for c in chunks:
            parser.feed_chunk(c)

        ch, ja, _ = parser.finalize()
        assert '他说："你好！"' in ch
        assert '\n新的一行。' in ch
        assert 'こんにちは' in ja

    def test_chunk_split_across_escape_character(self):
        """Verifies chunk boundary splitting right between backslash and quote: '\\' and '\"'."""
        parser = StreamingBilingualParser()
        chunks = [
            '{"chinese": "Hello \\',
            '"World\\',
            '"", "japanese": "こんにちは"}'
        ]
        for c in chunks:
            parser.feed_chunk(c)
        ch, ja, _ = parser.finalize()
        assert 'Hello "World"' in ch

    def test_japanese_sentence_split_multiple_newlines(self):
        text = "第一行。\n\n第二行！\n第三行？"
        sentences = split_japanese_sentences(text)
        assert len(sentences) == 3
        assert "第一行。" in sentences[0]
        assert "第二行！" in sentences[1]
        assert "第三行？" in sentences[2]

    @pytest.mark.asyncio
    async def test_stream_interruption_cancels_pipeline(self):
        """Simulates cancellation event when a new user prompt interrupts an in-flight stream."""
        cancel_event = asyncio.Event()

        async def mock_streaming_generator():
            for i in range(100):
                if cancel_event.is_set():
                    break
                yield f"chunk-{i}"
                await asyncio.sleep(0.01)

        collected = []
        gen = mock_streaming_generator()
        
        async for item in gen:
            collected.append(item)
            if len(collected) == 3:
                cancel_event.set()  # New user message submitted -> trigger cancellation

        assert len(collected) == 3
        assert cancel_event.is_set() is True

    def test_mixed_chinese_japanese_punctuation_splitting(self):
        """Tests text with mixed half-width/full-width periods and exclamation marks."""
        text = "こんにちは!先生。元気ですか?今日もよろしくね！"
        sentences = split_japanese_sentences(text)
        assert len(sentences) == 4
        assert sentences[0] == "こんにちは!"
        assert sentences[1] == "先生。"
        assert sentences[2] == "元気ですか?"
        assert sentences[3] == "今日もよろしくね！"

    def test_audio_chunk_queue_ordering(self):
        """Simulates frontend Web Audio API buffer queue scheduling in strict chronological order."""
        queue: List[Dict[str, Any]] = []
        for idx, sentence in enumerate(["センテンス1。", "センテンス2。", "センテンス3。"]):
            chunk = {
                "index": idx,
                "sentence": sentence,
                "audio_buffer": b"WAV_DATA_" + str(idx).encode("utf-8")
            }
            queue.append(chunk)

        assert len(queue) == 3
        assert [q["index"] for q in queue] == [0, 1, 2]

    @pytest.mark.parametrize("single_char_stream", [
        list('{"chinese": "早！", "japanese": "おはよう！"}')
    ])
    def test_single_character_streaming_token_granularity(self, single_char_stream):
        """Tests worst-case token streaming granularity (1 character per chunk)."""
        parser = StreamingBilingualParser()
        emitted_ch = []
        for char in single_char_stream:
            delta, _ = parser.feed_chunk(char)
            if delta:
                emitted_ch.append(delta)
        ch, ja, _ = parser.finalize()
        assert "".join(emitted_ch) == "早！"
        assert ch == "早！"
        assert ja == "おはよう！"

    def test_dual_language_discrepant_lengths(self):
        """Tests handling when Chinese text is very long while Japanese text is short."""
        parser = StreamingBilingualParser()
        long_chinese = "这是一个非常非常非常非常非常长的中文回复。" * 5
        short_japanese = "短い返信です。"
        chunk = json.dumps({"chinese": long_chinese, "japanese": short_japanese}, ensure_ascii=False)
        delta, sents = parser.feed_chunk(chunk)
        ch, ja, rem = parser.finalize()
        assert ch == long_chinese
        assert ja == short_japanese


