"""Pure normalization of legacy bilingual replies into ordered visual novel lines.

Chinese boundaries are terminal punctuation/newlines outside nested 「」/『』;
ellipsis and em dash are ordinary characters. Japanese boundaries come from the
existing strict sentence splitter (never its early TTS comma mode).

Pairing rule: "1:1 with tail merge"
Let Z be the Chinese sentences (split_chinese_sentences) and J the strict
Japanese sentences (split_japanese_sentences(text)). Let m = len(Z) and n = len(J).
- No Chinese (m == 0): one line per Japanese sentence, with zh = "".
- No Japanese (n == 0): one line per Chinese sentence, with ja = "".
- Both present: there are exactly k = min(m, n) lines.
  - For i < k-1, line i is (Z[i], J[i]).
  - The last line is ("".join(Z[k-1:]), "".join(J[k-1:])).
- Line shape: {index, zh, ja, emotion, act}, with act = {}.
  Emotion: detect_emotion_cue(ja) or emotion or classify_emotion(zh, ja).
- Lines are never revised after they are emitted.
"""

from __future__ import annotations

from typing import Any

from galgame2voice.services.emotion_classifier import classify_emotion, detect_emotion_cue
from galgame2voice.utils.text_splitter import split_japanese_sentences

MAX_LINE_LENGTH = 10000
_TERMINAL_PUNCT = frozenset("。！？!?\r\n")
_COMMA_PUNCT = frozenset("，、,")
_CLOSING_MARKS = frozenset("」』\"'”’）)】]")


def _find_split_end(text: str, start: int, limit: int, punct: frozenset[str]) -> int | None:
    """Return slice end inside (start, limit] right after the last matching punctuation."""
    for idx in range(limit - 1, start - 1, -1):
        if text[idx] in punct:
            end = idx + 1
            while end < limit and text[end] in _CLOSING_MARKS:
                end += 1
            return end
    return None


def split_overlong_text(text: str, max_length: int = MAX_LINE_LENGTH) -> list[str]:
    """Split text exceeding max_length by terminal punctuation, comma, or hard limit."""
    if max_length <= 0:
        raise ValueError("max_length must be positive")
    if not text:
        return []
    if len(text) <= max_length:
        return [text]

    parts: list[str] = []
    pos = 0
    total = len(text)
    while total - pos > max_length:
        limit = pos + max_length
        cut = (
            _find_split_end(text, pos, limit, _TERMINAL_PUNCT)
            or _find_split_end(text, pos, limit, _COMMA_PUNCT)
            or limit
        )
        parts.append(text[pos:cut])
        pos = cut
    if pos < total:
        parts.append(text[pos:])
    return parts


def split_chinese_sentences(text: str) -> list[str]:
    """Keep punctuation and quoted speech together, including nested quotes."""
    sentences: list[str] = []
    current: list[str] = []
    quotes: list[str] = []
    closing = {"「": "」", "『": "』"}
    for char in text:
        current.append(char)
        if char in closing:
            quotes.append(closing[char])
        elif quotes and char == quotes[-1]:
            quotes.pop()
        if not quotes and char in "。！？!?\r\n":
            sentence = "".join(current).strip()
            if sentence:
                sentences.append(sentence)
            current = []
    if sentence := "".join(current).strip():
        sentences.append(sentence)
    return sentences


def make_line(
    index: int,
    zh: str,
    ja: str,
    emotion: str | None = None,
    *,
    with_audio: bool = False,
) -> dict[str, Any]:
    """Create metadata using sentence cue then message fallback as audio."""
    line: dict[str, Any] = {
        "index": index,
        "zh": zh,
        "ja": ja,
        "emotion": detect_emotion_cue(ja) or emotion or classify_emotion(zh, ja),
        "act": {},
    }
    if with_audio:
        line["audio"] = {"status": "skipped", "chunks": []}
    return line


def build_lines(
    chinese: str,
    japanese: str,
    emotion: str | None = None,
    *,
    with_audio: bool = False,
) -> list[dict[str, Any]]:
    """Build deterministic lines from legacy whole-paragraph text using 1:1 with tail merge."""
    default_emotion = emotion or classify_emotion(chinese, japanese)
    z_sentences = split_chinese_sentences(chinese)
    j_sentences = split_japanese_sentences(japanese)
    m = len(z_sentences)
    n = len(j_sentences)

    if m == 0:
        units = [("", j) for j in j_sentences]
    elif n == 0:
        units = [(z, "") for z in z_sentences]
    else:
        k = min(m, n)
        units = [(z_sentences[i], j_sentences[i]) for i in range(k - 1)]
        units.append(("".join(z_sentences[k - 1 :]), "".join(j_sentences[k - 1 :])))

    lines: list[dict[str, Any]] = []
    for zh, ja in units:
        zh_parts = split_overlong_text(zh) if zh else []
        ja_parts = split_overlong_text(ja) if ja else []
        count = max(len(zh_parts), len(ja_parts), 1 if (zh or ja) else 0)
        for part_idx in range(count):
            lines.append(
                make_line(
                    len(lines),
                    zh_parts[part_idx] if part_idx < len(zh_parts) else "",
                    ja_parts[part_idx] if part_idx < len(ja_parts) else "",
                    default_emotion,
                    with_audio=with_audio,
                )
            )
    return lines
