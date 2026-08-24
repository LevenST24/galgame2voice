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
from galgame2voice.services.tts_cache_manager import get_tts_cache_manager, TtsCacheManager

logger = logging.getLogger("galgame2voice.services.tts_service")


class TtsService:
    """
    High-level TTS coordination service.
    Handles synthesizing text, streaming audio chunks, saving audio files to disk,
    and delegating to TtsCacheManager for persistent zero-latency (<50ms) audio reuse.
    """

    def __init__(
        self,
        client: Optional[GptSovitsClient] = None,
        audio_dir: Optional[Union[str, Path]] = None,
        cache_manager: Optional[TtsCacheManager] = None,
    ):
        settings = get_settings()
        self.client = client or GptSovitsClient(base_url=settings.gpt_sovits_base_url)
        self.audio_dir = Path(audio_dir or settings.audio_dir)
        self.audio_dir.mkdir(parents=True, exist_ok=True)
        self.cache_manager = cache_manager or get_tts_cache_manager(
            cache_dir=self.audio_dir / "cache",
            db_path=settings.db_path,
        )

    async def _populate_voice_profile_opts(self, opts: Dict[str, Any]) -> Dict[str, Any]:
        """Auto-populates active voice profile parameters if missing."""
        if not opts.get("ref_audio_path") and not opts.get("refer_audio_path") and not self.client.current_refer_audio:
            try:
                from galgame2voice.services.voice_manager import get_voice_manager
                active = await get_voice_manager().get_active_profile()
                if active:
                    opts.setdefault("voice_profile_id", active.id)
                    opts.setdefault("ref_audio_path", active.ref_audio_path)
                    opts.setdefault("prompt_text", active.prompt_text)
                    opts.setdefault("prompt_lang", active.prompt_lang)
                    opts.setdefault("text_lang", active.text_lang)
            except Exception as exc:
                logger.debug("Could not auto-populate active profile options in TtsService: %s", exc)
        return opts

    async def synthesize(
        self,
        text: str,
        options: Optional[Dict[str, Any]] = None,
        use_cache: bool = True,
    ) -> bytes:
        """
        Synthesizes text to WAV bytes via GptSovitsClient or local persistent cache.
        Returns cached audio bytes in <50ms on hit.
        """
        opts = dict(options or {})
        opts = await self._populate_voice_profile_opts(opts)

        cache_key = ""
        clean_text = ""
        params_hash = ""
        if use_cache:
            cache_key, clean_text, params_hash = self.cache_manager.compute_cache_key(text, options=opts)
            cached = await self.cache_manager.get(cache_key)
            if cached is not None:
                logger.debug("TTS Cache HIT for key %s ('%s')", cache_key[:12], clean_text[:20])
                return cached[0]

        logger.debug("TTS Cache MISS for key %s ('%s'), invoking GPU synthesis", cache_key[:12] if cache_key else "none", text[:20])
        audio_bytes = await self.client.synthesize(text, options=opts)

        if use_cache and audio_bytes and cache_key:
            try:
                await self.cache_manager.put(
                    cache_key=cache_key,
                    text=text,
                    clean_text=clean_text,
                    voice_profile_id=opts.get("voice_profile_id", 1),
                    params_hash=params_hash,
                    audio_bytes=audio_bytes,
                )
            except Exception as exc:
                logger.warning("Failed to store synthesized audio in cache: %s", exc)

        return audio_bytes

    async def synthesize_to_file(
        self,
        text: str,
        options: Optional[Dict[str, Any]] = None,
        filename_prefix: str = "voice",
        use_cache: bool = True,
    ) -> tuple[str, Path, int]:
        """
        Synthesizes text and saves or retrieves the resulting WAV file.
        Returns (url_path, local_file_path, byte_count).
        """
        opts = dict(options or {})
        opts = await self._populate_voice_profile_opts(opts)

        if use_cache:
            cache_key, clean_text, params_hash = self.cache_manager.compute_cache_key(text, options=opts)
            cached = await self.cache_manager.get(cache_key)
            if cached is not None:
                audio_bytes, url_path, file_size = cached
                file_path = self.cache_manager.cache_dir / f"{cache_key}.wav"
                logger.debug("TTS Cache HIT (file) for key %s -> %s", cache_key[:12], url_path)
                return url_path, file_path, file_size

            # Synthesize bytes on miss
            audio_bytes = await self.client.synthesize(text, options=opts)
            if audio_bytes and cache_key:
                url_path, file_path, file_size = await self.cache_manager.put(
                    cache_key=cache_key,
                    text=text,
                    clean_text=clean_text,
                    voice_profile_id=opts.get("voice_profile_id", 1),
                    params_hash=params_hash,
                    audio_bytes=audio_bytes,
                )
                return url_path, file_path, file_size

        # Fallback to non-cached ephemeral file write
        audio_bytes = await self.client.synthesize(text, options=opts)
        filename = f"{filename_prefix}_{uuid.uuid4().hex[:12]}.wav"
        file_path = self.audio_dir / filename
        await asyncio.to_thread(file_path.write_bytes, audio_bytes)
        url_path = f"/audio/{filename}"
        return url_path, file_path, len(audio_bytes)

    async def stream_tts(
        self,
        text: str,
        options: Optional[Dict[str, Any]] = None,
        chunk_size: int = 4096,
        use_cache: bool = True,
    ) -> AsyncGenerator[bytes, None]:
        """Streams audio chunks from cache or GptSovitsClient."""
        opts = dict(options or {})
        opts = await self._populate_voice_profile_opts(opts)

        if use_cache:
            cache_key, clean_text, params_hash = self.cache_manager.compute_cache_key(text, options=opts)
            cached = await self.cache_manager.get(cache_key)
            if cached is not None:
                cached_bytes = cached[0]
                for i in range(0, len(cached_bytes), chunk_size):
                    yield cached_bytes[i:i + chunk_size]
                return

        collected_chunks = []
        async for chunk in self.client.stream_tts(text, options=opts, chunk_size=chunk_size):
            collected_chunks.append(chunk)
            yield chunk

        if use_cache and collected_chunks and cache_key:
            full_bytes = b"".join(collected_chunks)
            if full_bytes:
                try:
                    await self.cache_manager.put(
                        cache_key=cache_key,
                        text=text,
                        clean_text=clean_text,
                        voice_profile_id=opts.get("voice_profile_id", 1),
                        params_hash=params_hash,
                        audio_bytes=full_bytes,
                    )
                except Exception as exc:
                    logger.debug("Failed to cache streamed TTS chunks: %s", exc)


__all__ = [
    "GptSovitsClient",
    "TtsService",
    "clean_japanese_parentheses",
    "resolve_tts_options",
    "SLICING_METHODS",
    "TTS_PRESETS",
]

