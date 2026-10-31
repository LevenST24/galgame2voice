"""
Voice Profile Manager for galgame2voice.
Coordinates SQLite voice_profiles persistence with GptSovitsClient model switching,
mutex locking, and atomic rollback on failure.
"""

import asyncio
import logging
import uuid
from contextlib import aclosing
from typing import Any, AsyncGenerator


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
from galgame2voice.utils.async_tasks import await_protected, drain_background_tasks
from galgame2voice.services.voice_state import VoiceProfileRemoved, persist_active_voice_profile
from galgame2voice.utils.hardware import (
    get_system_memory_status,
    get_gpu_vram_status,
    release_system_memory,
)

from galgame2voice.services.voice_resources import (
    VoiceResourceGuard,
    InsufficientMemoryError as InsufficientMemoryError,
    _MIN_FREE_MEMORY_RATIO as _MIN_FREE_MEMORY_RATIO,
    _MIN_FREE_MEMORY_FLOOR_GB as _MIN_FREE_MEMORY_FLOOR_GB,
    _MIN_FREE_MEMORY_CEILING_GB as _MIN_FREE_MEMORY_CEILING_GB,
    _MIN_FREE_VRAM_FLOOR_GB as _MIN_FREE_VRAM_FLOOR_GB,
)

logger = logging.getLogger("galgame2voice.services.voice_manager")


# GPT-SoVITS rejects symbol-only probe text.
WARMUP_PROBE_TEXT = "はい。"


def _get_switch_min_free_memory_gb() -> float:
    return VoiceResourceGuard(memory_status=get_system_memory_status).minimum_memory_gb()


def _get_switch_min_free_vram_gb() -> float:
    return VoiceResourceGuard.minimum_vram_gb()


class VoiceManager:
    """
    Coordinates character voice profile management and atomic model switching with GPT-SoVITS.
    Ensures thread-safe operations via an inference mutex and atomic SQLite persistence.

    By default binds to the application-wide shared GptSovitsClient singleton so
    that synthesis and switching are serialized against the GPU by a single lock.
    """

    def __init__(
        self,
        gpt_sovits_client_or_server: GptSovitsClient | Any | str | None = None,
        db_path: str | None = None,
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
        self._switching_profile_id: int | None = None
        self._bg_tasks: set[asyncio.Task] = set()

    def _spawn_background(self, coro: Any) -> asyncio.Task:
        """Spawns and retains a strong reference to a background task, preventing GC mid-execution.

        The done-callback also consumes the task's result. Without that, a failed
        fire-and-forget task keeps its exception unretrieved and the interpreter
        reports a bare "Task exception was never retrieved" whenever the task is
        collected — long after the context that could explain it is gone.
        """
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._on_background_done)
        return task

    def _on_background_done(self, task: asyncio.Task) -> None:
        self._bg_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.warning(
                "Background voice task failed: %s: %s",
                type(exc).__name__,
                exc,
                exc_info=exc,
            )

    async def aclose(self) -> None:
        """Gracefully drains and cancels pending background warmup tasks upon service shutdown."""
        await drain_background_tasks(self._bg_tasks, timeout=3.0)

    @property
    def lock(self) -> asyncio.Lock:
        """Shared inference and model switching mutex."""
        return self.client.lock

    @property
    def switch_lock(self) -> asyncio.Lock:
        """Lock for serializing voice profile switch transactions and state persistence."""
        return self._switch_lock

    @property
    def switching_profile_id(self) -> int | None:
        """Voice whose weights are loading now. Read without waiting for the switch lock."""
        return self._switching_profile_id

    @property
    def server(self) -> Any | None:
        """Access mock/internal server if configured."""
        return self.client.server

    @server.setter
    def server(self, val: Any) -> None:
        self.client.server = val

    @property
    def active_profile(self) -> Any | None:
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

    @staticmethod
    def _resolve_profile_field(p: Any, name: str, alias: str | None = None) -> Any:
        """Resolves profile field with attribute, fallback alias, and dict key lookups."""
        p_dict = p if isinstance(p, dict) else None
        val = getattr(p, name, None) or (p_dict.get(name) if p_dict else None)
        if not val and alias:
            val = getattr(p, alias, None) or (p_dict.get(alias) if p_dict else None)
        return val

    @staticmethod
    def _extract_profile_identity(p: Any) -> tuple[Any, Any, Any, Any, Any]:
        """Extracts canonical (id, gpt_weights_path, sovits_weights_path, ref_audio_path, prompt_text) tuple."""
        resolve = VoiceManager._resolve_profile_field
        return (
            resolve(p, "id"),
            resolve(p, "gpt_weights_path"),
            resolve(p, "sovits_weights_path"),
            resolve(p, "ref_audio_path", "refer_audio_path"),
            resolve(p, "prompt_text", "refer_text"),
        )

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

        if self._extract_profile_identity(active_prof) != self._extract_profile_identity(profile):
            return False
        if not self.client.models_unchanged():
            return False
        from galgame2voice.services.audio_identity import SourceFile
        for field, current in (("gpt_weights_path", self.client.current_gpt_weights),
                               ("sovits_weights_path", self.client.current_sovits_weights)):
            expected = self._resolve_profile_field(profile, field)
            if (isinstance(current, str) and current and isinstance(expected, str) and expected
                    and SourceFile.capture(current).path != SourceFile.capture(expected).path):
                return False
        return True

    async def switch_profile(
        self,
        target: int | str | VoiceProfileResponse | VoiceProfileInDB | dict[str, Any] | Any,
        persist: bool = True,
        _already_locked: bool = False,
        force: bool = False,
    ) -> bool:
        """
        Switches GPT-SoVITS weights, then confirms the saved default selection.
        If target is an int ID or string ID/name, looks up profile from SQLite DB.
        On success, updates SQLite active profile if persist=True.
        Model-load failure rolls back weights. Persistence failure is reported
        separately and keeps the known loaded models available for a retry.
        Serialized with self._switch_lock.
        """
        if _already_locked:
            return await self._execute_switch(target, persist=persist, force=force)
        async with self._switch_lock:
            return await self._execute_switch(target, persist=persist, force=force)

    async def switch_active_profile(
        self,
        target: int | str | VoiceProfileResponse | VoiceProfileInDB | dict[str, Any] | Any,
        persist: bool = True,
        force: bool = False,
    ) -> bool:
        """Alias for switch_profile to preserve backwards compatibility."""
        return await self.switch_profile(target, persist=persist, force=force)

    def _check_vram_guard(self, min_free_vram_gb: float | None = None) -> None:
        """Apply the shared VRAM policy with this manager's hardware probes."""
        VoiceResourceGuard(gpu_status=get_gpu_vram_status, release=release_system_memory).check_vram(min_free_vram_gb)

    async def warmup_current_profile(self) -> bool:
        """Warm the current selection in the low-priority worker, after engine ACK."""
        profile = None
        try:
            profile = self.active_profile or await self.get_active_profile()
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

            async def prepare_and_probe():
                nonlocal profile
                # Queueing and the health check may outlive a manual switch.
                # Select only after taking its lock; never replay a stale target.
                async with self.switch_lock:
                    profile = self.active_profile or await self.get_active_profile()
                    if not profile:
                        return False
                    if not await asyncio.to_thread(self.is_active_profile, profile):
                        await asyncio.to_thread(self._check_switch_memory_guard, False)
                    if not await self.client.switch_voice_profile(profile, force=False):
                        logger.debug("Warm-up model load failed for '%s'.", getattr(profile, "name", "unknown"))
                        return False
                    opts = await self._resolve_active_options({
                        "voice_profile_id": getattr(profile, "id", None),
                        "ref_audio_path": getattr(profile, "ref_audio_path", ""),
                        "prompt_text": getattr(profile, "prompt_text", ""),
                        "prompt_lang": getattr(profile, "prompt_lang", "ja"),
                        "text_lang": getattr(profile, "text_lang", "ja"),
                    })
                    await asyncio.wait_for(self.client.synthesize(WARMUP_PROBE_TEXT, options=opts), timeout=15.0)
                logger.info("GPT-SoVITS prompt audio cache warm-up succeeded for profile '%s'.", getattr(profile, "name", "unknown"))
                return True

            from galgame2voice.services.tts_scheduler import get_tts_scheduler, TtsPriority
            # Loading weights retains the client's 120s per-model deadlines;
            # the 15s probe limit must not cancel a normal cold model load.
            return await asyncio.wait_for(
                get_tts_scheduler().schedule(
                    prepare_and_probe,
                    priority=TtsPriority.LOW,
                    task_id=f"warmup_{getattr(profile, 'id', 'default')}_{uuid.uuid4().hex}",
                ),
                timeout=600.0,
            )
        except Exception as exc:
            prof_name = getattr(profile, "name", "unknown")
            logger.warning("Voice profile '%s' warm-up encountered error: %s", prof_name, exc)
            return False

    async def _resolve_switch_target(
        self,
        target: int | str | VoiceProfileResponse | VoiceProfileInDB | dict[str, Any] | Any,
    ) -> Any | None:
        """Resolves target identifier into a DB voice profile model or object."""
        if isinstance(target, int) or (isinstance(target, str) and target.isdigit()):
            profile_id = int(target)
            async with get_db(self.db_path) as conn:
                db_profile = await crud.get_voice_profile(conn, profile_id)
                if not db_profile:
                    logger.error("Voice profile ID %d not found in database", profile_id)
                    return None
                return db_profile

        if isinstance(target, str):
            async with get_db(self.db_path) as conn:
                db_profile = await crud.get_voice_profile_by_name(conn, target)
                if db_profile:
                    return db_profile
                logger.warning("Voice profile name '%s' not found in database", target)
                logger.error("Cannot switch voice profile: unresolved string target '%s'", target)
                return None

        return target

    def _check_switch_memory_guard(self, force: bool) -> None:
        """Keep manual and per-inference model loads on the same resource policy."""
        VoiceResourceGuard(memory_status=get_system_memory_status, gpu_status=get_gpu_vram_status,
                           release=release_system_memory).check(force=force)

    async def _persist_active_switch_profile(self, profile_obj: Any) -> None:
        """Persists the newly active voice profile ID into SQLite settings."""
        profile_id = None
        if hasattr(profile_obj, "id") and profile_obj.id is not None:
            profile_id = profile_obj.id
        elif isinstance(profile_obj, dict) and "id" in profile_obj:
            profile_id = profile_obj["id"]

        if profile_id:
            await persist_active_voice_profile(profile_id, db_path=self.db_path)

    async def _execute_switch(
        self,
        target: int | str | VoiceProfileResponse | VoiceProfileInDB | dict[str, Any] | Any,
        persist: bool = True,
        force: bool = False,
    ) -> bool:
        # 1. Resolve Profile from DB if ID or Name provided
        profile_obj = await self._resolve_switch_target(target)
        if profile_obj is None:
            return False

        # Memory precheck: new and old weights briefly co-reside during a switch; loading
        # with too little free memory OOM-crashes the engine. Sits here (not in the HTTP
        # layer) so every call path — REST, Telegram, auto-bind — gets the same guard.
        await asyncio.to_thread(self._check_switch_memory_guard, force)

        # 2. Execute 3-step atomic model switch with auto-rollback.
        # The id is published before the slow load and cleared only after commit or
        # rollback, so a delete can refuse this voice without waiting on the lock.
        await asyncio.to_thread(release_system_memory)
        tracked_id = _stored_profile_id(profile_obj)
        try:
            if tracked_id is not None:
                self._switching_profile_id = tracked_id
            previous = self.client.active_profile
            success = await self.client.switch_voice_profile(profile_obj, force=force)

            async def finalize():
                await asyncio.to_thread(release_system_memory)
                # Once the engine confirmed a switch, cancellation must not leave
                # its saved selection behind merely because cleanup is still slow.
                if success and tracked_id is not None and await self.get_profile(tracked_id) is None:
                    await self._restore_loaded_profile(previous, tracked_id)
                    raise VoiceProfileRemoved(tracked_id)
                if success and persist:
                    await self._persist_active_switch_profile(profile_obj)

            await await_protected(finalize())
        finally:
            if tracked_id is not None and self._switching_profile_id == tracked_id:
                self._switching_profile_id = None
        if not success:
            logger.error("Failed to switch GPT-SoVITS model weights for target: %s", target)
            return False

        # Trigger non-blocking background warm-up of newly activated voice profile
        try:
            self._spawn_background(self.warmup_current_profile())
        except Exception as warmup_err:
            logger.debug("Could not schedule warm-up task on profile switch: %s", warmup_err)

        return True

    async def _restore_loaded_profile(self, previous: Any, removed_id: int) -> None:
        """Put the engine back on the voice it had before a removed profile was loaded."""
        previous_id = getattr(previous, "id", None)
        if previous is not None and previous_id != removed_id:
            if await self.client.switch_voice_profile(previous, force=True):
                return
        if getattr(self.client.active_profile, "id", None) == removed_id:
            self.client.active_profile = None

    # ========================================================================
    # Synthesis & Streaming
    # ========================================================================

    async def _resolve_active_options(self, options: dict[str, Any] | None) -> dict[str, Any]:
        """Normalize legacy selection; TtsService resolves the actual snapshot."""
        opts = dict(options or {})
        legacy_id = opts.pop("profile_id", None)
        if opts.get("voice_profile_id") is None and legacy_id is not None:
            opts["voice_profile_id"] = legacy_id
        return opts

    async def synthesize(
        self,
        text: str,
        options: dict[str, Any] | None = None,
        use_cache: bool = True,
    ) -> bytes:
        """Synthesizes text into complete audio bytes using active weights, persistent cache and inference mutex."""
        opts = await self._resolve_active_options(options)
        return await self.tts_service.synthesize(text, options=opts, use_cache=use_cache)

    async def stream_tts(
        self,
        text: str,
        options: dict[str, Any] | None = None,
        chunk_size: int = 4096,
        use_cache: bool = True,
    ) -> AsyncGenerator[bytes, None]:
        """Use the same cache, identity, scheduling and cancellation path as TtsService."""
        opts = await self._resolve_active_options(options)
        effective_use_cache = bool(opts.get("use_cache", use_cache))
        async with aclosing(self.tts_service.stream_tts(
            text, options=opts, chunk_size=chunk_size, use_cache=effective_use_cache,
        )) as stream:
            async for chunk in stream:
                yield chunk

    # ========================================================================
    # Voice Profile Database CRUD Operations
    # ========================================================================

    async def list_profiles(self) -> list[VoiceProfileResponse]:
        """Lists all voice profiles in database."""
        async with get_db(self.db_path) as conn:
            return await crud.list_voice_profiles(conn)

    async def get_profile(self, profile_id: int) -> VoiceProfileResponse | None:
        """Gets voice profile by ID."""
        async with get_db(self.db_path) as conn:
            return await crud.get_voice_profile(conn, profile_id)

    async def get_active_profile(self) -> VoiceProfileResponse | None:
        """Gets currently configured active voice profile from database."""
        async with get_db(self.db_path) as conn:
            return await crud.get_active_voice_profile(conn)

    async def create_profile(self, profile: VoiceProfileCreate) -> VoiceProfileResponse:
        """Creates a new voice profile in database."""
        async with get_db(self.db_path) as conn:
            res = await crud.create_voice_profile(conn, profile)
        return res

    async def update_profile(
        self, profile_id: int, updates: VoiceProfileUpdate
    ) -> VoiceProfileResponse | None:
        """Updates an existing voice profile in database."""
        async with get_db(self.db_path) as conn:
            res = await crud.update_voice_profile(conn, profile_id, updates)
        return res

    async def delete_profile(self, profile_id: int) -> bool:
        """Deletes a voice profile from database."""
        async with get_db(self.db_path) as conn:
            res = await crud.delete_voice_profile(conn, profile_id)
        return res


# ============================================================================
# Global Singleton Accessor
# ============================================================================

_global_voice_manager: VoiceManager | None = None


def _stored_profile_id(profile_obj: Any) -> int | None:
    """Database voice rows are the only targets a concurrent delete can remove."""
    if isinstance(profile_obj, (VoiceProfileResponse, VoiceProfileInDB)):
        profile_id = profile_obj.id
        if isinstance(profile_id, int) and profile_id >= 1:
            return profile_id
    return None


def get_voice_manager() -> VoiceManager:
    """Returns application singleton VoiceManager instance."""
    global _global_voice_manager
    if _global_voice_manager is None:
        _global_voice_manager = VoiceManager()
    return _global_voice_manager


def set_voice_manager(manager: VoiceManager | None) -> None:
    """Sets or resets application singleton VoiceManager instance (useful for tests)."""
    global _global_voice_manager
    _global_voice_manager = manager


__all__ = [
    "VoiceManager",
    "get_voice_manager",
    "set_voice_manager",
]
