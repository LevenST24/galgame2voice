"""
Voice Profile Manager for galgame2voice.
Coordinates SQLite voice_profiles persistence with GptSovitsClient model switching,
mutex locking, and atomic rollback on failure.
"""

import asyncio
import logging
import os
import uuid
from typing import Any, AsyncGenerator, Dict, List, Optional, Union


from galgame2voice.config import get_settings
from galgame2voice.database import crud
from galgame2voice.database.session import get_db
from galgame2voice.database.models import (
    VoiceProfileCreate,
    VoiceProfileUpdate,
    VoiceProfileResponse,
    VoiceProfileInDB,
)
from galgame2voice.services.gpt_sovits_client import (
    GptSovitsClient,
    get_gpt_sovits_client,
)
from galgame2voice.utils.hardware import (
    get_system_memory_status,
    get_gpu_vram_status,
    release_system_memory,
)

logger = logging.getLogger("galgame2voice.services.voice_manager")


class InsufficientMemoryError(RuntimeError):
    """Raised when free memory is too low to safely load new model weights."""


# 切换权重时新旧模型会短暂同时驻留内存（GPT约155MB + SoVITS约172MB = 约330MB，极端过渡期约660MB）；
# 默认安全阈值：总内存的 6% 或最低 0.8GB，并设有 1.5GB 安全上限（大内存机器不会被百分比过度拦截）。
# 16GB 设备所需空闲仅约 0.96GB，只要空闲内存大于 1GB 即可丝滑切换。
_MIN_FREE_MEMORY_RATIO = 0.06
_MIN_FREE_MEMORY_FLOOR_GB = 0.8
_MIN_FREE_MEMORY_CEILING_GB = 1.5
_MIN_FREE_VRAM_FLOOR_GB = 0.45


def _get_switch_min_free_memory_gb() -> float:
    try:
        val = os.getenv("GALGAME2VOICE_MIN_FREE_MEM_GB")
        if val is not None:
            return float(val)
    except (ValueError, TypeError):
        pass
    total_gb, _ = get_system_memory_status()
    if total_gb:
        scaled = round(total_gb * _MIN_FREE_MEMORY_RATIO, 2)
        return min(_MIN_FREE_MEMORY_CEILING_GB, max(_MIN_FREE_MEMORY_FLOOR_GB, scaled))
    return _MIN_FREE_MEMORY_FLOOR_GB


def _get_switch_min_free_vram_gb() -> float:
    try:
        val = os.getenv("GALGAME2VOICE_MIN_FREE_VRAM_GB")
        if val is not None:
            return float(val)
    except (ValueError, TypeError):
        pass
    return _MIN_FREE_VRAM_FLOOR_GB


def _safe_invalidate_resolver(profile_id: Optional[int] = None) -> None:
    """Safely invalidates the voice resolver cache without raising exceptions."""
    try:
        from galgame2voice.services.voice_resolver import get_voice_resolver
        get_voice_resolver().invalidate(profile_id)
    except Exception as exc:
        logger.debug("Failed invalidating voice resolver cache: %s", exc)


class VoiceManager:
    """
    Coordinates character voice profile management and atomic model switching with GPT-SoVITS.
    Ensures thread-safe operations via an inference mutex and atomic SQLite persistence.

    By default binds to the application-wide shared GptSovitsClient singleton so
    that synthesis and switching are serialized against the GPU by a single lock.
    """

    def __init__(
        self,
        gpt_sovits_client_or_server: Union[GptSovitsClient, Any, str, None] = None,
        db_path: Optional[str] = None,
    ):
        settings = get_settings()
        self.db_path = db_path or str(settings.db_path)

        if isinstance(gpt_sovits_client_or_server, GptSovitsClient):
            self.client = gpt_sovits_client_or_server
        elif isinstance(gpt_sovits_client_or_server, str):
            self.client = GptSovitsClient(base_url=gpt_sovits_client_or_server)
        elif gpt_sovits_client_or_server is not None and hasattr(gpt_sovits_client_or_server, "handle_request"):
            # MockGptSovitsServer or test simulator
            self.client = GptSovitsClient(server=gpt_sovits_client_or_server)
        else:
            # Shared application singleton: one lock to rule them all.
            self.client = get_gpt_sovits_client()

        from galgame2voice.services.tts_service import TtsService
        self.tts_service = TtsService(client=self.client, db_path=self.db_path)

        self._switch_lock = asyncio.Lock()
        self._bg_tasks: set[asyncio.Task] = set()

    def _spawn_background(self, coro: Any) -> asyncio.Task:
        """Spawns and retains a strong reference to a background task, preventing GC mid-execution."""
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return task

    async def aclose(self) -> None:
        """Gracefully drains and cancels pending background warmup tasks upon service shutdown."""
        pending = [t for t in self._bg_tasks if not t.done()]
        if pending:
            await asyncio.wait(pending, timeout=3.0)
        stragglers = [t for t in self._bg_tasks if not t.done()]
        for t in stragglers:
            t.cancel()
        if stragglers:
            await asyncio.gather(*stragglers, return_exceptions=True)
        self._bg_tasks.clear()

    @property
    def lock(self) -> asyncio.Lock:
        """Shared inference and model switching mutex."""
        return self.client.lock

    @property
    def switch_lock(self) -> asyncio.Lock:
        """Lock for serializing voice profile switch transactions and state persistence."""
        return self._switch_lock

    @property
    def server(self) -> Optional[Any]:
        """Access mock/internal server if configured."""
        return self.client.server

    @server.setter
    def server(self, val: Any) -> None:
        self.client.server = val

    @property
    def active_profile(self) -> Optional[Any]:
        """Currently active voice profile model."""
        return self.client.active_profile

    @active_profile.setter
    def active_profile(self, val: Any) -> None:
        self.client.active_profile = val

    @property
    def is_switching(self) -> bool:
        """Whether a model switch is currently in progress."""
        return self.client.is_switching

    # ========================================================================
    # Voice Profile Switching (Atomic 3-Step + Persistence)
    # ========================================================================

    def is_active_profile(self, profile: Any, force: bool = False) -> bool:
        """
        Check if the given profile matches the currently active voice profile.
        Uses canonical fallback-chain semantics: attribute access, then
        refer_audio_path/refer_text legacy aliases, then dict-key fallback.
        """
        if force:
            return False

        active_prof = self.active_profile
        if not active_prof or not profile:
            return False

        active_id = getattr(active_prof, "id", None) or (active_prof.get("id") if isinstance(active_prof, dict) else None)
        active_gpt = getattr(active_prof, "gpt_weights_path", None) or (active_prof.get("gpt_weights_path") if isinstance(active_prof, dict) else None)
        active_sovits = getattr(active_prof, "sovits_weights_path", None) or (active_prof.get("sovits_weights_path") if isinstance(active_prof, dict) else None)
        active_ref = getattr(active_prof, "ref_audio_path", None) or getattr(active_prof, "refer_audio_path", None) or (active_prof.get("ref_audio_path") if isinstance(active_prof, dict) else (active_prof.get("refer_audio_path") if isinstance(active_prof, dict) else None))
        active_prompt = getattr(active_prof, "prompt_text", None) or getattr(active_prof, "refer_text", None) or (active_prof.get("prompt_text") if isinstance(active_prof, dict) else (active_prof.get("refer_text") if isinstance(active_prof, dict) else None))

        prof_id = getattr(profile, "id", None) or (profile.get("id") if isinstance(profile, dict) else None)
        prof_gpt = getattr(profile, "gpt_weights_path", None) or (profile.get("gpt_weights_path") if isinstance(profile, dict) else None)
        prof_sovits = getattr(profile, "sovits_weights_path", None) or (profile.get("sovits_weights_path") if isinstance(profile, dict) else None)
        prof_ref = getattr(profile, "ref_audio_path", None) or getattr(profile, "refer_audio_path", None) or (profile.get("ref_audio_path") if isinstance(profile, dict) else (profile.get("refer_audio_path") if isinstance(profile, dict) else None))
        prof_prompt = getattr(profile, "prompt_text", None) or getattr(profile, "refer_text", None) or (profile.get("prompt_text") if isinstance(profile, dict) else (profile.get("refer_text") if isinstance(profile, dict) else None))

        return bool(
            active_id == prof_id
            and active_gpt == prof_gpt
            and active_sovits == prof_sovits
            and active_ref == prof_ref
            and active_prompt == prof_prompt
        )

    async def switch_profile(
        self,
        target: Union[int, str, VoiceProfileResponse, VoiceProfileInDB, Dict[str, Any], Any],
        persist: bool = True,
        _already_locked: bool = False,
        force: bool = False,
    ) -> bool:
        """
        Atomically switches GPT-SoVITS weights to target voice profile.
        If target is an int ID or string ID/name, looks up profile from SQLite DB.
        On success, updates SQLite active profile if persist=True.
        On failure, automatically rolls back weights and preserves prior state.
        Serialized with self._switch_lock.
        """
        if _already_locked:
            return await self._execute_switch(target, persist=persist, force=force)
        async with self._switch_lock:
            return await self._execute_switch(target, persist=persist, force=force)

    async def switch_active_profile(
        self,
        target: Union[int, str, VoiceProfileResponse, VoiceProfileInDB, Dict[str, Any], Any],
        persist: bool = True,
        force: bool = False,
    ) -> bool:
        """Alias for switch_profile to preserve backwards compatibility."""
        return await self.switch_profile(target, persist=persist, force=force)

    def _check_vram_guard(self, min_free_vram_gb: float = 0.45) -> None:
        """
        Verifies discrete GPU VRAM safety floor before switching models.
        If discrete NVIDIA CUDA GPU is detected and free VRAM < floor,
        attempts release_system_memory() and re-checks.
        If still below floor, raises InsufficientMemoryError.
        Skips cleanly if no CUDA GPU is detected (e.g. CPU or MPS mode).
        """
        total_vram, free_vram = get_gpu_vram_status()
        if free_vram is None:
            return

        threshold = min_free_vram_gb if min_free_vram_gb is not None else _get_switch_min_free_vram_gb()
        if free_vram < threshold:
            release_system_memory()
            _, free_vram = get_gpu_vram_status()
            if free_vram is not None and free_vram < threshold:
                raise InsufficientMemoryError(
                    f"显卡可用显存不足（{free_vram:.2f} GB < {threshold:.2f} GB 安全阈值），"
                    "加载新模型权重存在 CUDA OOM 崩溃风险。请关闭占用显存的应用或清理后重试。"
                )

    async def warmup_current_profile(self) -> bool:
        """
        Asynchronously warms up current voice profile model weights and GPT-SoVITS prompt cache.
        Sends weights and a lightweight 1-word/short probe so the first turn is fully hot.
        Non-blocking, resilient against network errors or engine offline states.
        """
        try:
            profile = self.active_profile
            if not profile:
                profile = await self.get_active_profile()
                if profile:
                    self.active_profile = profile
            if not profile:
                logger.debug("Warm-up skipped: no active voice profile found.")
                return False

            # Check engine reachability before attempting switch to avoid connection error tracebacks
            try:
                health = await asyncio.wait_for(self.client.check_health(), timeout=2.0)
                if not health.get("connected"):
                    logger.debug("Warm-up skipped: GPT-SoVITS engine is offline.")
                    return False
            except Exception:
                logger.debug("Warm-up skipped: GPT-SoVITS engine unreachable.")
                return False

            # Ensure weights and reference audio are set
            ok = await self.client.switch_voice_profile(profile, force=False)
            if not ok:
                logger.debug("Warm-up skipped: weight switch to profile '%s' failed (engine offline).", getattr(profile, "name", "unknown"))
                return False

            # Probe synthesis to warm up HuBERT, STFT, and RoBERTa prompt_cache via LOW priority scheduling
            opts = await self._resolve_active_options({
                "voice_profile_id": getattr(profile, "id", None),
                "ref_audio_path": getattr(profile, "ref_audio_path", ""),
                "prompt_text": getattr(profile, "prompt_text", ""),
                "prompt_lang": getattr(profile, "prompt_lang", "ja"),
                "text_lang": getattr(profile, "text_lang", "ja"),
            })
            try:
                from galgame2voice.services.tts_scheduler import get_tts_scheduler, TtsPriority
                scheduler = get_tts_scheduler()
                await asyncio.wait_for(
                    scheduler.schedule(
                        lambda: self.client.synthesize("。", options=opts),
                        priority=TtsPriority.LOW,
                        task_id=f"warmup_{getattr(profile, 'id', 'default')}",
                    ),
                    timeout=15.0,
                )
                logger.info("GPT-SoVITS prompt audio cache warm-up succeeded for profile '%s'.", getattr(profile, "name", "unknown"))
                return True
            except Exception as probe_err:
                logger.warning("Lightweight probe during warm-up failed or timed out: %s", probe_err)
                return False
        except Exception as exc:
            logger.warning("Voice profile warm-up encountered error: %s", exc)
            return False

    async def _execute_switch(
        self,
        target: Union[int, str, VoiceProfileResponse, VoiceProfileInDB, Dict[str, Any], Any],
        persist: bool = True,
        force: bool = False,
    ) -> bool:
        profile_obj = target

        # 1. Resolve Profile from DB if ID or Name provided
        if isinstance(target, int) or (isinstance(target, str) and target.isdigit()):
            profile_id = int(target)
            async with get_db(self.db_path) as conn:
                db_profile = await crud.get_voice_profile(conn, profile_id)
                if not db_profile:
                    logger.error("Voice profile ID %d not found in database", profile_id)
                    return False
                profile_obj = db_profile

        elif isinstance(target, str):
            # Target may be a character profile name
            async with get_db(self.db_path) as conn:
                db_profile = await crud.get_voice_profile_by_name(conn, target)
                if db_profile:
                    profile_obj = db_profile
                else:
                    logger.warning("Voice profile name '%s' not found in database", target)

        # Bail out if the target failed to resolve to an actual profile object
        if isinstance(profile_obj, str):
            logger.error("Cannot switch voice profile: unresolved string target '%s'", profile_obj)
            return False

        # Memory precheck: new and old weights briefly co-reside during a switch; loading
        # with too little free memory OOM-crashes the engine. Sits here (not in the HTTP
        # layer) so every call path — REST, Telegram, auto-bind — gets the same guard.
        if not force and not os.getenv("GALGAME2VOICE_SKIP_MEM_CHECK"):
            release_system_memory()
            _, free_gb = get_system_memory_status()
            min_free_gb = _get_switch_min_free_memory_gb()
            if free_gb is not None and free_gb < min_free_gb:
                raise InsufficientMemoryError(
                    f"系统空闲内存不足（{free_gb:.1f} GB < {min_free_gb:.1f} GB），"
                    "加载新模型权重可能导致语音引擎崩溃。请关闭占内存的程序后重试。"
                )
            self._check_vram_guard()

        # 2. Execute 3-step atomic model switch with auto-rollback
        release_system_memory()
        success = await self.client.switch_voice_profile(profile_obj, force=force)
        release_system_memory()
        if not success:
            logger.error("Failed to switch GPT-SoVITS model weights for target: %s", target)
            return False

        # 3. Update Persistence in SQLite (under switch lock)
        if persist:
            profile_id = None
            if hasattr(profile_obj, "id") and profile_obj.id is not None:
                profile_id = profile_obj.id
            elif isinstance(profile_obj, dict) and "id" in profile_obj:
                profile_id = profile_obj["id"]

            if profile_id:
                try:
                    async with get_db(self.db_path) as conn:
                        await crud.set_active_voice_profile(conn, profile_id)
                        logger.info("Persisted active voice profile ID %d in settings", profile_id)
                except Exception as exc:
                    logger.warning("Could not persist active voice profile ID to DB: %s", exc)

        # Invalidate in-memory voice resolver cache
        _safe_invalidate_resolver()

        # Trigger non-blocking background warm-up of newly activated voice profile
        try:
            self._spawn_background(self.warmup_current_profile())
        except Exception as warmup_err:
            logger.debug("Could not schedule warm-up task on profile switch: %s", warmup_err)

        return True

    # ========================================================================
    # Synthesis & Streaming
    # ========================================================================

    async def _resolve_active_options(self, options: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        opts = dict(options or {})
        if not opts.get("ref_audio_path") and not opts.get("refer_audio_path") and not self.client.current_refer_audio:
            target_profile = None
            profile_id = opts.get("voice_profile_id") or opts.get("profile_id")
            if profile_id is not None:
                try:
                    target_profile = await self.get_profile(int(profile_id))
                except Exception as exc:
                    logger.debug("Could not resolve voice_profile_id %s: %s", profile_id, exc)
            if not target_profile:
                try:
                    target_profile = await self.get_active_profile()
                except Exception as exc:
                    logger.debug("Could not auto-populate active profile options: %s", exc)
            if target_profile:
                opts.setdefault("voice_profile_id", target_profile.id)
                opts.setdefault("ref_audio_path", target_profile.ref_audio_path)
                opts.setdefault("prompt_text", target_profile.prompt_text)
                opts.setdefault("prompt_lang", target_profile.prompt_lang)
                opts.setdefault("text_lang", target_profile.text_lang)
        return opts

    async def synthesize(
        self,
        text: str,
        options: Optional[Dict[str, Any]] = None,
        use_cache: bool = True,
    ) -> bytes:
        """Synthesizes text into complete audio bytes using active weights, persistent cache and inference mutex."""
        opts = await self._resolve_active_options(options)
        if getattr(self, "tts_service", None) is not None:
            return await self.tts_service.synthesize(text, options=opts, use_cache=use_cache)
        return await self.client.synthesize(text, options=opts)

    async def stream_tts(
        self,
        text: str,
        options: Optional[Dict[str, Any]] = None,
        chunk_size: int = 4096,
        use_cache: bool = True,
    ) -> AsyncGenerator[bytes, None]:
        """
        Streams synthesized audio in binary chunks using priority scheduling and persistent cache.
        1. First checks TTS cache (tts_cache_manager.stream_cached(cache_key)). If cached, yields from cache directly with zero GPU overhead.
        2. If cache miss, routes through tts_scheduler.schedule_stream(...).
        3. While streaming generated chunks, if caching is enabled, collects chunks and asynchronously puts into tts_cache_manager when complete.
        """
        opts = await self._resolve_active_options(options)

        effective_use_cache = bool(opts.get("use_cache", use_cache))

        from galgame2voice.services.tts_cache_manager import get_tts_cache_manager
        cache_mgr = (
            getattr(getattr(self, "tts_service", None), "cache_manager", None)
            or get_tts_cache_manager(db_path=self.db_path)
        )

        cache_key = ""
        clean_text = ""
        params_hash = ""
        if effective_use_cache and cache_mgr is not None:
            try:
                cache_key, clean_text, params_hash = cache_mgr.compute_cache_key(text, options=opts)
            except Exception as exc:
                logger.debug("Failed to compute cache key in stream_tts: %s", exc)

        # 1. First check TTS cache (tts_cache_manager.stream_cached(cache_key))
        if effective_use_cache and cache_key and cache_mgr is not None:
            is_cached = False
            try:
                async for chunk in cache_mgr.stream_cached(cache_key, chunk_size=chunk_size):
                    is_cached = True
                    yield chunk
            except Exception as exc:
                logger.warning("Error reading from TTS stream cache: %s", exc)

            if is_cached:
                logger.debug("TTS Cache HIT (stream) for key %s ('%s')", cache_key[:12], text[:20])
                return

        # 2. If cache miss, route through tts_scheduler.schedule_stream(...)
        from galgame2voice.services.tts_scheduler import get_tts_scheduler, TtsPriority
        scheduler = get_tts_scheduler()
        priority = TtsPriority.from_options(opts)

        gen_id = opts.get("_generation_id")
        task_id = opts.get("_task_id") or (
            f"{gen_id}_{cache_key[:8]}_{uuid.uuid4().hex[:4]}" if gen_id and cache_key else None
        )

        def _client_stream_fn() -> AsyncGenerator[bytes, None]:
            return self.client.stream_tts(text, options=opts, chunk_size=chunk_size)

        collected_chunks: List[bytes] = []
        completed_normally = False
        try:
            async for chunk in scheduler.schedule_stream(
                stream_fn=_client_stream_fn,
                priority=priority,
                generation_id=gen_id,
                task_id=task_id,
            ):
                if effective_use_cache and cache_key:
                    collected_chunks.append(chunk)
                yield chunk
            completed_normally = True
        finally:
            # 3. While streaming generated chunks, if caching is enabled, collect chunks
            # and asynchronously put into tts_cache_manager when complete.
            if completed_normally and effective_use_cache and cache_key and collected_chunks and cache_mgr is not None:
                full_bytes = b"".join(collected_chunks)
                if full_bytes:
                    vpid = opts.get("voice_profile_id", 1)
                    async def _async_cache_put(
                        b_key: str = cache_key,
                        b_text: str = text,
                        b_clean: str = clean_text,
                        b_vpid: Any = vpid,
                        b_hash: str = params_hash,
                        b_audio: bytes = full_bytes,
                    ) -> None:
                        try:
                            await cache_mgr.put(
                                cache_key=b_key,
                                text=b_text,
                                clean_text=b_clean,
                                voice_profile_id=b_vpid,
                                params_hash=b_hash,
                                audio_bytes=b_audio,
                            )
                        except Exception as put_exc:
                            logger.debug("Failed to asynchronously cache streamed TTS: %s", put_exc)

                    self._spawn_background(_async_cache_put())

    # ========================================================================
    # Voice Profile Database CRUD Operations
    # ========================================================================

    async def list_profiles(self) -> List[VoiceProfileResponse]:
        """Lists all voice profiles in database."""
        async with get_db(self.db_path) as conn:
            return await crud.list_voice_profiles(conn)

    async def get_profile(self, profile_id: int) -> Optional[VoiceProfileResponse]:
        """Gets voice profile by ID."""
        async with get_db(self.db_path) as conn:
            return await crud.get_voice_profile(conn, profile_id)

    async def get_active_profile(self) -> Optional[VoiceProfileResponse]:
        """Gets currently configured active voice profile from database."""
        async with get_db(self.db_path) as conn:
            return await crud.get_active_voice_profile(conn)

    async def create_profile(self, profile: VoiceProfileCreate) -> VoiceProfileResponse:
        """Creates a new voice profile in database."""
        async with get_db(self.db_path) as conn:
            res = await crud.create_voice_profile(conn, profile)
        _safe_invalidate_resolver(res.id if res else None)
        return res

    async def update_profile(
        self, profile_id: int, updates: VoiceProfileUpdate
    ) -> Optional[VoiceProfileResponse]:
        """Updates an existing voice profile in database."""
        async with get_db(self.db_path) as conn:
            res = await crud.update_voice_profile(conn, profile_id, updates)
        _safe_invalidate_resolver(profile_id)
        return res

    async def delete_profile(self, profile_id: int) -> bool:
        """Deletes a voice profile from database."""
        async with get_db(self.db_path) as conn:
            res = await crud.delete_voice_profile(conn, profile_id)
        _safe_invalidate_resolver(profile_id)
        return res


# ============================================================================
# Global Singleton Accessor
# ============================================================================

_global_voice_manager: Optional[VoiceManager] = None


def get_voice_manager() -> VoiceManager:
    """Returns application singleton VoiceManager instance."""
    global _global_voice_manager
    if _global_voice_manager is None:
        _global_voice_manager = VoiceManager()
    return _global_voice_manager


def set_voice_manager(manager: Optional[VoiceManager]) -> None:
    """Sets or resets application singleton VoiceManager instance (useful for tests)."""
    global _global_voice_manager
    _global_voice_manager = manager


__all__ = [
    "VoiceManager",
    "get_voice_manager",
    "set_voice_manager",
]
