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
)
from galgame2voice.utils.prosody import (
    clamp_dynamic_speed,
    clamp_dynamic_temperature,
)
from galgame2voice.utils.text_splitter import split_japanese_sentences

logger = logging.getLogger("galgame2voice.services.streaming_parser")


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
        self.emitted_japanese_len: int = 0
        self.first_sentence_emitted: bool = False
        self.is_plain_text_fallback: bool = False
        self.tts_speed: Optional[float] = None
        self.tts_temperature: Optional[float] = None
        self.tts_emotion: Optional[str] = None
        self.tts_params: Dict[str, Any] = {}

    def clean_markdown_delimiters(self, text: str) -> str:
        """Strips markdown ```json and ``` code block wrappers."""
        cleaned = re.sub(r'```(?:json)?\s*', '', text, flags=re.IGNORECASE)
        cleaned = re.sub(r'^`\s*', '', cleaned, flags=re.MULTILINE)
        return cleaned

    def _strip_incomplete_escape(self, s: str) -> str:
        """Strips trailing incomplete unicode or dangling backslash escape sequence."""
        u_match = re.search(r'(?<!\\)(?:\\\\)*(\\u[0-9a-fA-F]{0,3})$', s)
        if u_match:
            return s[:-len(u_match.group(1))]

        m = re.search(r'\\+$', s)
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
    ) -> Dict[str, Any]:
        """
        Merges base session TTS options with dynamic parameters if adaptive_enabled is True.
        When adaptive_enabled is False, returns base_options without dynamic overrides.
        """
        opts = dict(base_options or {})
        if "ai_adaptive_voice" in opts or not adaptive_enabled:
            opts["ai_adaptive_voice"] = bool(adaptive_enabled)
        if not adaptive_enabled:
            return opts

        if self.tts_speed is not None:
            opts["speed"] = self.tts_speed
            opts["speed_factor"] = self.tts_speed
        if self.tts_temperature is not None:
            opts["temperature"] = self.tts_temperature
            opts["temp"] = self.tts_temperature
        if self.tts_emotion:
            opts["emotion"] = self.tts_emotion

        return opts

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
        tts_match = re.search(r'["\']?tts["\']?\s*:\s*\{([^}]*)', sanitized)
        if tts_match:
            tts_block = tts_match.group(1)
            sp_match = re.search(r'["\']?(?:speed|speed_factor)["\']?\s*:\s*["\']?(-?[0-9]*\.?[0-9]+)["\']?', tts_block)
            if sp_match:
                try:
                    raw_sp = float(sp_match.group(1))
                    self.tts_speed = clamp_dynamic_speed(raw_sp)
                    self.tts_params["speed"] = self.tts_speed
                except (ValueError, TypeError):
                    pass

            temp_match = re.search(r'["\']?(?:temp|temperature)["\']?\s*:\s*["\']?(-?[0-9]*\.?[0-9]+)["\']?', tts_block)
            if temp_match:
                try:
                    raw_temp = float(temp_match.group(1))
                    self.tts_temperature = clamp_dynamic_temperature(raw_temp)
                    self.tts_params["temperature"] = self.tts_temperature
                    self.tts_params["temp"] = self.tts_temperature
                except (ValueError, TypeError):
                    pass

            emo_match_tts = re.search(r'["\']?emotion["\']?\s*:\s*["\']?([a-zA-Z\u4e00-\u9fa5]+)["\']?', tts_block)
            if emo_match_tts:
                raw_emo = emo_match_tts.group(1).lower()
                if raw_emo in EMOTION_NAME_MAP:
                    raw_emo = EMOTION_NAME_MAP[raw_emo]
                if raw_emo in VALID_EMOTIONS:
                    self.tts_emotion = raw_emo
                    self.emotion_extracted = raw_emo
                    self.tts_params["emotion"] = raw_emo

        # Standalone emotion extraction
        emo_match = re.search(r'["\']?emotion["\']?\s*:\s*["\']?([a-zA-Z\u4e00-\u9fa5]+)["\']?', sanitized)
        if emo_match:
            raw_emo = emo_match.group(1).lower()
            if raw_emo in EMOTION_NAME_MAP:
                raw_emo = EMOTION_NAME_MAP[raw_emo]
            if raw_emo in VALID_EMOTIONS:
                self.emotion_extracted = raw_emo

        # 1. Incremental Chinese Extraction
        ch_match = re.search(r'"chinese"\s*:\s*"((?:[^"\\]|\\.)*)', sanitized)
        if ch_match:
            raw_ch = ch_match.group(1)
            current_ch = self._unescape_json_string(raw_ch)

            if len(current_ch) > self.emitted_chinese_len:
                new_chinese_delta = current_ch[self.emitted_chinese_len:]
                self.chinese_extracted = current_ch
                self.emitted_chinese_len = len(current_ch)
        else:
            # Fallback check: If the stream does not look like JSON after some tokens
            if not self.chinese_extracted and len(sanitized) > 15 and not sanitized.lstrip().startswith("{"):
                self.is_plain_text_fallback = True
                ch_fallback = re.search(r'(?:中文|Chinese)[:：]\s*(.*?)(?:(?:日文|Japanese)[:：]|$)', sanitized, flags=re.DOTALL | re.IGNORECASE)
                if ch_fallback:
                    current_ch = ch_fallback.group(1).strip()
                else:
                    current_ch = sanitized.strip()

                if len(current_ch) > self.emitted_chinese_len:
                    new_chinese_delta = current_ch[self.emitted_chinese_len:]
                    self.chinese_extracted = current_ch
                    self.emitted_chinese_len = len(current_ch)

        # 2. Incremental Japanese Sentence Slicing
        ja_match = re.search(r'"japanese"\s*:\s*"((?:[^"\\]|\\.)*)', sanitized)
        if ja_match:
            raw_ja = ja_match.group(1)
            current_ja = self._unescape_json_string(raw_ja)
            self.japanese_extracted = current_ja

            is_first = not self.first_sentence_emitted
            all_sentences = split_japanese_sentences(current_ja, is_first_chunk=is_first)
            # If neither the japanese field nor the JSON object is closed, the last sentence might still be growing
            is_ja_closed = bool(
                re.search(r'"japanese"\s*:\s*"(?:[^"\\]|\\.)*"', sanitized)
                or sanitized.rstrip().endswith(('"}', '"}`', '"} \n`', '"} \n', '"}'))
            )
            if not is_ja_closed:
                if all_sentences:
                    last_sent = all_sentences[-1]
                    if is_first and len(all_sentences) == 1:
                        valid_end = bool(
                            re.search(r'[。！？!?\n]$', last_sent)
                            or (re.search(r'[、，,]$', last_sent) and len(last_sent.strip()) >= 6)
                        )
                        if not valid_end:
                            all_sentences = all_sentences[:-1]
                    else:
                        if not re.search(r'[。！？!?\n]$', last_sent):
                            all_sentences = all_sentences[:-1]

            completed_text = "".join(all_sentences)
            if len(completed_text) > self.emitted_japanese_len:
                remaining = completed_text[self.emitted_japanese_len:]
                new_sentences = split_japanese_sentences(remaining, is_first_chunk=not self.first_sentence_emitted)
                self.emitted_japanese_len = len(completed_text)
                if new_sentences:
                    self.first_sentence_emitted = True
        elif self.is_plain_text_fallback:
            ja_fallback = re.search(r'(?:日文|Japanese)[:：]\s*(.*)$', sanitized, flags=re.DOTALL | re.IGNORECASE)
            if ja_fallback:
                current_ja = ja_fallback.group(1).strip()
                self.japanese_extracted = current_ja
                is_first = not self.first_sentence_emitted
                all_sentences = split_japanese_sentences(current_ja, is_first_chunk=is_first)
                if all_sentences:
                    last_sent = all_sentences[-1]
                    if is_first and len(all_sentences) == 1:
                        valid_end = bool(
                            re.search(r'[。！？!?\n]$', last_sent)
                            or (re.search(r'[、，,]$', last_sent) and len(last_sent.strip()) >= 6)
                        )
                        if not valid_end:
                            all_sentences = all_sentences[:-1]
                    else:
                        if not re.search(r'[。！？!?\n]$', last_sent):
                            all_sentences = all_sentences[:-1]
                completed_text = "".join(all_sentences)
                if len(completed_text) > self.emitted_japanese_len:
                    remaining = completed_text[self.emitted_japanese_len:]
                    new_sentences = split_japanese_sentences(remaining, is_first_chunk=not self.first_sentence_emitted)
                    self.emitted_japanese_len = len(completed_text)
                    if new_sentences:
                        self.first_sentence_emitted = True

        return new_chinese_delta, new_sentences

    def finalize(self) -> Tuple[str, str, List[str]]:
        """
        Flushes parser buffer at end of stream.
        Returns:
            (full_chinese, full_japanese, remaining_unemitted_sentences)
        """
        sanitized = self.clean_markdown_delimiters(self.buffer).strip()

        # Try full JSON parsing (including finding JSON block within leading text)
        parsed = None
        try:
            parsed = json.loads(sanitized)
        except Exception:
            json_match = re.search(r'\{.*\}', sanitized, flags=re.DOTALL)
            if json_match:
                try:
                    parsed = json.loads(json_match.group(0))
                except Exception:
                    pass

        if isinstance(parsed, dict):
            self.chinese_extracted = parsed.get("chinese", self.chinese_extracted)
            self.japanese_extracted = parsed.get("japanese", self.japanese_extracted)
            if "emotion" in parsed:
                raw_e = str(parsed["emotion"]).lower()
                if raw_e in EMOTION_NAME_MAP:
                    raw_e = EMOTION_NAME_MAP[raw_e]
                if raw_e in VALID_EMOTIONS:
                    self.emotion_extracted = raw_e
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
                    raw_te = str(t_dict["emotion"]).lower()
                    if raw_te in EMOTION_NAME_MAP:
                        raw_te = EMOTION_NAME_MAP[raw_te]
                    if raw_te in VALID_EMOTIONS:
                        self.tts_emotion = raw_te
                        self.emotion_extracted = self.tts_emotion
                        self.tts_params["emotion"] = self.tts_emotion
        else:
            # Try regex extraction for unclosed JSON
            ch_match = re.search(r'"chinese"\s*:\s*"((?:[^"\\]|\\.)*)', sanitized)
            if ch_match:
                self.chinese_extracted = self._unescape_json_string(ch_match.group(1))
            ja_match = re.search(r'"japanese"\s*:\s*"((?:[^"\\]|\\.)*)', sanitized)
            if ja_match:
                self.japanese_extracted = self._unescape_json_string(ja_match.group(1))
            emo_match = re.search(r'["\']?emotion["\']?\s*:\s*["\']?([a-zA-Z\u4e00-\u9fa5]+)["\']?', sanitized)
            if emo_match:
                raw_emo = emo_match.group(1).lower()
                if raw_emo in EMOTION_NAME_MAP:
                    raw_emo = EMOTION_NAME_MAP[raw_emo]
                if raw_emo in VALID_EMOTIONS:
                    self.emotion_extracted = raw_emo

            # Regex for tts block in unclosed JSON
            tts_match = re.search(r'["\']?tts["\']?\s*:\s*\{([^}]*)', sanitized)
            if tts_match:
                tts_block = tts_match.group(1)
                sp_match = re.search(r'["\']?(?:speed|speed_factor)["\']?\s*:\s*["\']?(-?[0-9]*\.?[0-9]+)["\']?', tts_block)
                if sp_match:
                    try:
                        raw_sp = float(sp_match.group(1))
                        self.tts_speed = clamp_dynamic_speed(raw_sp)
                        self.tts_params["speed"] = self.tts_speed
                    except (ValueError, TypeError):
                        pass
                temp_match = re.search(r'["\']?(?:temp|temperature)["\']?\s*:\s*["\']?(-?[0-9]*\.?[0-9]+)["\']?', tts_block)
                if temp_match:
                    try:
                        raw_temp = float(temp_match.group(1))
                        self.tts_temperature = clamp_dynamic_temperature(raw_temp)
                        self.tts_params["temperature"] = self.tts_temperature
                        self.tts_params["temp"] = self.tts_temperature
                    except (ValueError, TypeError):
                        pass
                emo_match_tts = re.search(r'["\']?emotion["\']?\s*:\s*["\']?([a-zA-Z\u4e00-\u9fa5]+)["\']?', tts_block)
                if emo_match_tts:
                    raw_emo = emo_match_tts.group(1).lower()
                    if raw_emo in EMOTION_NAME_MAP:
                        raw_emo = EMOTION_NAME_MAP[raw_emo]
                    if raw_emo in VALID_EMOTIONS:
                        self.tts_emotion = raw_emo
                        self.emotion_extracted = raw_emo
                        self.tts_params["emotion"] = raw_emo

            # Fallback for structured text without valid JSON
            if not self.chinese_extracted and not self.japanese_extracted:
                ch_fallback = re.search(r'(?:中文|Chinese)[:：]\s*(.*?)(?:(?:日文|Japanese)[:：]|$)', sanitized, flags=re.DOTALL | re.IGNORECASE)
                ja_fallback = re.search(r'(?:日文|Japanese)[:：]\s*(.*)$', sanitized, flags=re.DOTALL | re.IGNORECASE)
                if ch_fallback:
                    self.chinese_extracted = ch_fallback.group(1).strip()
                if ja_fallback:
                    self.japanese_extracted = ja_fallback.group(1).strip()
                if not self.chinese_extracted:
                    self.chinese_extracted = sanitized
                if not self.japanese_extracted:
                    self.japanese_extracted = self.chinese_extracted

        self.emotion_extracted = classify_emotion(self.chinese_extracted, self.japanese_extracted, self.emotion_extracted)

        remaining_sentences: List[str] = []
        if self.japanese_extracted:
            is_first = not self.first_sentence_emitted
            all_sentences = split_japanese_sentences(self.japanese_extracted, is_first_chunk=is_first)
            emitted_so_far = self.emitted_japanese_len
            full_ja_text = "".join(all_sentences)
            if len(full_ja_text) > emitted_so_far:
                rem_text = full_ja_text[emitted_so_far:]
                if rem_text.strip():
                    remaining_sentences = split_japanese_sentences(rem_text, is_first_chunk=not self.first_sentence_emitted)
                    if remaining_sentences:
                        self.first_sentence_emitted = True

        return self.chinese_extracted, self.japanese_extracted, remaining_sentences
