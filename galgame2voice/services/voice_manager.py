"""
Voice Profile Manager for galgame2voice.
Coordinates SQLite voice_profiles persistence with GptSovitsClient model switching,
mutex locking, and atomic rollback on failure.
"""

import asyncio
import logging
import os
from typing import Any, AsyncGenerator, Dict, List, Optional, Union

import aiosqlite

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
from galgame2voice.utils.hardware import get_system_memory_status, release_system_memory

logger = logging.getLogger("galgame2voice.services.voice_manager")


class InsufficientMemoryError(RuntimeError):
    """Raised when free memory is too low to safely load new model weights."""


# 切换权重时新旧模型会短暂同时驻留内存（GPT约155MB + SoVITS约172MB = 约330MB，极端过渡期约660MB）；
# 默认安全阈值：总内存的 6% 或最低 0.8GB，并设有 1.5GB 安全上限（大内存机器不会被百分比过度拦截）。
# 16GB 设备所需空闲仅约 0.96GB，只要空闲内存大于 1GB 即可丝滑切换。
_MIN_FREE_MEMORY_RATIO = 0.06
_MIN_FREE_MEMORY_FLOOR_GB = 0.8
_MIN_FREE_MEMORY_CEILING_GB = 1.5


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
    def server(self, val: Any):
        self.client.server = val

    @property
    def active_profile(self) -> Optional[Any]:
        """Currently active voice profile model."""
        return self.client.active_profile

    @active_profile.setter
    def active_profile(self, val: Any):
        self.client.active_profile = val

    @property
    def is_switching(self) -> bool:
        """Whether a model switch is currently in progress."""
        return self.client.is_switching

    # ========================================================================
    # Voice Profile Switching (Atomic 3-Step + Persistence)
    # ========================================================================

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
            if hasattr(profile_obj, "id") and getattr(profile_obj, "id") is not None:
                profile_id = getattr(profile_obj, "id")
            elif isinstance(profile_obj, dict) and "id" in profile_obj:
                profile_id = profile_obj["id"]

            if profile_id:
                try:
                    async with get_db(self.db_path) as conn:
                        await crud.set_active_voice_profile(conn, profile_id)
                        logger.info("Persisted active voice profile ID %d in settings", profile_id)
                except Exception as exc:
                    logger.warning("Could not persist active voice profile ID to DB: %s", exc)

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
    ) -> AsyncGenerator[bytes, None]:
        """Streams synthesized audio in binary chunks using inference mutex."""
        opts = await self._resolve_active_options(options)
        async for chunk in self.client.stream_tts(text, options=opts, chunk_size=chunk_size):
            yield chunk

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
            return await crud.create_voice_profile(conn, profile)

    async def update_profile(
        self, profile_id: int, updates: VoiceProfileUpdate
    ) -> Optional[VoiceProfileResponse]:
        """Updates an existing voice profile in database."""
        async with get_db(self.db_path) as conn:
            return await crud.update_voice_profile(conn, profile_id, updates)

    async def delete_profile(self, profile_id: int) -> bool:
        """Deletes a voice profile from database."""
        async with get_db(self.db_path) as conn:
            return await crud.delete_voice_profile(conn, profile_id)


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
