"""
Voice Profile Manager for galgame2voice.
Coordinates SQLite voice_profiles persistence with GptSovitsClient model switching,
mutex locking, and atomic rollback on failure.
"""

import asyncio
import logging
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
    clean_japanese_parentheses,
    resolve_tts_options,
)

logger = logging.getLogger("galgame2voice.services.voice_manager")


class VoiceManager:
    """
    Coordinates character voice profile management and atomic model switching with GPT-SoVITS.
    Ensures thread-safe operations via an inference mutex and atomic SQLite persistence.
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
            self.client = GptSovitsClient(base_url=settings.gpt_sovits_base_url)

    @property
    def lock(self) -> asyncio.Lock:
        """Shared inference and model switching mutex."""
        return self.client.lock

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
    ) -> bool:
        """
        Atomically switches GPT-SoVITS weights to target voice profile.
        If target is an int ID or string ID/name, looks up profile from SQLite DB.
        On success, updates SQLite active profile if persist=True.
        On failure, automatically rolls back weights and preserves prior state.
        """
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
                conn.row_factory = aiosqlite.Row
                cursor = await conn.execute("SELECT * FROM voice_profiles WHERE name = ? LIMIT 1;", (target,))
                row = await cursor.fetchone()
                if row:
                    profile_obj = VoiceProfileResponse(**dict(row))
                else:
                    logger.warning("Voice profile name '%s' not found in database; treating as raw object if possible", target)

        # 2. Execute 3-step atomic model switch with auto-rollback
        success = await self.client.switch_voice_profile(profile_obj)
        if not success:
            logger.error("Failed to switch GPT-SoVITS model weights for target: %s", target)
            return False

        # 3. Update Persistence in SQLite
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
            try:
                active = await self.get_active_profile()
                if active:
                    opts.setdefault("ref_audio_path", active.ref_audio_path)
                    opts.setdefault("prompt_text", active.prompt_text)
                    opts.setdefault("prompt_lang", active.prompt_lang)
                    opts.setdefault("text_lang", active.text_lang)
            except Exception as exc:
                logger.debug("Could not auto-populate active profile options: %s", exc)
        return opts

    async def synthesize(self, text: str, options: Optional[Dict[str, Any]] = None) -> bytes:
        """Synthesizes text into complete audio bytes using active weights and inference mutex."""
        opts = await self._resolve_active_options(options)
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
