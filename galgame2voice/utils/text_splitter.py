"""
Japanese dialogue sentence boundary splitter for galgame2voice.
Splits Japanese text by punctuation markers (。, ！, ？, !, ?, \n).
Preserves punctuation with the sentence and removes empty segments.
"""

import re
from typing import List


def split_japanese_sentences(
    text: str,
    is_first_chunk: bool = False,
    min_chars: int = 6,
) -> List[str]:
    """
    Splits Japanese text by punctuation markers (。, ！, ？, !, ?, \n).
    Preserves punctuation with the sentence and removes empty segments.

    When is_first_chunk=True, allows the first sentence chunk to split on
    clause pauses (、, ，, ,) provided the segment length >= min_chars.
    Subsequent sentence chunks strictly require terminal punctuation.

    Examples:
        "こんにちは！先生、今日はいい天気ですね。一緒に出かけましょう？"
        -> ["こんにちは！", "先生、今日はいい天気ですね。", "一緒に出かけましょう？"]
        "第一行。\n\n第二行！\n第三行？"
        -> ["第一行。", "第二行！", "第三行？"]
        "こんにちは、先生、今日はいい天気ですね。" (is_first_chunk=True, min_chars=6)
        -> ["こんにちは、", "先生、今日はいい天気ですね。"]
    """
    if not text:
        return []

    if not is_first_chunk:
        # Match contiguous segments of non-punctuation followed by punctuation markers
        pattern = r'([^。！？!?\n]+[。！？!?\n]*)'
        matches = re.findall(pattern, text)
        sentences = [m.strip() for m in matches if m.strip()]
        if not sentences and text.strip():
            return [text.strip()]
        return sentences

    # Agile first-chunk mode:
    # Scan for the first split boundary:
    # Either terminal punctuation [。！？!?\n] OR clause pause [、，,] if accumulated chars >= min_chars.
    terminal_punct = set("。！？!?\n")
    clause_punct = set("、，,")

    curr = []
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        curr.append(c)
        if c in terminal_punct:
            while i + 1 < n and text[i + 1] in terminal_punct:
                i += 1
                curr.append(text[i])
            first_sent = "".join(curr).strip()
            if first_sent:
                rem_text = text[i + 1:]
                subsequent = split_japanese_sentences(rem_text, is_first_chunk=False) if rem_text.strip() else []
                return [first_sent] + subsequent
        elif c in clause_punct:
            while i + 1 < n and text[i + 1] in clause_punct:
                i += 1
                curr.append(text[i])
            cand = "".join(curr).strip()
            if len(cand) >= min_chars:
                rem_text = text[i + 1:]
                subsequent = split_japanese_sentences(rem_text, is_first_chunk=False) if rem_text.strip() else []
                return [cand] + subsequent
        i += 1

    remaining = "".join(curr).strip()
    if remaining:
        return [remaining]
    return []


__all__ = ["split_japanese_sentences"]
