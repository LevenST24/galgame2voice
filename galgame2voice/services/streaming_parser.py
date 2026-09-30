"""
Streaming Bilingual Parser for galgame2voice.
Incremental state machine for parsing streaming LLM output tokens into
immediate Chinese delta text, dynamic voice prosody parameters, emotion state,
and completed Japanese sentence chunks.
Decoupled from chat orchestration for clean architecture, high maintainability,
and isolated unit testing.
"""

import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from galgame2voice.services.emotion_classifier import (
    EMOTION_NAME_MAP,
    VALID_EMOTIONS,
    classify_emotion,
    extract_bracketed_emotion,
)
from galgame2voice.utils.japanese_phonetics import extract_stage_directions_and_emotion
from galgame2voice.utils.prosody import (
    clamp_dynamic_speed,
    clamp_dynamic_temperature,
    clamp_dynamic_top_k,
    clamp_dynamic_top_p,
    clamp_dynamic_fragment_interval,
    calculate_adaptive_prosody,
)
from galgame2voice.utils.text_splitter import (
    split_japanese_sentences,
    is_natural_clause_boundary,
    is_short_salutation,
)

logger = logging.getLogger("galgame2voice.services.streaming_parser")

TERMINAL_PUNCT_WITH_CLOSING = re.compile(r'[。！？!?\n][」』"\'”’\)）\]】]*$')
CLAUSE_PUNCT_WITH_CLOSING = re.compile(r'[、，,][」』"\'”’\)）\]】]*$')
TRAILING_PUNCT_AND_CLOSING = re.compile(r'[、，,\s…\.〜~ー\-」』"\'”’\)）\]】]+$')

_RE_CHINESE_FIELD = re.compile(r'"chinese"\s*:\s*"((?:[^"\\]|\\.)*)')
_RE_CHINESE_FALLBACK = re.compile(r'(?:中文|Chinese)[:：]\s*(.*?)(?:(?:日文|Japanese)[:：]|$)', flags=re.DOTALL | re.IGNORECASE)
_RE_JAPANESE_FIELD = re.compile(r'"japanese"\s*:\s*"((?:[^"\\]|\\.)*)')
_RE_JAPANESE_CLOSED = re.compile(r'"japanese"\s*:\s*"(?:[^"\\]|\\.)*"')
_RE_JAPANESE_FALLBACK = re.compile(r'(?:日文|Japanese)[:：]\s*(.*)$', flags=re.DOTALL | re.IGNORECASE)
_RE_JSON_BLOCK = re.compile(r'\{.*\}', flags=re.DOTALL)
_RE_BRACKETED_JAPANESE = re.compile(r'【([^】]+)】')
_RE_BRACKETED_JAPANESE_STRIP = re.compile(r'【[^】]+】')

_RE_MD_CODE_BLOCK = re.compile(r'```(?:json)?\s*', flags=re.IGNORECASE)
_RE_MD_LEADING_BACKTICK = re.compile(r'^`\s*', flags=re.MULTILINE)
_RE_INCOMPLETE_UNICODE = re.compile(r'(?<!\\)(?:\\\\)*(\\u[0-9a-fA-F]{0,3})$')
_RE_TRAILING_BACKSLASHES = re.compile(r'\\+$')

_RE_TTS_BLOCK = re.compile(r'["\']?tts["\']?\s*:\s*\{([^}]*)')
_RE_PARAM_SPEED = re.compile(r'["\']?(?:speed|speed_factor)["\']?\s*:\s*["\']?(-?[0-9]*\.?[0-9]+)["\']?')
_RE_PARAM_TEMP = re.compile(r'["\']?(?:temp|temperature)["\']?\s*:\s*["\']?(-?[0-9]*\.?[0-9]+)["\']?')
_RE_PARAM_TOP_K = re.compile(r'["\']?top_?k["\']?\s*:\s*["\']?([0-9]+)["\']?', flags=re.IGNORECASE)
_RE_PARAM_TOP_P = re.compile(r'["\']?top_?p["\']?\s*:\s*["\']?(-?[0-9]*\.?[0-9]+)["\']?', flags=re.IGNORECASE)
_RE_PARAM_FRAG = re.compile(r'["\']?(?:fragment_interval|interval|pause)["\']?\s*:\s*["\']?(-?[0-9]*\.?[0-9]+)["\']?', flags=re.IGNORECASE)
_RE_PARAM_EMOTION = re.compile(r'["\']?emotion["\']?\s*:\s*["\']?([a-zA-Z\u4e00-\u9fa5]+)["\']?')


def _extract_numeric_param(
    text: str,
    pattern: re.Pattern,
    converter: type,
    clamp_fn: Any,
) -> Optional[Any]:
    """Searches pattern in text, parses numerical value, and applies safety clamping."""
    match = pattern.search(text)
    if match:
        try:
            return clamp_fn(converter(match.group(1)))
        except (ValueError, TypeError):
            pass
    return None


def _trim_unclosed_sentence(sentences: List[str], is_first: bool) -> List[str]:
    """Trims incomplete trailing sentence chunk if sentence ending punctuation is missing."""
    if not sentences:
        return sentences
    last_sent = sentences[-1]
    if is_first and len(sentences) == 1:
        clause = TRAILING_PUNCT_AND_CLOSING.sub('', last_sent)
        valid_end = bool(
            TERMINAL_PUNCT_WITH_CLOSING.search(last_sent)
            or (
                CLAUSE_PUNCT_WITH_CLOSING.search(last_sent)
                and (len(last_sent.strip()) >= 6 or is_short_salutation(clause))
                and is_natural_clause_boundary(clause)
            )
        )
        if not valid_end:
            return sentences[:-1]
    else:
        if not TERMINAL_PUNCT_WITH_CLOSING.search(last_sent):
            return sentences[:-1]
    return sentences


def _normalize_valid_emotion(val: Any) -> Optional[str]:
    """Normalizes raw emotion label and verifies it belongs to VALID_EMOTIONS."""
    if not val:
        return None
    raw_e = str(val).lower()
    norm = EMOTION_NAME_MAP.get(raw_e, raw_e)
    return norm if norm in VALID_EMOTIONS else None


class StreamingBilingualParser:
    """
    Incremental state machine for parsing streaming LLM output tokens into
    immediate Chinese delta text, emotion state, and completed Japanese sentence chunks.
    Robust against markdown code fences, unescaped characters, partial JSON tokens,
    Unicode escape sequences split across chunks, and non-JSON fallback text.
    """

    def __init__(self):
        self.buffer: str = ""
        self.chinese_extracted: str = ""
        self.japanese_extracted: str = ""
        self.emotion_extracted: str = ""
        self.emitted_chinese_len: int = 0
        # Monotone CHARACTER cursor over the normalized confirmed Japanese text
        # (the concatenation of the confirmed completed sentences) that has
        # already been dispatched to TTS. feed_chunk/finalize re-split the full
        # accumulated Japanese text every round and compare the confirmed prefix
        # against this offset, guaranteeing each sentence is emitted exactly once.
        # A character offset (not a sentence index) is required because the
        # splitter runs with is_first_chunk=True until the first emission and
        # False afterwards, and the two modes yield DIFFERENT boundaries for the
        # same text (see _drain_new_ja_sentences).
        self.emitted_japanese_len: int = 0
        self.first_sentence_emitted: bool = False
        self.is_plain_text_fallback: bool = False
        self.tts_speed: Optional[float] = None
        self.tts_temperature: Optional[float] = None
        self.tts_top_k: Optional[int] = None
        self.tts_top_p: Optional[float] = None
        self.tts_fragment_interval: Optional[float] = None
        self.tts_emotion: Optional[str] = None
        self.tts_params: Dict[str, Any] = {}

    def _advance_chinese(self, current_ch: str) -> str:
        """Updates extracted Chinese buffer and returns new incremental delta text."""
        new_delta = ""
        if len(current_ch) > self.emitted_chinese_len:
            new_delta = current_ch[self.emitted_chinese_len:]
            self.emitted_chinese_len = len(current_ch)
        self.chinese_extracted = current_ch
        return new_delta

    def _set_lead_emotion(self, emo: str) -> None:
        """Sets lead extracted emotion and defaults TTS emotion parameter if unset."""
        self.emotion_extracted = emo
        if self.tts_emotion is None:
            self.tts_emotion = emo
            self.tts_params["emotion"] = emo

    def clean_markdown_delimiters(self, text: str) -> str:
        """Strips markdown ```json and ``` code block wrappers."""
        cleaned = _RE_MD_CODE_BLOCK.sub('', text)
        cleaned = _RE_MD_LEADING_BACKTICK.sub('', cleaned)
        return cleaned

    def _strip_incomplete_escape(self, s: str) -> str:
        """Strips trailing incomplete unicode or dangling backslash escape sequence."""
        u_match = _RE_INCOMPLETE_UNICODE.search(s)
        if u_match:
            return s[:-len(u_match.group(1))]

        m = _RE_TRAILING_BACKSLASHES.search(s)
        if m and len(m.group(0)) % 2 == 1:
            return s[:-1]

        return s

    def _unescape_json_string(self, raw_str: str) -> str:
        """Safely unescapes raw JSON string fragment."""
        safe_str = self._strip_incomplete_escape(raw_str)
        try:
            return json.loads(f'"{safe_str}"')
        except Exception:
            return (
                safe_str.replace('\\"', '"')
                .replace('\\n', '\n')
                .replace('\\t', '\t')
                .replace('\\r', '\r')
                .replace('\\\\', '\\')
            )

    def get_emotion(self) -> str:
        """Returns the classified emotion for current extracted content."""
        return classify_emotion(self.chinese_extracted, self.japanese_extracted, self.emotion_extracted)

    def get_dynamic_tts_options(
        self,
        base_options: Optional[Dict[str, Any]] = None,
        adaptive_enabled: bool = True,
        sentence_text: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Merges base session TTS options with dynamic parameters if adaptive_enabled is True.
        When adaptive_enabled is False, returns base_options without dynamic overrides.
        If explicit dynamic parameters are provided (e.g. from LLM JSON), uses them.
        Otherwise, applies fine-grained emotion archetype baseline + sentence nuance prosody.
        """
        opts = dict(base_options or {})
        if "ai_adaptive_voice" in opts or not adaptive_enabled:
            opts["ai_adaptive_voice"] = bool(adaptive_enabled)
        if not adaptive_enabled:
            return opts

        has_dynamic_llm = any(
            v is not None
            for v in (self.tts_speed, self.tts_temperature, self.tts_top_k, self.tts_top_p, self.tts_fragment_interval, self.tts_emotion)
        )
        has_explicit_emotion = bool(self.tts_emotion or opts.get("emotion"))

        # Backward compatibility for legacy tests checking get_dynamic_tts_options(base_opts) without sentence
        if not has_dynamic_llm and not has_explicit_emotion and not sentence_text and not self.is_plain_text_fallback:
            return opts

        emo = self.tts_emotion or opts.get("emotion")
        if not emo and sentence_text:
            _, text_emo = extract_stage_directions_and_emotion(sentence_text)
            if text_emo:
                emo = text_emo
        if not emo and self.buffer:
            _, text_emo = extract_stage_directions_and_emotion(self.buffer)
            if text_emo:
                emo = text_emo
        if not emo:
            emo = self.get_emotion()

        text_context = sentence_text or self.japanese_extracted or self.chinese_extracted or self.buffer
        adaptive_calc = calculate_adaptive_prosody(text=text_context, emotion=emo, base_params=opts)

        # Apply calculated baseline if not explicitly pinned in base_options
        if "speed" not in opts and "speed_factor" not in opts:
            opts["speed"] = adaptive_calc["speed"]
            opts["speed_factor"] = adaptive_calc["speed"]
        if "temperature" not in opts and "temp" not in opts:
            opts["temperature"] = adaptive_calc["temperature"]
            opts["temp"] = adaptive_calc["temperature"]
        if "top_k" not in opts:
            opts["top_k"] = adaptive_calc["top_k"]
        if "top_p" not in opts:
            opts["top_p"] = adaptive_calc["top_p"]
        if "fragment_interval" not in opts:
            opts["fragment_interval"] = adaptive_calc["fragment_interval"]

        # Explicit LLM tts parameters override
        if self.tts_speed is not None:
            opts["speed"] = self.tts_speed
            opts["speed_factor"] = self.tts_speed
        if self.tts_temperature is not None:
            opts["temperature"] = self.tts_temperature
            opts["temp"] = self.tts_temperature
        if self.tts_top_k is not None:
            opts["top_k"] = self.tts_top_k
        if self.tts_top_p is not None:
            opts["top_p"] = self.tts_top_p
        if self.tts_fragment_interval is not None:
            opts["fragment_interval"] = self.tts_fragment_interval
        if emo:
            opts["emotion"] = emo

        return opts

    def _parse_dynamic_tts_block(self, sanitized: str) -> None:
        """Extracts dynamic TTS parameters and emotion tags from regex matches in raw stream buffer."""
        tts_match = _RE_TTS_BLOCK.search(sanitized)
        if tts_match:
            tts_block = tts_match.group(1)

            val_speed = _extract_numeric_param(tts_block, _RE_PARAM_SPEED, float, clamp_dynamic_speed)
            if val_speed is not None:
                self.tts_speed = val_speed
                self.tts_params["speed"] = val_speed

            val_temp = _extract_numeric_param(tts_block, _RE_PARAM_TEMP, float, clamp_dynamic_temperature)
            if val_temp is not None:
                self.tts_temperature = val_temp
                self.tts_params["temperature"] = val_temp
                self.tts_params["temp"] = val_temp

            val_top_k = _extract_numeric_param(tts_block, _RE_PARAM_TOP_K, int, clamp_dynamic_top_k)
            if val_top_k is not None:
                self.tts_top_k = val_top_k
                self.tts_params["top_k"] = val_top_k

            val_top_p = _extract_numeric_param(tts_block, _RE_PARAM_TOP_P, float, clamp_dynamic_top_p)
            if val_top_p is not None:
                self.tts_top_p = val_top_p
                self.tts_params["top_p"] = val_top_p

            val_frag = _extract_numeric_param(tts_block, _RE_PARAM_FRAG, float, clamp_dynamic_fragment_interval)
            if val_frag is not None:
                self.tts_fragment_interval = val_frag
                self.tts_params["fragment_interval"] = val_frag

            emo_match_tts = _RE_PARAM_EMOTION.search(tts_block)
            if emo_match_tts:
                norm_emo = _normalize_valid_emotion(emo_match_tts.group(1))
                if norm_emo:
                    self.tts_emotion = norm_emo
                    self.emotion_extracted = norm_emo
                    self.tts_params["emotion"] = norm_emo

        # Standalone emotion extraction
        emo_match = _RE_PARAM_EMOTION.search(sanitized)
        if emo_match:
            norm_emo = _normalize_valid_emotion(emo_match.group(1))
            if norm_emo:
                self.emotion_extracted = norm_emo

    def feed_chunk(self, chunk: str) -> Tuple[str, List[str]]:
        """
        Feeds an incoming stream token chunk.
        Returns:
            (new_chinese_delta, list_of_newly_completed_japanese_sentences)
        """
        if not chunk:
            return "", []

        self.buffer += chunk
        sanitized = self.clean_markdown_delimiters(self.buffer)

        new_chinese_delta = ""
        new_sentences: List[str] = []

        # 0. Dynamic TTS Parameter & Emotion Extraction
        self._parse_dynamic_tts_block(sanitized)

        # 1. Incremental Chinese Extraction
        ch_match = _RE_CHINESE_FIELD.search(sanitized)
        if ch_match:
            raw_ch = ch_match.group(1)
            current_ch = self._unescape_json_string(raw_ch)
            new_chinese_delta = self._advance_chinese(current_ch)
        else:
            # Fallback check: If the stream contains structured Chinese: / 中文:
            ch_fallback = _RE_CHINESE_FALLBACK.search(sanitized)
            if ch_fallback:
                self.is_plain_text_fallback = True
                current_ch = ch_fallback.group(1).strip()
                if current_ch and current_ch != self.chinese_extracted:
                    new_chinese_delta = self._advance_chinese(current_ch)
            elif not self.chinese_extracted and len(sanitized) > 15 and not sanitized.lstrip().startswith(("{", "```")):
                self.is_plain_text_fallback = True
                current_ch = sanitized.strip()
                new_chinese_delta = self._advance_chinese(current_ch)

        if not self.emotion_extracted:
            lead_emo = None
            if self.chinese_extracted:
                lead_emo, _ = extract_bracketed_emotion(self.chinese_extracted)
            if not lead_emo and self.japanese_extracted:
                lead_emo, _ = extract_bracketed_emotion(self.japanese_extracted)
            if lead_emo:
                self._set_lead_emotion(lead_emo)

        # 2. Incremental Japanese Sentence Slicing
        ja_match = _RE_JAPANESE_FIELD.search(sanitized)
        if ja_match:
            raw_ja = ja_match.group(1)
            current_ja = self._unescape_json_string(raw_ja)
            self.japanese_extracted = current_ja

            if not self.emotion_extracted:
                lead_emo_ja, _ = extract_bracketed_emotion(self.japanese_extracted)
                if lead_emo_ja:
                    self._set_lead_emotion(lead_emo_ja)

            is_ja_closed = bool(
                _RE_JAPANESE_CLOSED.search(sanitized)
                or sanitized.rstrip().endswith(('"}', '"}`', '"} \n`', '"} \n'))
            )
            new_sentences = self._extract_new_ja_sentences(current_ja, is_closed=is_ja_closed)
        elif self.is_plain_text_fallback:
            ja_fallback = _RE_JAPANESE_FALLBACK.search(sanitized)
            if ja_fallback:
                current_ja = ja_fallback.group(1).strip()
                self.japanese_extracted = current_ja
                new_sentences = self._extract_new_ja_sentences(current_ja, is_closed=False)

        return new_chinese_delta, new_sentences

    def _extract_new_ja_sentences(self, current_ja: str, is_closed: bool) -> List[str]:
        """Splits accumulated Japanese text into sentences and drains newly confirmed ones."""
        is_first = not self.first_sentence_emitted
        all_sentences = split_japanese_sentences(current_ja, is_first_chunk=is_first)
        # If the sentence source is not yet closed, the trailing sentence might still be growing
        if not is_closed:
            all_sentences = _trim_unclosed_sentence(all_sentences, is_first)
        return self._drain_new_ja_sentences(all_sentences)

    def _drain_new_ja_sentences(self, confirmed_sentences: List[str]) -> List[str]:
        """
        Advances the monotone character cursor over the confirmed-completed
        Japanese prefix and returns only the newly confirmed sentences.

        Why a character cursor instead of a sentence-index cursor: the splitter
        is invoked with ``is_first_chunk=True`` until the first sentence has been
        emitted and with ``is_first_chunk=False`` afterwards, and those two modes
        produce DIFFERENT boundaries for the very same text -- agile mode may cut
        on a clause comma (``["こんにちは、", "先生、今日は…ですね。"]``) while the
        strict mode merges them back into a single sentence
        (``["こんにちは、先生、今日は…ですね。"]``). Comparing sentence *counts*
        would then report "nothing new" and silently drop the remainder of the
        already-started sentence, so the cursor is tracked as a character offset
        over the concatenated confirmed text, which is guaranteed to be a
        monotonically growing prefix of the accumulated Japanese text.
        """
        confirmed_text = "".join(confirmed_sentences)
        if len(confirmed_text) <= self.emitted_japanese_len:
            return []

        remainder = confirmed_text[self.emitted_japanese_len:]
        new_sentences = split_japanese_sentences(remainder, is_first_chunk=not self.first_sentence_emitted)
        # The splitter only ever discards pure whitespace, so advancing the cursor
        # past `confirmed_text` is lossless even when nothing is emitted here.
        self.emitted_japanese_len = len(confirmed_text)
        if new_sentences:
            self.first_sentence_emitted = True
        return new_sentences

    @staticmethod
    def _parse_json_payload(sanitized: str) -> Optional[Dict[str, Any]]:
        """Attempts to parse JSON payload directly or extracts embedded JSON block."""
        try:
            # Fast path: direct JSON parse if buffer is a clean JSON object
            parsed = json.loads(sanitized)
        except json.JSONDecodeError:
            # Only fall back to regex block extraction when direct JSON decoding fails
            parsed = None
            json_match = _RE_JSON_BLOCK.search(sanitized)
            if json_match:
                try:
                    parsed = json.loads(json_match.group(0))
                except json.JSONDecodeError:
                    # Extracted block is also malformed JSON; fall through to buffer regex extraction
                    pass
        return parsed if isinstance(parsed, dict) else None

    def _apply_parsed_json_dict(self, parsed: Dict[str, Any]) -> None:
        """Applies parsed JSON fields to extracted Chinese, Japanese, and TTS parameters."""
        self.chinese_extracted = parsed.get("chinese", self.chinese_extracted)
        self.japanese_extracted = parsed.get("japanese", self.japanese_extracted)
        if "emotion" in parsed:
            norm_e = _normalize_valid_emotion(parsed["emotion"])
            if norm_e:
                self.emotion_extracted = norm_e
        # Parse tts block
        if "tts" in parsed and isinstance(parsed["tts"], dict):
            t_dict = parsed["tts"]
            if "speed" in t_dict or "speed_factor" in t_dict:
                raw_sp = t_dict.get("speed", t_dict.get("speed_factor"))
                self.tts_speed = clamp_dynamic_speed(raw_sp)
                self.tts_params["speed"] = self.tts_speed
            if "temp" in t_dict or "temperature" in t_dict:
                raw_temp = t_dict.get("temp", t_dict.get("temperature"))
                self.tts_temperature = clamp_dynamic_temperature(raw_temp)
                self.tts_params["temperature"] = self.tts_temperature
                self.tts_params["temp"] = self.tts_temperature
            if "emotion" in t_dict:
                norm_te = _normalize_valid_emotion(t_dict["emotion"])
                if norm_te:
                    self.tts_emotion = norm_te
                    self.emotion_extracted = norm_te
                    self.tts_params["emotion"] = norm_te

    def _apply_fallback_buffer_parsing(self, sanitized: str) -> None:
        """Extracts Chinese, Japanese, and TTS parameters using regex and heuristics on unclosed buffer."""
        ch_match = _RE_CHINESE_FIELD.search(sanitized)
        if ch_match:
            self.chinese_extracted = self._unescape_json_string(ch_match.group(1))
        ja_match = _RE_JAPANESE_FIELD.search(sanitized)
        if ja_match:
            self.japanese_extracted = self._unescape_json_string(ja_match.group(1))
        self._parse_dynamic_tts_block(sanitized)

        # Fallback for structured text without valid JSON
        ch_fallback = _RE_CHINESE_FALLBACK.search(sanitized)
        ja_fallback = _RE_JAPANESE_FALLBACK.search(sanitized)
        if ch_fallback:
            self.chinese_extracted = ch_fallback.group(1).strip()
        if ja_fallback:
            self.japanese_extracted = ja_fallback.group(1).strip()
        if not self.chinese_extracted:
            self.chinese_extracted = sanitized

        if self.chinese_extracted and "【" in self.chinese_extracted and "】" in self.chinese_extracted:
            ja_bracket = _RE_BRACKETED_JAPANESE.search(self.chinese_extracted)
            if ja_bracket:
                if not self.japanese_extracted or self.japanese_extracted == self.chinese_extracted:
                    self.japanese_extracted = ja_bracket.group(1).strip()
                self.chinese_extracted = _RE_BRACKETED_JAPANESE_STRIP.sub('', self.chinese_extracted).strip()

        if not self.japanese_extracted:
            self.japanese_extracted = self.chinese_extracted

    def _extract_trailing_bracketed_emotion(self) -> None:
        """Extracts and strips bracketed emotion from extracted Chinese or Japanese text if emotion is unset."""
        if self.emotion_extracted:
            return
        lead_emo = None
        if self.chinese_extracted:
            lead_emo, cl_ch = extract_bracketed_emotion(self.chinese_extracted)
            if lead_emo and cl_ch:
                self.chinese_extracted = cl_ch
        if not lead_emo and self.japanese_extracted:
            lead_emo, cl_ja = extract_bracketed_emotion(self.japanese_extracted)
            if lead_emo and cl_ja:
                self.japanese_extracted = cl_ja
        if lead_emo:
            self._set_lead_emotion(lead_emo)

    def finalize(self) -> Tuple[str, str, List[str]]:
        """
        Flushes parser buffer at end of stream.
        Returns:
            (full_chinese, full_japanese, remaining_unemitted_sentences)
        """
        sanitized = self.clean_markdown_delimiters(self.buffer).strip()

        parsed = self._parse_json_payload(sanitized)
        if parsed is not None:
            self._apply_parsed_json_dict(parsed)
        else:
            self._apply_fallback_buffer_parsing(sanitized)

        self._extract_trailing_bracketed_emotion()
        self.emotion_extracted = classify_emotion(self.chinese_extracted, self.japanese_extracted, self.emotion_extracted)

        remaining_sentences: List[str] = []
        if self.japanese_extracted:
            # Re-split the full accumulated Japanese text with the same
            # is_first_chunk parameter as feed_chunk, then drain everything
            # beyond the monotone emitted-sentence cursor so feed_chunk and
            # finalize stay aligned (no duplicated or skipped sentences).
            remaining_sentences = self._extract_new_ja_sentences(self.japanese_extracted, is_closed=True)

        return self.chinese_extracted, self.japanese_extracted, remaining_sentences

    @classmethod
    def parse_full_text(cls, raw_text: str) -> Tuple[str, str, "StreamingBilingualParser"]:
        """
        Parses non-streaming bilingual completion text into (chinese, japanese, parser).
        Guarantees fallback to raw_text if extracted fields are empty.
        """
        parser = cls()
        parser.feed_chunk(raw_text)
        chinese, japanese, _ = parser.finalize()
        if not chinese:
            chinese = raw_text
        if not japanese:
            japanese = chinese
        return chinese, japanese, parser
