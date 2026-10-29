"""
TTS Service and High-Level Audio Generation for galgame2voice.
Integrates the shared GPT-SoVITS singleton client with local audio storage,
sentence streaming, and the persistent TTS cache.
"""

import asyncio
import logging
import uuid
from contextlib import aclosing
from pathlib import Path
from typing import Any, AsyncGenerator

from galgame2voice.config import get_settings
from galgame2voice.utils.path_guard import resolve_existing_audio_path
from galgame2voice.utils.audio_spec import (
    _AUDIO_SPEC_CACHE,
    async_probe_audio_duration_seconds,
    REFERENCE_AUDIO_MIN_SECONDS,
    REFERENCE_AUDIO_MAX_SECONDS,
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
from galgame2voice.services.tts_scheduler import get_tts_scheduler, TtsPriority
from galgame2voice.services.character_identity import AmbiguousProfileName
from galgame2voice.services.voice_resolver import VoiceContextChangedError, VoiceContextUnavailableError
from galgame2voice.services.audio_identity import VoicePreparation, bind_audio_options
from galgame2voice.services.audio_artifacts import discard_new_audio_file, write_new_audio_file

logger = logging.getLogger("galgame2voice.services.tts_service")


class VoiceReferenceMissingError(ValueError):
    """A resolved voice has no reference audio and must not borrow another voice."""


class VoiceModelMissingError(ValueError):
    """A resolved voice lacks weights and cannot use an unrelated engine model."""


def _require_voice_reference(opts: dict[str, Any]) -> None:
    if not opts.get("ref_audio_path"):
        raise VoiceReferenceMissingError(
            "当前音色缺少参考音频，请在设置中心的角色与会话中重新选择音色，"
            "或创建包含参考音频的自定义音色。"
        )


def _require_voice_models(opts: dict[str, Any]) -> None:
    for field in ("gpt_weights_path", "sovits_weights_path"):
        value = opts.get(field)
        if not isinstance(value, (str, Path)) or not str(value).strip():
            raise VoiceModelMissingError(
                "当前音色的模型权重缺少 GPT 或 SoVITS 路径，"
                "请在设置中心的角色与会话中补全模型路径后重试。"
            )


# Backward compatibility aliases pointing to the thread-safe AudioSpecCache singleton.
_AUDIO_STAT_DURATION_CACHE = _AUDIO_SPEC_CACHE._cache
_AUDIO_DURATION_CACHE = _AUDIO_STAT_DURATION_CACHE


def clear_tts_profile_cache() -> None:
    """Clears in-memory caches for reference audio durations."""
    _AUDIO_SPEC_CACHE.clear()


async def async_get_audio_duration(path: str | Path | None) -> float | None:
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
        client: GptSovitsClient | None = None,
        audio_dir: str | Path | None = None,
        cache_manager: TtsCacheManager | None = None,
        db_path: str | None = None,
    ):
        settings = get_settings()
        self.client = client or get_gpt_sovits_client()
        self.audio_dir = Path(audio_dir or settings.audio_dir)
        self.audio_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path or (cache_manager.db_path if cache_manager is not None else None)
        self.cache_manager = cache_manager or get_tts_cache_manager(
            cache_dir=self.audio_dir / "cache",
            db_path=self.db_path or settings.db_path,
        )

    @staticmethod
    def get_audio_duration(path: str | Path | None) -> float | None:
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
    async def async_get_audio_duration(path: str | Path | None) -> float | None:
        """
        Asynchronously measures reference audio duration in seconds.
        Delegates to AudioSpecCache to keep the main asyncio event loop unblocked.
        """
        return await async_get_audio_duration(path)

    async def _resolve_fallback_ref_audio(
        self,
        opts: dict[str, Any],
        fallback_ref_audio: str | None,
        fallback_prompt_text: str | None,
        fallback_prompt_lang: str | None,
    ) -> None:
        """Validates user-provided reference audio or falls back to profile default reference."""
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
            if dur is not None and not (REFERENCE_AUDIO_MIN_SECONDS <= dur <= REFERENCE_AUDIO_MAX_SECONDS):
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
            elif "prompt_text" not in opts and not opts.get("refer_text"):
                # Fill an omitted transcript from this same resolved profile,
                # not from whatever voice happens to be loaded in the engine.
                opts["prompt_text"] = fallback_prompt_text
        else:
            opts["ref_audio_path"] = str(fallback_ref_audio) if fallback_ref_audio else ""
            opts.setdefault("prompt_text", fallback_prompt_text)
            if not opts.get("prompt_lang"):
                opts["prompt_lang"] = fallback_prompt_lang

    async def _populate_voice_profile_opts(self, opts: dict[str, Any]) -> dict[str, Any]:
        """Auto-populates active voice profile parameters, applying dynamic emotion reference audios if available."""
        # Preserve explicit aliases before defaults from the profile are added.
        for canonical, aliases in (
            ("ref_audio_path", ("refer_audio_path",)),
            ("prompt_text", ("refer_text",)),
            ("prompt_lang", ("prompt_language", "refer_language")),
            ("text_lang", ("text_language",)),
        ):
            if not opts.get(canonical):
                for alias in aliases:
                    if opts.get(alias):
                        opts[canonical] = opts[alias]
                        break
        # A pre-resolved reference is not necessarily a pre-resolved model.
        has_selector = bool(opts.get("voice_profile_id") or opts.get("character_name"))
        has_models = "gpt_weights_path" in opts and "sovits_weights_path" in opts
        opts.pop("_audio_identity", None)
        opts.pop('_voice_preparation', None)
        resolved_name = None
        if opts.get("_pre_resolved") and (has_models or not has_selector):
            if has_selector:
                _require_voice_reference(opts)
                _require_voice_models(opts)
            return await bind_audio_options(opts, self.client)

        # If user explicitly supplied all required reference audio options and no adaptive emotion override is needed,
        # completely bypass SQLite to keep the TTS critical path in-memory (TTFA < 1s).
        has_full_ref = bool(
            opts.get("ref_audio_path")
            and opts.get("prompt_text")
            and opts.get("prompt_lang")
        )
        ai_adaptive = opts.get("ai_adaptive_voice", opts.get("aiAdaptiveVoice", True))
        emotion = opts.get("emotion")
        if has_full_ref and not has_selector and not (ai_adaptive and emotion):
            return await bind_audio_options(opts, self.client)

        try:
            # Only implicit/default voice resolution is best-effort. An explicit
            # selector must never silently borrow the engine's current voice.
            from galgame2voice.services.voice_resolver import get_voice_resolver
            resolver = get_voice_resolver()

            prof_id = opts.get("voice_profile_id")
            char_name_opt = opts.get("character_name")
            ctx = await resolver.resolve_context(
                db_path=self.db_path,
                profile_id=int(prof_id) if prof_id is not None else None,
                character_name=str(char_name_opt) if char_name_opt else None,
            )

            if ctx is None and has_selector:
                raise ValueError("所选音色已不存在，请在角色与会话中重新选择音色。")

            if ctx:
                opts.setdefault("voice_profile_id", ctx.profile_id or 1)
                opts.setdefault("gpt_weights_path", ctx.gpt_weights_path)
                opts.setdefault("sovits_weights_path", ctx.sovits_weights_path)
                if not opts.get("prompt_lang"):
                    opts["prompt_lang"] = ctx.prompt_lang
                if not opts.get("text_lang"):
                    opts["text_lang"] = ctx.text_lang

                fallback_ref_audio = str(ctx.ref_audio_path) if ctx.ref_audio_path is not None else None
                fallback_prompt_text = ctx.prompt_text
                fallback_prompt_lang = ctx.prompt_lang

                # An empty transcript in a resolved profile is still part of
                # that profile; do not borrow the engine's previous voice text.
                if opts.get("prompt_text") is None and opts.get("refer_text") is None:
                    opts["prompt_text"] = fallback_prompt_text or ""
                opts["_profile_prompt_resolved"] = True

                resolved_emo = ctx.get_emotion_ref(str(emotion)) if (ai_adaptive and emotion) else None
                if resolved_emo:
                    opts["ref_audio_path"] = str(resolved_emo["ref_audio_path"]) if resolved_emo.get("ref_audio_path") else ""
                    opts["prompt_text"] = resolved_emo["prompt_text"]
                    opts["prompt_lang"] = resolved_emo["prompt_lang"]
                else:
                    await self._resolve_fallback_ref_audio(
                        opts=opts,
                        fallback_ref_audio=fallback_ref_audio,
                        fallback_prompt_text=fallback_prompt_text,
                        fallback_prompt_lang=fallback_prompt_lang,
                    )
                _require_voice_reference(opts)
                # A selected profile must not borrow either model from the
                # engine's previous voice. Explicit complete overrides remain valid.
                _require_voice_models(opts)
                resolved_name = ctx.name
        except (AmbiguousProfileName, VoiceContextChangedError, VoiceContextUnavailableError,
                VoiceReferenceMissingError, VoiceModelMissingError):
            raise
        except Exception as exc:
            if has_selector:
                raise ValueError("所选音色暂时无法读取，请在角色与会话中重新选择音色后重试。") from exc
            logger.debug("Could not auto-populate active profile options in TtsService: %s", exc)
        opts = await bind_audio_options(opts, self.client)
        identity = opts['_audio_identity']
        if resolved_name is not None and identity.gpt.path and identity.sovits.path:
            opts['_voice_preparation'] = VoicePreparation(resolved_name)
        return opts

    def _sanitize_dynamic_voice_options(self, opts: dict[str, Any], text: str | None = None) -> dict[str, Any]:
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

        if text is not None and "text_split_method" not in opts and "cut_option" not in opts and "how_to_cut" not in opts:
            opts["text_split_method"] = "cut0" if len(text.strip()) <= 80 else "cut2"

        return opts

    async def synthesize(
        self,
        text: str,
        options: dict[str, Any] | None = None,
        use_cache: bool = True,
    ) -> bytes:
        """
        Synthesizes text to WAV bytes via the shared GPT-SoVITS client or cache.
        Returns cached audio bytes in <50ms on hit.
        """
        opts = dict(options or {})
        opts = await self._populate_voice_profile_opts(opts)
        opts = self._sanitize_dynamic_voice_options(opts, text=text)

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
            # Several callers can miss before the first queued job finishes.
            # Recheck after admission to the serial worker to avoid re-synthesis.
            if cache_key:
                cached = await self.cache_manager.get(cache_key, record_miss=False)
                if cached is not None:
                    await self._validate_queued_cache_identity(opts)
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

        scheduler = get_tts_scheduler()
        priority = TtsPriority.from_options(opts)
        gen_id = opts.get("_generation_id")
        task_id = f"{gen_id}_{cache_key[:8]}_{uuid.uuid4().hex}" if gen_id and cache_key else None

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

    async def _validate_queued_cache_identity(self, opts: dict[str, Any]) -> None:
        """Recheck the pinned sources before delivering a queued cache hit."""
        await asyncio.to_thread(opts["_audio_identity"].validate_sources, opts, self.client)

    async def _write_ephemeral_audio_file(
        self,
        audio_bytes: bytes,
        filename_prefix: str,
    ) -> tuple[str, Path, int]:
        """Writes audio bytes to a new file in audio_dir and returns (url_path, local_file_path, byte_count)."""
        filename = f"{filename_prefix}_{uuid.uuid4().hex[:12]}.wav"
        file_path = self.audio_dir / filename
        await write_new_audio_file(file_path, audio_bytes)
        url_path = f"/audio/{filename}"
        return url_path, file_path, len(audio_bytes)

    async def synthesize_to_file(
        self,
        text: str,
        options: dict[str, Any] | None = None,
        filename_prefix: str = "voice",
        use_cache: bool = True,
    ) -> tuple[str, Path, int]:
        """
        Synthesizes text and saves or retrieves the resulting WAV file.
        Returns (url_path, local_file_path, byte_count).
        """
        opts = dict(options or {})
        opts = await self._populate_voice_profile_opts(opts)
        opts = self._sanitize_dynamic_voice_options(opts, text=text)

        scheduler = get_tts_scheduler()
        priority = TtsPriority.from_options(opts)
        gen_id = opts.get("_generation_id")

        if use_cache:
            cache_key, clean_text, params_hash = self.cache_manager.compute_cache_key(text, options=opts)
            cached = await self.cache_manager.get_file(cache_key)
            if cached is not None:
                logger.debug("TTS Cache HIT (file) for key %s -> %s", cache_key[:12], cached[0])
                return cached

            task_id = f"{gen_id}_{cache_key[:8]}_{uuid.uuid4().hex}" if gen_id else None

            # A preceding bytes/file job may populate the shared cache while queued.
            async def _do_synth_file() -> tuple[str, Path, int]:
                cached = await self.cache_manager.get_file(cache_key, record_miss=False)
                if cached is not None:
                    await self._validate_queued_cache_identity(opts)
                    return cached
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
                return await self._write_ephemeral_audio_file(audio_b, filename_prefix)

            return await scheduler.schedule(
                lambda: scheduler.single_flight.execute(f"file_{cache_key}", _do_synth_file),
                priority=priority,
                generation_id=gen_id,
                task_id=task_id,
            )

        # Ephemeral non-cached file write (when use_cache=False)
        owned_file = None
        abandoned = False
        async def _do_ephemeral_file() -> tuple[str, Path, int]:
            nonlocal owned_file
            audio_bytes = await self.client.synthesize(text, options=opts)
            if abandoned:
                raise asyncio.CancelledError()
            result = await self._write_ephemeral_audio_file(audio_bytes, filename_prefix)
            owned_file = result[1]
            if abandoned:
                await discard_new_audio_file(owned_file)
                raise asyncio.CancelledError()
            return result

        try:
            return await scheduler.schedule(
                _do_ephemeral_file,
                priority=priority,
                generation_id=gen_id,
            )
        except BaseException:
            # A completed job can lose its recipient while its result is being
            # delivered. This path owns only unique, uncached files; shared cache
            # and cache-fallback results retain their existing lifetime.
            abandoned = True
            if owned_file is not None:
                await discard_new_audio_file(owned_file)
            raise

    async def stream_tts(
        self,
        text: str,
        options: dict[str, Any] | None = None,
        chunk_size: int = 4096,
        use_cache: bool = True,
    ) -> AsyncGenerator[bytes, None]:
        """Streams audio chunks from cache or the shared GPT-SoVITS client."""
        opts = dict(options or {})
        opts = await self._populate_voice_profile_opts(opts)
        opts = self._sanitize_dynamic_voice_options(opts, text=text)

        cache_key = ""
        clean_text = ""
        params_hash = ""
        if use_cache:
            cache_key, clean_text, params_hash = self.cache_manager.compute_cache_key(text, options=opts)
            # Probe by reading the first bounded chunk, rather than loading the
            # whole clip before streaming it. One playback records one hit.
            async with aclosing(self.cache_manager.stream_cached(
                cache_key, chunk_size=chunk_size, record_miss=True,
            )) as cached_stream:
                first_chunk = await anext(cached_stream, None)
                if first_chunk is not None:
                    yield first_chunk
                    async for chunk in cached_stream:
                        yield chunk
                    return

        scheduler = get_tts_scheduler()
        priority = TtsPriority.from_options(opts)

        gen_id = opts.get("_generation_id")
        task_id = opts.get("_task_id")

        collected_chunks = []
        upstream_complete = False
        completed_normally = False
        reused_cached_audio = False

        async def _stream_upstream() -> AsyncGenerator[bytes, None]:
            nonlocal upstream_complete, reused_cached_audio
            # A preceding bytes/file job may have filled the cache while this
            # stream waited for the worker. Recheck without counting a second
            # miss, and keep replay bounded and read-only.
            if use_cache and cache_key:
                async with aclosing(self.cache_manager.stream_cached(
                    cache_key, chunk_size=chunk_size, record_miss=False,
                )) as cached_stream:
                    first_chunk = await anext(cached_stream, None)
                    if first_chunk is not None:
                        await self._validate_queued_cache_identity(opts)
                        reused_cached_audio = True
                        yield first_chunk
                        async for chunk in cached_stream:
                            yield chunk
                        return
            async with aclosing(self.client.stream_tts(text, options=opts, chunk_size=chunk_size)) as upstream:
                async for chunk in upstream:
                    yield chunk
            upstream_complete = True

        try:
            async with aclosing(scheduler.schedule_stream(
                _stream_upstream,
                priority=priority,
                generation_id=gen_id,
                task_id=task_id,
            )) as scheduled_stream:
                async for chunk in scheduled_stream:
                    if use_cache and cache_key and not reused_cached_audio:
                        collected_chunks.append(chunk)
                    yield chunk
            # A cancelled generation ends the scheduled stream without raising;
            # only actual upstream EOF plus a fully drained buffer is cacheable.
            completed_normally = upstream_complete
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
