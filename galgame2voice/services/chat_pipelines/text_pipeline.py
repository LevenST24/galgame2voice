"""
Text Segmentation Pipeline for Galgame2Voice.
Encapsulates real-time bilingual token parsing, agile first-sentence chunking,
emotion classification, and pause/prosody normalization.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from galgame2voice.services.streaming_parser import StreamingBilingualParser
from galgame2voice.services.emotion_classifier import classify_emotion

logger = logging.getLogger("galgame2voice.services.chat_pipelines.text_pipeline")


class TextSegmentationPipeline:
    """
    Parses an incoming LLM token stream in real time into:
    1. Incremental Chinese tokens (for immediate frontend subtitle display)
    2. Segmented Japanese sentences (for pipelined TTS generation)
    3. Emotion metadata
    """

    def __init__(self, parser: Optional[StreamingBilingualParser] = None) -> None:
        self.parser = parser or StreamingBilingualParser()

    def feed_token(self, token: str) -> Tuple[str, List[str], Optional[str]]:
        """
        Feeds an LLM token into the parser.
        Returns:
            delta_chinese: New Chinese characters ready for display
            completed_sentences: List of completed Japanese sentences ready for TTS
            current_emotion: Detected or classified emotion tag
        """
        delta_ch, completed_sentences = self.parser.feed_chunk(token)
        emotion = self.parser.emotion_extracted
        if delta_ch and not emotion:
            emotion = classify_emotion(
                self.parser.chinese_extracted,
                self.parser.japanese_extracted,
            )
        return delta_ch, completed_sentences, emotion

    def finalize(self) -> Tuple[str, str, str, List[str], Optional[str]]:
        """
        Finalizes the text stream.
        Returns:
            full_chinese: Complete Chinese text
            full_japanese: Complete Japanese text
            remaining_chinese: Any un-emitted trailing Chinese text
            remaining_sentences: Trailing completed Japanese sentences
            final_emotion: Final classified emotion tag
        """
        full_ch, full_ja, rem_sentences = self.parser.finalize()
        rem_ch = ""
        if len(full_ch) > self.parser.emitted_chinese_len:
            rem_ch = full_ch[self.parser.emitted_chinese_len:]

        final_emotion = self.parser.emotion_extracted or classify_emotion(full_ch, full_ja)
        return full_ch, full_ja, rem_ch, rem_sentences, final_emotion

    def get_dynamic_tts_options(
        self,
        base_options: Optional[Dict[str, Any]] = None,
        adaptive_enabled: bool = False,
        sentence_text: str = "",
    ) -> Dict[str, Any]:
        """Calculates emotion-aware, dynamic speed/pitch/prompt TTS options."""
        return self.parser.get_dynamic_tts_options(
            base_options=base_options,
            adaptive_enabled=adaptive_enabled,
            sentence_text=sentence_text,
        )

    @property
    def emotion_extracted(self) -> Optional[str]:
        return self.parser.emotion_extracted

    @property
    def chinese_extracted(self) -> str:
        return self.parser.chinese_extracted

    @property
    def japanese_extracted(self) -> str:
        return self.parser.japanese_extracted

    @property
    def tts_speed(self) -> Optional[float]:
        return self.parser.tts_speed

    @property
    def tts_temperature(self) -> Optional[float]:
        return self.parser.tts_temperature

    @property
    def tts_emotion(self) -> Optional[str]:
        return self.parser.tts_emotion
