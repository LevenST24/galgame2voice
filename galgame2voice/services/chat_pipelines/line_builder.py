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

    def set_chinese(self, chinese: str) -> None:
        """Set Chinese text once complete and split into sentences."""
        self.chinese = chinese
        self.chinese_sentences = split_chinese_sentences(chinese)

    @property
    def has_chinese(self) -> bool:
        return self.chinese_sentences is not None

    def feed_japanese(self, japanese: str, *, final: bool = False) -> list[dict[str, Any]]:
        """Feed current Japanese text and return newly frozen lines."""
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
            while len(self.emitted_lines) < limit:
                i = len(self.emitted_lines)
                line = make_line(i, "", j_sentences[i], self.default_emotion)
                self.emitted_lines.append(line)
                new_lines.append(line)
            return new_lines

        if n == 0:
            if not final:
                return []
            while len(self.emitted_lines) < m:
                i = len(self.emitted_lines)
                line = make_line(i, z_sentences[i], "", self.default_emotion)
                self.emitted_lines.append(line)
                new_lines.append(line)
            return new_lines

        # Both present: exactly k = min(m, n) lines eventually
        # Online freezing: line i (i < m - 1) is frozen once n > i + 1
        while len(self.emitted_lines) < m - 1 and n > len(self.emitted_lines) + 1:
            i = len(self.emitted_lines)
            line = make_line(i, z_sentences[i], j_sentences[i], self.default_emotion)
            self.emitted_lines.append(line)
            new_lines.append(line)

        if final:
            k = min(m, n)
            while len(self.emitted_lines) < k - 1:
                i = len(self.emitted_lines)
                line = make_line(i, z_sentences[i], j_sentences[i], self.default_emotion)
                self.emitted_lines.append(line)
                new_lines.append(line)

            if len(self.emitted_lines) == k - 1:
                i = k - 1
                zh_tail = "".join(z_sentences[k - 1:])
                ja_tail = "".join(j_sentences[k - 1:])
                line = make_line(i, zh_tail, ja_tail, self.default_emotion)
                self.emitted_lines.append(line)
                new_lines.append(line)

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
