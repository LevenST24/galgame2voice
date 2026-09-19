"""
TTS Stream Pipeline for Galgame2Voice.
Encapsulates priority synthesis scheduling, profile options routing,
single-flight deduplication, and error isolation.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Dict, Optional, Tuple

from galgame2voice.services.tts_service import TtsService
from galgame2voice.utils.logger import sanitize_error_detail
from galgame2voice.services.gpt_sovits_client import clean_japanese_parentheses
from galgame2voice.utils.profiler import ChatTurnProfiler

logger = logging.getLogger("galgame2voice.services.chat_pipelines.tts_pipeline")

_VOCAL_RE = re.compile(r'[\w\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff]')


class TtsStreamPipeline:
    """
    Manages audio synthesis for sentence chunks produced by the chat parser.
    """

    def __init__(self, tts_service: TtsService, generation_id: str):
        self.tts_service = tts_service
        self.generation_id = generation_id

    @staticmethod
    def is_vocal_sentence(sentence: str) -> bool:
        """Determines if a sentence contains speakable phonetic content."""
        if not sentence or not sentence.strip():
            return False
        cleaned = clean_japanese_parentheses(sentence).strip()
        if not cleaned:
            return False
        return bool(_VOCAL_RE.search(sentence))

    def prepare_chunk_options(
        self,
        base_options: Optional[Dict[str, Any]],
        active_profile: Optional[Any],
        chunk_index: int,
        sentence: str,
    ) -> Dict[str, Any]:
        """Prepares options dictionary for a specific sentence chunk."""
        opts = dict(base_options) if base_options else {}
        if active_profile:
            is_dict = isinstance(active_profile, dict)
            prof_id = active_profile.get("id") if is_dict else getattr(active_profile, "id", None)
            if prof_id is not None:
                opts.setdefault("voice_profile_id", prof_id)

            char_name = active_profile.get("name") if is_dict else getattr(active_profile, "name", None)
            if char_name:
                opts.setdefault("character_name", char_name)

            p_lang = active_profile.get("prompt_lang") if is_dict else getattr(active_profile, "prompt_lang", None)
            opts.setdefault("prompt_lang", p_lang or "ja")

            t_lang = active_profile.get("text_lang") if is_dict else getattr(active_profile, "text_lang", None)
            opts.setdefault("text_lang", t_lang or "ja")

            ref_path = active_profile.get("ref_audio_path") if is_dict else getattr(active_profile, "ref_audio_path", None)
            if ref_path:
                opts.setdefault("ref_audio_path", ref_path)

            prompt_text = active_profile.get("prompt_text") if is_dict else getattr(active_profile, "prompt_text", None)
            if prompt_text:
                opts.setdefault("prompt_text", prompt_text)

        user_split = opts.get("text_split_method") or opts.get("cut_option") or opts.get("how_to_cut")
        if not user_split:
            opts["text_split_method"] = "cut0" if len(sentence.strip()) <= 80 else "cut2"

        opts["_generation_id"] = self.generation_id
        opts["_priority"] = 0 if chunk_index == 0 else 1
        return opts

    async def synthesize_chunk(
        self,
        sentence: str,
        chunk_index: int,
        options: Dict[str, Any],
        profiler: Optional[ChatTurnProfiler] = None,
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """
        Synthesizes a single vocal sentence chunk.
        Returns:
            (chunk_data, None) on success
            (None, error_str) on failure
        """
        if profiler:
            profiler.record_tts_dispatch(chunk_index)
            options["profiler"] = profiler

        try:
            audio_url, local_path, _ = await self.tts_service.synthesize_to_file(
                sentence,
                options=options,
                filename_prefix=f"chunk_{chunk_index}",
            )
            is_cached = "/audio/cache/" in str(audio_url)
            if profiler:
                profiler.record_tts_inference(chunk_index, cached=is_cached)
                profiler.record_first_audio()

            chunk_data = {
                "index": chunk_index,
                "audio_url": audio_url,
                "sentence": sentence,
                "local_path": str(local_path),
                "is_cached": is_cached,
            }
            return chunk_data, None
        except asyncio.CancelledError:
            logger.info("TTS synthesis cancelled for chunk %d (generation %s)", chunk_index, self.generation_id)
            raise
        except Exception as exc:
            logger.warning(
                "Failed to synthesize audio chunk %d for sentence '%s': %s",
                chunk_index, sentence, exc,
            )
            safe_err = sanitize_error_detail(str(exc)[:200])
            return None, safe_err or "TTS synthesis failed"
