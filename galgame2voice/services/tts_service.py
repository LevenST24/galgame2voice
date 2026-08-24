"""
TTS Service and High-Level Audio Generation for galgame2voice.
Integrates GPT-SoVITS Client with local audio storage and sentence streaming.
"""

import asyncio
import logging
import os
import uuid
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional, Union

from galgame2voice.config import get_settings
from galgame2voice.services.gpt_sovits_client import (
    GptSovitsClient,
    clean_japanese_parentheses,
    resolve_tts_options,
    SLICING_METHODS,
    TTS_PRESETS,
)

logger = logging.getLogger("galgame2voice.services.tts_service")


class TtsService:
    """
    High-level TTS coordination service.
    Handles synthesizing text, streaming audio chunks, and saving audio files to disk.
    """

    def __init__(
        self,
        client: Optional[GptSovitsClient] = None,
        audio_dir: Optional[Union[str, Path]] = None,
    ):
        settings = get_settings()
        self.client = client or GptSovitsClient(base_url=settings.gpt_sovits_base_url)
        self.audio_dir = Path(audio_dir or settings.audio_dir)
        self.audio_dir.mkdir(parents=True, exist_ok=True)

    async def synthesize(self, text: str, options: Optional[Dict[str, Any]] = None) -> bytes:
        """Synthesizes text to WAV bytes via GptSovitsClient with active profile fallback."""
        opts = dict(options or {})
        if not opts.get("ref_audio_path") and not opts.get("refer_audio_path") and not self.client.current_refer_audio:
            try:
                from galgame2voice.services.voice_manager import get_voice_manager
                active = await get_voice_manager().get_active_profile()
                if active:
                    opts.setdefault("ref_audio_path", active.ref_audio_path)
                    opts.setdefault("prompt_text", active.prompt_text)
                    opts.setdefault("prompt_lang", active.prompt_lang)
                    opts.setdefault("text_lang", active.text_lang)
            except Exception as exc:
                logger.debug("Could not auto-populate active profile options in TtsService: %s", exc)

        return await self.client.synthesize(text, options=opts)

    async def synthesize_to_file(
        self,
        text: str,
        options: Optional[Dict[str, Any]] = None,
        filename_prefix: str = "voice",
    ) -> tuple[str, Path, int]:
        """
        Synthesizes text and saves the resulting WAV to disk in audio_dir.
        Returns (url_path, local_file_path, byte_count).
        """
        audio_bytes = await self.synthesize(text, options=options)
        filename = f"{filename_prefix}_{uuid.uuid4().hex[:12]}.wav"
        file_path = self.audio_dir / filename
        
        # Write bytes asynchronously using asyncio.to_thread
        await asyncio.to_thread(file_path.write_bytes, audio_bytes)
        url_path = f"/audio/{filename}"
        return url_path, file_path, len(audio_bytes)

    async def stream_tts(
        self,
        text: str,
        options: Optional[Dict[str, Any]] = None,
        chunk_size: int = 4096,
    ) -> AsyncGenerator[bytes, None]:
        """Streams audio chunks from GptSovitsClient."""
        async for chunk in self.client.stream_tts(text, options=options, chunk_size=chunk_size):
            yield chunk


__all__ = [
    "GptSovitsClient",
    "TtsService",
    "clean_japanese_parentheses",
    "resolve_tts_options",
    "SLICING_METHODS",
    "TTS_PRESETS",
]
