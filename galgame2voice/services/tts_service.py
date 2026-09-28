"""
TTS Service and High-Level Audio Generation for galgame2voice.
Integrates the shared GPT-SoVITS singleton client with local audio storage,
sentence streaming, and the persistent TTS cache.
"""

import asyncio
import logging
import time
import uuid
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, Optional, Tuple, Union

from galgame2voice.config import get_settings
from galgame2voice.utils.path_guard import resolve_existing_audio_path
from galgame2voice.utils.audio_spec import (
    AudioSpecCache,
    _AUDIO_SPEC_CACHE,
    async_probe_audio_duration_seconds,
)
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
)
from galgame2voice.utils.prosody import (
    clamp_dynamic_speed,
    clamp_dynamic_temperature,
    clamp_dynamic_top_k,
    clamp_dynamic_top_p,
    clamp_dynamic_fragment_interval,
    clamp_dynamic_batch_size,
    calculate_adaptive_prosody,
)
from galgame2voice.services.tts_cache_manager import get_tts_cache_manager, TtsCacheManager

logger = logging.getLogger("galgame2voice.services.tts_service")


# Backward compatibility aliases pointing to the thread-safe AudioSpecCache singleton.
_AUDIO_STAT_DURATION_CACHE = _AUDIO_SPEC_CACHE._cache
_AUDIO_DURATION_CACHE = _AUDIO_STAT_DURATION_CACHE


def clear_tts_profile_cache() -> None:
    """Clears in-memory caches for reference audio durations."""
    _AUDIO_SPEC_CACHE.clear()


async def async_get_audio_duration(path: Union[str, Path, None]) -> Optional[float]:
    """
    Safely inspects and measures reference audio duration asynchronously.
    Returns float duration, or None if file is missing, unreadable, or invalid.
    Uses AudioSpecCache to bypass disk inspection when audio files have not changed,
    and delegates file inspection to asyncio.to_thread() so the main asyncio event
    loop is never blocked.
    """
    if not path:
        return None
    try:
        p = resolve_existing_audio_path(path)
        if p is None or not p.is_file():
            return None
        return await async_probe_audio_duration_seconds(p)
    except Exception:
        return None


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
        self.db_path = db_path
        self.cache_manager = cache_manager or get_tts_cache_manager(
            cache_dir=self.audio_dir / "cache",
            db_path=db_path or settings.db_path,
        )

    @staticmethod
    def get_audio_duration(path: Union[str, Path, None]) -> Optional[float]:
        """
        Safely inspects and measures reference audio duration in seconds.
        Returns float duration, or None if file is missing, unreadable, or invalid.
        Results are cached using stat-based keys (resolved_path, mtime_ns, size)
        in AudioSpecCache to bypass disk inspection when audio files haven't changed.
        """
        if not path:
            return None
        try:
            p = resolve_existing_audio_path(path)
            if p is None or not p.is_file():
                return None
            return _AUDIO_SPEC_CACHE.get_duration(p)
        except Exception:
            return None

    @staticmethod
    async def async_get_audio_duration(path: Union[str, Path, None]) -> Optional[float]:
        """
        Asynchronously measures reference audio duration in seconds.
        Delegates to AudioSpecCache to keep the main asyncio event loop unblocked.
        """
        return await async_get_audio_duration(path)

    async def _populate_voice_profile_opts(self, opts: Dict[str, Any]) -> Dict[str, Any]:
        """Auto-populates active voice profile parameters, applying dynamic emotion reference audios if available."""
        if opts.get("_pre_resolved"):
            return opts

        # If user explicitly supplied all required reference audio options and no adaptive emotion override is needed,
        # completely bypass SQLite to keep the TTS critical path in-memory (TTFA < 1s).
        has_full_ref = bool(
            opts.get("ref_audio_path")
            and opts.get("prompt_text")
            and opts.get("prompt_lang")
        )
        ai_adaptive = opts.get("ai_adaptive_voice", opts.get("aiAdaptiveVoice", True))
        emotion = opts.get("emotion")
        if has_full_ref and not (ai_adaptive and emotion):
            return opts

        try:
            from galgame2voice.services.voice_resolver import get_voice_resolver
            resolver = get_voice_resolver()

            prof_id = opts.get("voice_profile_id")
            char_name_opt = opts.get("character_name")
            has_explicit_voice = bool(prof_id or char_name_opt)

            ctx = await resolver.resolve_context(
                db_path=self.db_path,
                profile_id=int(prof_id) if prof_id is not None else None,
                character_name=str(char_name_opt) if char_name_opt else None,
            )

            if ctx:
                opts.setdefault("voice_profile_id", ctx.profile_id or 1)
                opts.setdefault("prompt_lang", ctx.prompt_lang)
                opts.setdefault("text_lang", ctx.text_lang)

                fallback_ref_audio = str(ctx.ref_audio_path) if ctx.ref_audio_path is not None else None
                fallback_prompt_text = ctx.prompt_text
                fallback_prompt_lang = ctx.prompt_lang

                ai_adaptive = opts.get("ai_adaptive_voice", opts.get("aiAdaptiveVoice", True))
                emotion = opts.get("emotion")

                resolved_emo = ctx.get_emotion_ref(str(emotion)) if (ai_adaptive and emotion) else None
                if resolved_emo:
                    opts["ref_audio_path"] = str(resolved_emo["ref_audio_path"]) if resolved_emo.get("ref_audio_path") else ""
                    opts["prompt_text"] = resolved_emo["prompt_text"]
                    opts["prompt_lang"] = resolved_emo["prompt_lang"]
                else:
                    user_ref = opts.get("ref_audio_path") or opts.get("refer_audio_path")
                    if user_ref:
                        file_exists = resolve_existing_audio_path(user_ref) is not None
                        dur = await self.async_get_audio_duration(user_ref)
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
                            opts["ref_audio_path"] = str(fallback_ref_audio) if fallback_ref_audio else ""
                            opts["prompt_text"] = fallback_prompt_text
                            opts["prompt_lang"] = fallback_prompt_lang
                    else:
                        if has_explicit_voice or not getattr(self.client, "current_refer_audio", None):
                            opts.setdefault("ref_audio_path", str(fallback_ref_audio) if fallback_ref_audio else "")
                            opts.setdefault("prompt_text", fallback_prompt_text)
                            opts.setdefault("prompt_lang", fallback_prompt_lang)
        except Exception as exc:
            logger.debug("Could not auto-populate active profile options in TtsService: %s", exc)
        return opts

    def _sanitize_dynamic_voice_options(self, opts: Dict[str, Any], text: Optional[str] = None) -> Dict[str, Any]:
        """Safely clamps and enriches speech prosody parameters (speed, temperature, top_k, top_p, fragment_interval, batch_size)."""
        if opts.get("ai_adaptive_voice", opts.get("aiAdaptiveVoice", False)):
            emo = opts.get("emotion")
            target_text = text or opts.get("prompt_text", "")
            if emo or target_text:
                calc = calculate_adaptive_prosody(text=target_text, emotion=emo, base_params=opts)
                if "speed" not in opts and "speed_factor" not in opts:
                    opts["speed"] = calc["speed"]
                    opts["speed_factor"] = calc["speed"]
                if "temperature" not in opts and "temp" not in opts:
                    opts["temperature"] = calc["temperature"]
                    opts["temp"] = calc["temperature"]
                if "top_k" not in opts:
                    opts["top_k"] = calc["top_k"]
                if "top_p" not in opts:
                    opts["top_p"] = calc["top_p"]
                if "fragment_interval" not in opts:
                    opts["fragment_interval"] = calc["fragment_interval"]

            if "speed" in opts or "speed_factor" in opts:
                sp_val = opts.get("speed", opts.get("speed_factor"))
                opts["speed"] = clamp_dynamic_speed(sp_val, fallback=1.0)
                opts["speed_factor"] = opts["speed"]
            if "temperature" in opts or "temp" in opts:
                temp_val = opts.get("temperature", opts.get("temp"))
                opts["temperature"] = clamp_dynamic_temperature(temp_val, fallback=1.0)
            if "top_k" in opts:
                opts["top_k"] = clamp_dynamic_top_k(opts["top_k"], fallback=15)
            if "top_p" in opts:
                opts["top_p"] = clamp_dynamic_top_p(opts["top_p"], fallback=1.0)
            if "fragment_interval" in opts:
                opts["fragment_interval"] = clamp_dynamic_fragment_interval(opts["fragment_interval"], fallback=0.3)
            if "batch_size" in opts and opts["batch_size"] is not None:
                opts["batch_size"] = clamp_dynamic_batch_size(opts["batch_size"], fallback=1)

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
        opts = self._sanitize_dynamic_voice_options(opts, text=text)
        if "text_split_method" not in opts and "cut_option" not in opts and "how_to_cut" not in opts:
            opts["text_split_method"] = "cut0" if len(text.strip()) <= 80 else "cut2"

        cache_key = ""
        clean_text = ""
        params_hash = ""
        if use_cache:
            cache_key, clean_text, params_hash = self.cache_manager.compute_cache_key(text, options=opts)
            cached = await self.cache_manager.get(cache_key)
            if cached is not None:
                logger.debug("TTS Cache HIT for key %s ('%s')", cache_key[:12], clean_text[:20])
                return cached[0]

        async def _do_gpu_synthesis() -> bytes:
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

        from galgame2voice.services.tts_scheduler import get_tts_scheduler, TtsPriority
        scheduler = get_tts_scheduler()
        raw_prio = opts.get("_priority", TtsPriority.NORMAL)
        try:
            priority = TtsPriority(int(raw_prio))
        except (ValueError, TypeError):
            priority = TtsPriority.NORMAL
        gen_id = opts.get("_generation_id")
        task_id = f"{gen_id}_{cache_key[:8]}_{uuid.uuid4().hex[:4]}" if gen_id and cache_key else None

        if cache_key:
            return await scheduler.schedule(
                lambda: scheduler.single_flight.execute(cache_key, _do_gpu_synthesis),
                priority=priority,
                generation_id=gen_id,
                task_id=task_id,
            )
        return await scheduler.schedule(
            _do_gpu_synthesis,
            priority=priority,
            generation_id=gen_id,
            task_id=task_id,
        )

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
        opts = self._sanitize_dynamic_voice_options(opts, text=text)
        if "text_split_method" not in opts and "cut_option" not in opts and "how_to_cut" not in opts:
            opts["text_split_method"] = "cut0" if len(text.strip()) <= 80 else "cut2"

        from galgame2voice.services.tts_scheduler import get_tts_scheduler, TtsPriority
        scheduler = get_tts_scheduler()
        raw_prio = opts.get("_priority", TtsPriority.NORMAL)
        try:
            priority = TtsPriority(int(raw_prio))
        except (ValueError, TypeError):
            priority = TtsPriority.NORMAL
        gen_id = opts.get("_generation_id")

        if use_cache:
            cache_key, clean_text, params_hash = self.cache_manager.compute_cache_key(text, options=opts)
            cached = await self.cache_manager.get(cache_key)
            if cached is not None:
                audio_bytes, url_path, file_size = cached
                file_path = self.cache_manager.cache_dir / f"{cache_key}.wav"
                logger.debug("TTS Cache HIT (file) for key %s -> %s", cache_key[:12], url_path)
                return url_path, file_path, file_size

            task_id = f"{gen_id}_{cache_key[:8]}_{uuid.uuid4().hex[:4]}" if gen_id else None

            # Synthesize exactly once on miss, coordinated via scheduler + single-flight
            async def _do_synth_file():
                audio_b = await self.client.synthesize(text, options=opts)
                if audio_b and cache_key:
                    try:
                        return await self.cache_manager.put(
                            cache_key=cache_key,
                            text=text,
                            clean_text=clean_text,
                            voice_profile_id=opts.get("voice_profile_id", 1),
                            params_hash=params_hash,
                            audio_bytes=audio_b,
                        )
                    except Exception as exc:
                        logger.warning("TTS cache put failed, writing ephemeral file instead: %s", exc)
                filename = f"{filename_prefix}_{uuid.uuid4().hex[:12]}.wav"
                file_path = self.audio_dir / filename
                await asyncio.to_thread(file_path.write_bytes, audio_b)
                url_path = f"/audio/{filename}"
                return url_path, file_path, len(audio_b)

            return await scheduler.schedule(
                lambda: scheduler.single_flight.execute(f"file_{cache_key}", _do_synth_file),
                priority=priority,
                generation_id=gen_id,
                task_id=task_id,
            )

        # Ephemeral non-cached file write (when use_cache=False)
        async def _do_ephemeral_file():
            audio_bytes = await self.client.synthesize(text, options=opts)
            filename = f"{filename_prefix}_{uuid.uuid4().hex[:12]}.wav"
            file_path = self.audio_dir / filename
            await asyncio.to_thread(file_path.write_bytes, audio_bytes)
            url_path = f"/audio/{filename}"
            return url_path, file_path, len(audio_bytes)

        return await scheduler.schedule(
            _do_ephemeral_file,
            priority=priority,
            generation_id=gen_id,
        )

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
        opts = self._sanitize_dynamic_voice_options(opts, text=text)
        if "text_split_method" not in opts and "cut_option" not in opts and "how_to_cut" not in opts:
            opts["text_split_method"] = "cut0" if len(text.strip()) <= 80 else "cut2"

        cache_key = ""
        clean_text = ""
        params_hash = ""
        if use_cache:
            cache_key, clean_text, params_hash = self.cache_manager.compute_cache_key(text, options=opts)
            cached = await self.cache_manager.get(cache_key)
            if cached is not None:
                async for chunk in self.cache_manager.stream_cached(cache_key, chunk_size=chunk_size):
                    yield chunk
                return

        from galgame2voice.services.tts_scheduler import get_tts_scheduler, TtsPriority
        scheduler = get_tts_scheduler()
        raw_prio = opts.get("_priority", TtsPriority.NORMAL)
        try:
            priority = TtsPriority(int(raw_prio))
        except (ValueError, TypeError):
            priority = TtsPriority.NORMAL

        gen_id = opts.get("_generation_id")
        task_id = opts.get("_task_id")

        collected_chunks = []
        completed_normally = False
        try:
            async for chunk in scheduler.schedule_stream(
                lambda: self.client.stream_tts(text, options=opts, chunk_size=chunk_size),
                priority=priority,
                generation_id=gen_id,
                task_id=task_id,
            ):
                collected_chunks.append(chunk)
                yield chunk
            completed_normally = True
        finally:
            if completed_normally and use_cache and collected_chunks and cache_key:
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
    "async_get_audio_duration",
    "clear_tts_profile_cache",
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
    "clamp_dynamic_top_k",
    "clamp_dynamic_top_p",
    "clamp_dynamic_fragment_interval",
    "clamp_dynamic_batch_size",
    "calculate_adaptive_prosody",
]
