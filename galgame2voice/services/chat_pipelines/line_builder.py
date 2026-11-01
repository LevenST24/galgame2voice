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


def _unit_line_count(zh: str, ja: str) -> int:
    if not zh and not ja:
        return 0
    zh_count = len(split_overlong_text(zh)) if zh else 0
    ja_count = len(split_overlong_text(ja)) if ja else 0
    return max(zh_count, ja_count, 1)


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


class LineAligner:
    """Stateful 1:1 with tail merge line aligner."""

    def __init__(self, default_emotion: str | None = None) -> None:
        self.default_emotion = default_emotion
        self.chinese = ""
        self.chinese_sentences: list[str] | None = None
        self.emitted_lines: list[dict[str, Any]] = []
        self.emitted_units = 0
        self.non_tail_line_count = 0
        self._finalized = False

    def set_chinese(self, chinese: str) -> None:
        """Set Chinese text once complete and split into sentences."""
        self.chinese = chinese
        self.chinese_sentences = split_chinese_sentences(chinese)

    @property
    def has_chinese(self) -> bool:
        return self.chinese_sentences is not None

    def _emit_unit(
        self,
        zh: str,
        ja: str,
        out: list[dict[str, Any]],
        *,
        is_tail: bool = False,
    ) -> None:
        zh_parts = split_overlong_text(zh) if zh else []
        ja_parts = split_overlong_text(ja) if ja else []
        count = max(len(zh_parts), len(ja_parts), 1 if (zh or ja) else 0)
        for part_idx in range(count):
            line = make_line(
                len(self.emitted_lines),
                zh_parts[part_idx] if part_idx < len(zh_parts) else "",
                ja_parts[part_idx] if part_idx < len(ja_parts) else "",
                self.default_emotion,
            )
            self.emitted_lines.append(line)
            out.append(line)
        self.emitted_units += 1
        if not is_tail:
            self.non_tail_line_count = len(self.emitted_lines)

    def line_index_for_japanese_sentence(self, sentence_index: int, japanese: str) -> int:
        """Return the emitted line holding the start of a strict Japanese sentence.

        Sentences beyond the last Chinese sentence are merged into the tail unit,
        which may be cut into several lines; map each to the line holding its start.
        """
        z_sentences = self.chinese_sentences or []
        m = len(z_sentences)
        target_unit = sentence_index if m == 0 else min(sentence_index, m - 1)
        j_sentences = split_japanese_sentences(japanese)
        line_idx = 0
        for unit_idx in range(target_unit):
            zh_unit = z_sentences[unit_idx] if unit_idx < m else ""
            ja_unit = j_sentences[unit_idx] if unit_idx < len(j_sentences) else ""
            line_idx += _unit_line_count(zh_unit, ja_unit)
        if sentence_index > target_unit:
            offset = len("".join(j_sentences[target_unit:sentence_index]))
            end = 0
            for part_idx, part in enumerate(split_overlong_text("".join(j_sentences[target_unit:]))):
                end += len(part)
                if offset < end:
                    return line_idx + part_idx
        return line_idx

    def feed_japanese(self, japanese: str, *, final: bool = False) -> list[dict[str, Any]]:
        """Feed current Japanese text and return newly frozen lines."""
        if self._finalized:
            return []
        # A caller may supply stable declared reply metadata for online output.
        # Otherwise the whole-reply fallback is only known at finalization.
        if not self.default_emotion:
            if not final:
                return []
            self.default_emotion = classify_emotion(self.chinese, japanese)
        if self.chinese_sentences is None:
            if not final:
                return []
            self.chinese_sentences = []

        z_sentences = self.chinese_sentences
        m = len(z_sentences)
        j_sentences = split_japanese_sentences(japanese)
        n = len(j_sentences)

        new_lines: list[dict[str, Any]] = []

        if m == 0:
            limit = n if final else max(0, n - 1)
            while self.emitted_units < limit:
                i = self.emitted_units
                self._emit_unit("", j_sentences[i], new_lines)
            if final:
                self._finalized = True
            return new_lines

        if n == 0:
            if not final:
                return []
            while self.emitted_units < m:
                i = self.emitted_units
                self._emit_unit(z_sentences[i], "", new_lines)
            self._finalized = True
            return new_lines

        # Both present: exactly k = min(m, n) alignment units eventually
        # Online freezing: unit i (i < m - 1) is frozen once n > i + 1
        while self.emitted_units < m - 1 and n > self.emitted_units + 1:
            i = self.emitted_units
            self._emit_unit(z_sentences[i], j_sentences[i], new_lines)

        if final:
            k = min(m, n)
            while self.emitted_units < k - 1:
                i = self.emitted_units
                self._emit_unit(z_sentences[i], j_sentences[i], new_lines)

            if self.emitted_units == k - 1:
                zh_tail = "".join(z_sentences[k - 1:])
                ja_tail = "".join(j_sentences[k - 1:])
                self._emit_unit(zh_tail, ja_tail, new_lines, is_tail=True)
            self._finalized = True

        return new_lines


def build_lines(
    chinese: str,
    japanese: str,
    emotion: str | None = None,
    *,
    with_audio: bool = False,
) -> list[dict[str, Any]]:
    """Build deterministic lines using 1:1 with tail merge rule."""
    aligner = LineAligner(default_emotion=emotion)
    aligner.set_chinese(chinese)
    lines = aligner.feed_japanese(japanese, final=True)
    if with_audio:
        for line in lines:
            line["audio"] = {"status": "skipped", "chunks": []}
    return lines
