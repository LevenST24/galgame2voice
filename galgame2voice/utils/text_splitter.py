"""
Japanese dialogue sentence boundary splitter for galgame2voice.
Splits Japanese text by punctuation markers (。, ！, ？, !, ?, \n).
Preserves punctuation with the sentence and removes empty segments.
"""

import re
from typing import List


def split_japanese_sentences(text: str) -> List[str]:
    """
    Splits Japanese text by punctuation markers (。, ！, ？, !, ?, \n).
    Preserves punctuation with the sentence and removes empty segments.

    Examples:
        "こんにちは！先生、今日はいい天気ですね。一緒に出かけましょう？"
        -> ["こんにちは！", "先生、今日はいい天気ですね。", "一緒に出かけましょう？"]
        "第一行。\n\n第二行！\n第三行？"
        -> ["第一行。", "第二行！", "第三行？"]
    """
    if not text:
        return []

    # Match contiguous segments of non-punctuation followed by punctuation markers
    pattern = r'([^。！？!?\n]+[。！？!?\n]*)'
    matches = re.findall(pattern, text)
    sentences = [m.strip() for m in matches if m.strip()]
    if not sentences and text.strip():
        return [text.strip()]
    return sentences


__all__ = ["split_japanese_sentences"]
