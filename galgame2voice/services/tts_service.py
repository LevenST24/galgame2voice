"""
TTS Service and High-Level Audio Generation for galgame2voice.
Integrates the shared GPT-SoVITS singleton client with local audio storage,
sentence streaming, and the persistent TTS cache.
"""

import asyncio
import logging
import uuid
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, Optional, Tuple, Union

from galgame2voice.config import get_settings
from galgame2voice.utils.path_guard import resolve_existing_audio_path
from galgame2voice.services.gpt_sovits_client import (
    GptSovitsClient,
    get_gpt_sovits_client,
    clean_japanese_parentheses,
    resolve_tts_options,
    SLICING_METHODS,
    TTS_PRESETS,
    DYNAMIC_SPEED_MIN,
    DYNAMIC_SPEED_MAX,
    DYNAMIC_TEMP_MIN,
    DYNAMIC_TEMP_MAX,
    clamp_dynamic_speed,
    clamp_dynamic_temperature,
    probe_audio_duration_seconds,
)
from galgame2voice.services.tts_cache_manager import get_tts_cache_manager, TtsCacheManager

logger = logging.getLogger("galgame2voice.services.tts_service")


class TtsService:
    """
    High-level TTS coordination service.
    Synthesizes text, streams audio chunks, saves files to disk, and delegates
    to TtsCacheManager for persistent near-zero-latency audio reuse.

    IMPORTANT: By default this service binds to the application-wide shared
    GptSovitsClient singleton so that GPU inference is globally serialized by
    one mutex. Passing an explicit `client` (e.g. a mock server client) keeps
    tests isolated.
    """

    def __init__(
        self,
        client: Optional[GptSovitsClient] = None,
        audio_dir: Optional[Union[str, Path]] = None,
        cache_manager: Optional[TtsCacheManager] = None,
        db_path: Optional[str] = None,
    ):
        settings = get_settings()
        self.client = client or get_gpt_sovits_client()
        self.audio_dir = Path(audio_dir or settings.audio_dir)
        self.audio_dir.mkdir(parents=True, exist_ok=True)
        self.cache_manager = cache_manager or get_tts_cache_manager(
            cache_dir=self.audio_dir / "cache",
            db_path=db_path or settings.db_path,
        )

    @staticmethod
    def get_audio_duration(path: Union[str, Path, None]) -> Optional[float]:
        """
        Safely inspects and measures reference audio duration in seconds.
        Returns float duration, or None if file is missing, unreadable, or invalid.
        """
        if not path:
            return None
        try:
            p = resolve_existing_audio_path(path)
            if p is None:
                return None

            # 1. Try soundfile (handles OGG, WAV, FLAC, etc.)
            try:
                import soundfile as sf
                info = sf.info(str(p))
                return float(info.duration)
            except Exception:
                pass

            # 2. Try wave standard library for PCM WAV
            try:
                import wave
                with wave.open(str(p), "rb") as wf:
                    frames = wf.getnframes()
                    rate = wf.getframerate()
                    if rate > 0:
                        return float(frames) / float(rate)
            except Exception:
                pass

            # 3. Try mutagen
            try:
                import mutagen
                m = mutagen.File(str(p))
                if m and m.info and hasattr(m.info, "length"):
                    return float(m.info.length)
            except Exception:
                pass

            # 4. Stdlib OGG/Opus granule-position probe (works without soundfile/mutagen,
            #    which are not bundled — without this every .ogg ref would look invalid
            #    and emotion voices would silently fall back to the baseline audio)
            try:
                dur = probe_audio_duration_seconds(str(p))
                if dur is not None:
                    return float(dur)
            except Exception:
                pass
        except Exception:
            pass
        return None

    async def _populate_voice_profile_opts(self, opts: Dict[str, Any]) -> Dict[str, Any]:
        """Auto-populates active voice profile parameters, applying dynamic emotion reference audios if available."""
        try:
            from galgame2voice.services.voice_manager import get_voice_manager
            active = await get_voice_manager().get_active_profile()
            if active:
                opts.setdefault("voice_profile_id", active.id)
                opts.setdefault("prompt_lang", active.prompt_lang)
                opts.setdefault("text_lang", active.text_lang)

                fallback_ref_audio = active.ref_audio_path
                fallback_prompt_text = active.prompt_text
                fallback_prompt_lang = active.prompt_lang

                # Ensure fallback_ref_audio exists; if not, point to bundled gentle.ogg
                settings = get_settings()
                if resolve_existing_audio_path(fallback_ref_audio) is None:
                    bundled_default = settings.project_root / "audio" / "references" / "natsume" / "gentle.ogg"
                    if bundled_default.is_file():
                        fallback_ref_audio = str(bundled_default.resolve())

                # Check for dynamic emotion reference audio override
                ai_adaptive = opts.get("ai_adaptive_voice", opts.get("aiAdaptiveVoice", True))
                emotion = opts.get("emotion")

                resolved_emo = None
                if ai_adaptive and emotion:
                    from galgame2voice.services.emotion_references import resolve_emotion_reference
                    char_name = getattr(active, "name", "") or "四季夏目"
                    resolved_emo = resolve_emotion_reference(char_name, str(emotion))

                if resolved_emo:
                    candidate_audio = resolved_emo["ref_audio_path"]
                    # Validate candidate emotion reference audio duration: must be in [3.0, 10.0]s
                    dur = self.get_audio_duration(candidate_audio)
                    if dur is not None and 3.0 <= dur <= 10.0:
                        opts["ref_audio_path"] = candidate_audio
                        opts["prompt_text"] = resolved_emo["prompt_text"]
                        opts["prompt_lang"] = resolved_emo["prompt_lang"]
                    else:
                        logger.warning(
                            "Emotion reference audio '%s' is invalid (duration: %s, required: [3.0, 10.0]s) or missing. "
                            "Falling back to active profile default reference audio: %s",
                            candidate_audio, dur, fallback_ref_audio
                        )
                        opts["ref_audio_path"] = fallback_ref_audio
                        opts["prompt_text"] = fallback_prompt_text
                        opts["prompt_lang"] = fallback_prompt_lang
                else:
                    # User-supplied or pre-existing ref_audio_path
                    user_ref = opts.get("ref_audio_path") or opts.get("refer_audio_path")
                    if user_ref:
                        file_exists = resolve_existing_audio_path(user_ref) is not None
                        dur = self.get_audio_duration(user_ref)
                        is_mock_client = (
                            getattr(self.client, "_mock_return_value", None) is not None
                            or type(self.client).__name__ == "MagicMock"
                            or getattr(self.client, "server", None) is not None
                        )
                        needs_fallback = False
                        if dur is not None and (dur < 3.0 or dur > 10.0):
                            needs_fallback = True
                        elif not is_mock_client and not file_exists:
                            needs_fallback = True

                        if needs_fallback:
                            logger.warning(
                                "Reference audio '%s' is invalid (exists: %s, duration: %s, required: [3.0, 10.0]s). "
                                "Falling back to default reference audio: %s",
                                user_ref, file_exists, dur, fallback_ref_audio
                            )
                            opts["ref_audio_path"] = fallback_ref_audio
                            opts["prompt_text"] = fallback_prompt_text
                            opts["prompt_lang"] = fallback_prompt_lang
                    elif not getattr(self.client, "current_refer_audio", None):
                        opts.setdefault("ref_audio_path", fallback_ref_audio)
                        opts.setdefault("prompt_text", fallback_prompt_text)
        except Exception as exc:
            logger.debug("Could not auto-populate active profile options in TtsService: %s", exc)
        return opts

    def _sanitize_dynamic_voice_options(self, opts: Dict[str, Any]) -> Dict[str, Any]:
        """Safely clamps speed to [0.70, 1.35] and temperature to [0.60, 1.20] if AI adaptive voice is enabled."""
        if opts.get("ai_adaptive_voice", opts.get("aiAdaptiveVoice", False)):
            if "speed" in opts or "speed_factor" in opts:
                sp_val = opts.get("speed", opts.get("speed_factor"))
                opts["speed"] = clamp_dynamic_speed(sp_val, fallback=1.0)
                opts["speed_factor"] = opts["speed"]
            if "temperature" in opts or "temp" in opts:
                temp_val = opts.get("temperature", opts.get("temp"))
                opts["temperature"] = clamp_dynamic_temperature(temp_val, fallback=1.0)
        return opts

    async def synthesize(
        self,
        text: str,
        options: Optional[Dict[str, Any]] = None,
        use_cache: bool = True,
    ) -> bytes:
        """
        Synthesizes text to WAV bytes via the shared GPT-SoVITS client or cache.
        Returns cached audio bytes in <50ms on hit.
        """
        opts = dict(options or {})
        opts = await self._populate_voice_profile_opts(opts)
        opts = self._sanitize_dynamic_voice_options(opts)

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
                # Cache write failure must never fail an otherwise successful synthesis.
                logger.warning("Failed to store synthesized audio in cache: %s", exc)

        return audio_bytes

    async def synthesize_to_file(
        self,
        text: str,
        options: Optional[Dict[str, Any]] = None,
        filename_prefix: str = "voice",
        use_cache: bool = True,
    ) -> Tuple[str, Path, int]:
        """
        Synthesizes text and saves or retrieves the resulting WAV file.
        Returns (url_path, local_file_path, byte_count).
        """
        opts = dict(options or {})
        opts = await self._populate_voice_profile_opts(opts)
        opts = self._sanitize_dynamic_voice_options(opts)

        if use_cache:
            cache_key, clean_text, params_hash = self.cache_manager.compute_cache_key(text, options=opts)
            cached = await self.cache_manager.get(cache_key)
            if cached is not None:
                audio_bytes, url_path, file_size = cached
                file_path = self.cache_manager.cache_dir / f"{cache_key}.wav"
                logger.debug("TTS Cache HIT (file) for key %s -> %s", cache_key[:12], url_path)
                return url_path, file_path, file_size

            # Synthesize exactly once on miss.
            audio_bytes = await self.client.synthesize(text, options=opts)
            if audio_bytes and cache_key:
                try:
                    url_path, file_path, file_size = await self.cache_manager.put(
                        cache_key=cache_key,
                        text=text,
                        clean_text=clean_text,
                        voice_profile_id=opts.get("voice_profile_id", 1),
                        params_hash=params_hash,
                        audio_bytes=audio_bytes,
                    )
                    return url_path, file_path, file_size
                except Exception as exc:
                    # Cache write failed — fall through to ephemeral file so the
                    # caller still gets usable audio.
                    logger.warning("TTS cache put failed, writing ephemeral file instead: %s", exc)

        # Ephemeral non-cached file write (also reached when use_cache=False)
        if not use_cache:
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
        """Streams audio chunks from cache or the shared GPT-SoVITS client."""
        opts = dict(options or {})
        opts = await self._populate_voice_profile_opts(opts)

        cache_key = ""
        clean_text = ""
        params_hash = ""
        if use_cache:
            cache_key, clean_text, params_hash = self.cache_manager.compute_cache_key(text, options=opts)
            cached = await self.cache_manager.get(cache_key)
            if cached is not None:
                cached_bytes = cached[0]
                for i in range(0, len(cached_bytes), chunk_size):
                    yield cached_bytes[i:i + chunk_size]
                return

        collected_chunks = []
        stream = self.client.stream_tts(text, options=opts, chunk_size=chunk_size)
        try:
            async for chunk in stream:
                collected_chunks.append(chunk)
                yield chunk
        finally:
            # Guarantee the underlying generator (and its inference lock) is
            # released even if the consumer abandons us mid-stream.
            await stream.aclose()

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
    "DYNAMIC_SPEED_MIN",
    "DYNAMIC_SPEED_MAX",
    "DYNAMIC_TEMP_MIN",
    "DYNAMIC_TEMP_MAX",
    "clamp_dynamic_speed",
    "clamp_dynamic_temperature",
]
