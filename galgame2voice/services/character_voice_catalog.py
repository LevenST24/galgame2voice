"""
Unified Character & Voice Catalog coordinator for galgame2voice.
Coordinates character manifest staging/replacement, SQLite voice_profiles CRUD,
session/default unbinding, package rescanning, and VoiceManager switching/warmup lifecycle.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import json
import logging
import os
from pathlib import Path
from typing import Any
from weakref import WeakKeyDictionary

import aiosqlite
from fastapi import HTTPException, status

from galgame2voice.database import crud
from galgame2voice.database.models import (
    VoiceProfileCreate,
    VoiceProfileResponse,
    VoiceProfileUpdate,
)
from galgame2voice.database.session import _run_uncancellable, get_db, immediate_transaction
from galgame2voice.services.character_identity import (
    AmbiguousProfileName,
    DuplicateCharacterId,
    owned_package_for_profile,
    resolve_profile_for_query,
)
from galgame2voice.services.character_manager import get_character_manager
from galgame2voice.services.voice_manager import VoiceManager, get_voice_manager
from galgame2voice.utils.async_tasks import await_protected
from galgame2voice.utils.logger import sanitize_error_detail
from galgame2voice.utils.path_guard import (
    PathTraversalError,
    to_project_relative_path,
    validate_voice_profile_paths,
)

logger = logging.getLogger("galgame2voice.services.character_voice_catalog")


class VoiceProfileBusyError(RuntimeError):
    """Raised when deleting a voice profile whose weights are currently loading."""

    def __init__(self, profile_id: int) -> None:
        super().__init__("这个音色正在加载，请稍后再删除")
        self.profile_id = profile_id


def _restore_manifest(manifest_path: Path, original: bytes) -> None:
    """Atomically restores manifest.json back to original bytes."""
    tmp_path = manifest_path.with_name("manifest.json.tmp")
    try:
        tmp_path.write_bytes(original)
        os.replace(tmp_path, manifest_path)
    except OSError:
        tmp_path.unlink(missing_ok=True)
        raise


async def _rollback_manifest_and_cache(
    manifest_path: Path,
    original: bytes,
    mgr: Any,
) -> bool:
    """Restores manifest.json after an aborted post-replace step and refreshes CharacterManager."""
    restored = True
    try:
        _restore_manifest(manifest_path, original)
    except OSError as restore_exc:
        restored = False
        logger.warning("回滚角色包 manifest.json 失败: %s", sanitize_error_detail(restore_exc))
    _, exc, _ = await _run_uncancellable(asyncio.to_thread(mgr.discover_characters))
    if exc is not None:
        logger.warning("回滚角色包后刷新内存缓存失败: %s", exc)
    return restored


def _stage_manifest_system_prompt(
    manifest_path: Path,
    original_manifest: bytes,
    text: str,
) -> Path:
    """Parses manifest.json and writes updated system_prompt to manifest.json.tmp only."""
    try:
        data = json.loads(original_manifest.decode("utf-8-sig"))
    except Exception as exc:
        safe_err = sanitize_error_detail(exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"无法解析角色 manifest.json: {safe_err}",
        ) from exc

    if not isinstance(data, dict):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="无法解析角色 manifest.json: 根节点必须为 JSON 对象",
        )

    data["system_prompt"] = text
    tmp_path = manifest_path.with_name("manifest.json.tmp")
    try:
        tmp_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as exc:
        tmp_path.unlink(missing_ok=True)
        safe_err = sanitize_error_detail(exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"写入角色 manifest.json 失败: {safe_err}",
        ) from exc
    return tmp_path


class CharacterVoiceCatalog:
    """Single coordinator for character manifest files, voice_profiles rows, sessions, and VoiceManager."""

    def __init__(
        self,
        *,
        character_manager: Any | None = None,
        voice_manager: Any | None = None,
        db_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._character_manager = character_manager
        self._voice_manager = voice_manager
        self._db_factory = db_factory
        self._persona_locks: WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = WeakKeyDictionary()
        self._rescan_locks: WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = WeakKeyDictionary()

    def _resolve_character_manager(self, override: Any | None = None) -> Any:
        return override if override is not None else (self._character_manager or get_character_manager())

    def _resolve_voice_manager(self, override: Any | None = None) -> Any:
        return override if override is not None else (self._voice_manager or get_voice_manager())

    def _resolve_db_factory(self, override: Callable[[], Any] | None = None) -> Callable[[], Any]:
        return override if override is not None else (self._db_factory or get_db)

    def _get_loop_lock(
        self,
        registry: WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock],
    ) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        lock = registry.get(loop)
        if lock is None:
            lock = asyncio.Lock()
            registry[loop] = lock
        return lock

    async def update_persona(
        self,
        character_id: int,
        system_prompt: str,
        *,
        update_profile_fn: Callable[..., Any] | None = None,
        character_manager: Any | None = None,
        db_factory: Callable[[], Any] | None = None,
    ) -> dict[str, Any]:
        text = system_prompt.strip()
        if not text:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="人设提示词不能为空",
            )

        open_db = self._resolve_db_factory(db_factory)
        mgr = self._resolve_character_manager(character_manager)
        update_fn = update_profile_fn or crud.update_voice_profile

        async with self._get_loop_lock(self._persona_locks):
            async with open_db() as conn:
                prof = await crud.get_voice_profile(conn, character_id)
                if not prof:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail=f"Character with ID {character_id} not found",
                    )
                profiles = await crud.list_voice_profiles(conn)

            pkg = owned_package_for_profile(prof, profiles, mgr)
            manifest_path: Path | None = None
            original_manifest: bytes | None = None
            tmp_path: Path | None = None

            if pkg is not None:
                manifest_path = pkg.folder / "manifest.json"
                if not manifest_path.is_file():
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail=f"角色包缺少 manifest.json: {manifest_path}",
                    )
                original_manifest = manifest_path.read_bytes()
                tmp_path = _stage_manifest_system_prompt(manifest_path, original_manifest, text)

            original_prompt = getattr(prof, "system_prompt", "") or ""
            manifest_replaced = False
            db_committed = False
            try:
                async with open_db() as conn:
                    async with immediate_transaction(conn):
                        updated = await update_fn(
                            conn, character_id, VoiceProfileUpdate(system_prompt=text)
                        )
                        if updated is None:
                            raise HTTPException(
                                status_code=status.HTTP_404_NOT_FOUND,
                                detail=f"Character with ID {character_id} not found",
                            )
                        if tmp_path is not None and manifest_path is not None:
                            try:
                                os.replace(tmp_path, manifest_path)
                            except OSError as exc:
                                safe_err = sanitize_error_detail(exc)
                                raise HTTPException(
                                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                                    detail=f"写入角色 manifest.json 失败: {safe_err}",
                                ) from exc
                            manifest_replaced = True
                            tmp_path = None
                db_committed = True

                # Run filesystem scan outside immediate_transaction so SQLite write lock
                # is not held during audio duration probing, while _persona_locks still
                # serializes overlapping persona saves.
                if manifest_replaced:
                    await asyncio.to_thread(mgr.discover_characters)
            except BaseException as exc:
                if tmp_path is not None:
                    tmp_path.unlink(missing_ok=True)
                if manifest_replaced and manifest_path is not None and original_manifest is not None:
                    async def _compensate_replaced_manifest() -> None:
                        if db_committed:
                            try:
                                async with open_db() as rb_conn:
                                    async with immediate_transaction(rb_conn):
                                        await crud.update_voice_profile(
                                            rb_conn,
                                            character_id,
                                            VoiceProfileUpdate(system_prompt=original_prompt),
                                        )
                            except Exception as rb_exc:
                                logger.warning(
                                    "回滚角色人设数据库记录失败: %s",
                                    sanitize_error_detail(rb_exc),
                                )
                        await _rollback_manifest_and_cache(manifest_path, original_manifest, mgr)

                    await await_protected(_compensate_replaced_manifest())
                if isinstance(exc, DuplicateCharacterId):
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=str(exc),
                    ) from exc
                if not isinstance(exc, Exception):
                    raise
                if isinstance(exc, HTTPException):
                    raise
                safe_err = sanitize_error_detail(exc)
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"人设保存到数据库失败: {safe_err}",
                ) from exc

        logger.info("System prompt updated for character %s (%d chars)", prof.name, len(text))
        return {
            "character_id": character_id,
            "name": prof.name,
            "system_prompt": text,
            "length": len(text),
            "manifest_path": str(manifest_path) if manifest_path is not None else None,
        }

    async def list_profiles(
        self,
        *,
        db_factory: Callable[[], Any] | None = None,
    ) -> list[VoiceProfileResponse]:
        open_db = self._resolve_db_factory(db_factory)
        async with open_db() as conn:
            return await crud.list_voice_profiles(conn)

    async def get_profile(
        self,
        profile_id: int,
        *,
        db_factory: Callable[[], Any] | None = None,
    ) -> VoiceProfileResponse | None:
        open_db = self._resolve_db_factory(db_factory)
        async with open_db() as conn:
            return await crud.get_voice_profile(conn, profile_id)

    async def get_active_profile(
        self,
        *,
        db_factory: Callable[[], Any] | None = None,
    ) -> VoiceProfileResponse | None:
        open_db = self._resolve_db_factory(db_factory)
        async with open_db() as conn:
            return await crud.get_active_voice_profile(conn)

    async def create_profile(
        self,
        profile: VoiceProfileCreate,
        *,
        validate_paths: bool = False,
        db_factory: Callable[[], Any] | None = None,
    ) -> VoiceProfileResponse:
        if validate_paths:
            if not profile.name.strip():
                raise HTTPException(status_code=422, detail="音色名称不能为空")
            if not profile.gpt_weights_path.strip():
                raise HTTPException(status_code=422, detail="GPT 权重路径不能为空")
            if not profile.sovits_weights_path.strip():
                raise HTTPException(status_code=422, detail="SoVITS 权重路径不能为空")
            try:
                validate_voice_profile_paths(
                    gpt_weights_path=profile.gpt_weights_path.strip(),
                    sovits_weights_path=profile.sovits_weights_path.strip(),
                    ref_audio_path=profile.ref_audio_path.strip() if profile.ref_audio_path else None,
                )
            except PathTraversalError as pte:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid file path: {pte}",
                ) from pte

        open_db = self._resolve_db_factory(db_factory)
        async with open_db() as conn:
            return await crud.create_voice_profile(conn, profile)

    async def update_profile(
        self,
        profile_id: int,
        updates: VoiceProfileUpdate,
        *,
        validate_paths: bool = False,
        db_factory: Callable[[], Any] | None = None,
    ) -> VoiceProfileResponse | None:
        if validate_paths:
            if updates.name is not None and not updates.name.strip():
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="音色名称不能为空",
                )
            if updates.gpt_weights_path is not None and not updates.gpt_weights_path.strip():
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="GPT 权重路径不能为空",
                )
            if updates.sovits_weights_path is not None and not updates.sovits_weights_path.strip():
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="SoVITS 权重路径不能为空",
                )
            try:
                validate_voice_profile_paths(
                    gpt_weights_path=updates.gpt_weights_path.strip() if updates.gpt_weights_path else None,
                    sovits_weights_path=updates.sovits_weights_path.strip() if updates.sovits_weights_path else None,
                    ref_audio_path=updates.ref_audio_path.strip() if updates.ref_audio_path else None,
                )
            except PathTraversalError as pte:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid file path: {pte}",
                ) from pte
            if updates.ref_audio_path:
                updates.ref_audio_path = to_project_relative_path(updates.ref_audio_path)

        open_db = self._resolve_db_factory(db_factory)
        async with open_db() as conn:
            return await crud.update_voice_profile(conn, profile_id, updates)

    async def delete_profile(
        self,
        profile_id: int,
        *,
        voice_manager: VoiceManager | None = None,
        db_factory: Callable[[], Any] | None = None,
    ) -> bool:
        vm = self._resolve_voice_manager(voice_manager)
        if vm.switching_profile_id == profile_id:
            raise VoiceProfileBusyError(profile_id)

        vm.cancel_warmup(profile_id)
        open_db = self._resolve_db_factory(db_factory)
        async with open_db() as conn:
            deleted = await crud.delete_voice_profile(conn, profile_id)

        if deleted:
            vm.cancel_warmup(profile_id)
            if VoiceManager._resolve_profile_field(vm.active_profile, "id") == profile_id:
                vm.active_profile = None
        return deleted

    async def rescan_packages(
        self,
        *,
        lock: asyncio.Lock | None = None,
        character_manager: Any | None = None,
        db_factory: Callable[[], Any] | None = None,
    ) -> dict[str, Any]:
        effective_lock = lock if lock is not None else self._get_loop_lock(self._rescan_locks)
        async with effective_lock:
            char_mgr = self._resolve_character_manager(character_manager)
            open_db = self._resolve_db_factory(db_factory)
            discovered = await asyncio.to_thread(char_mgr.discover_characters)
            async with open_db() as conn:
                synced = await char_mgr.sync_with_db(conn)
                profiles = await crud.list_voice_profiles(conn)
                active = await crud.get_active_voice_profile(conn)
            import galgame2voice.routers.voice as voice_router

            scan_cache = voice_router._scan_cache
            generation = scan_cache.get("generation", 0) + 1
            scan_cache.clear()
            scan_cache["generation"] = generation
            return {
                "status": "ok",
                "discovered_count": len(discovered),
                "synced_count": synced,
                "total_profiles": len(profiles),
                "active_profile_id": active.id if active else (profiles[0].id if profiles else None),
                "characters": [p.name for p in discovered],
                "catalog_warning": char_mgr.catalog_warning,
            }

    async def resolve_profile_by_name(
        self,
        conn: aiosqlite.Connection,
        query_name: str,
        *,
        character_manager: Any | None = None,
    ) -> Any | None:
        cm = self._resolve_character_manager(character_manager)

        async def find_profile() -> tuple[Any | None, Any | None]:
            profiles = await crud.list_voice_profiles(conn)
            try:
                matched, pkg, conflicting = resolve_profile_for_query(
                    profiles, query_name, manager=cm
                )
            except AmbiguousProfileName as exc:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=str(exc),
                ) from exc

            if conflicting:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"角色“{query_name}”匹配到多个角色包；请使用唯一的角色 ID。",
                )
            return matched, pkg

        profile, pkg = await find_profile()
        if profile is None and pkg is not None:
            await cm.sync_with_db(conn)
            profile, _ = await find_profile()
        return profile

    async def lookup_profile(
        self,
        conn: aiosqlite.Connection,
        profile_id: int | None,
        profile_name: str | None,
        *,
        character_manager: Any | None = None,
    ) -> Any | None:
        if profile_id is not None:
            return await crud.get_voice_profile(conn, profile_id)
        if profile_name:
            return await self.resolve_profile_by_name(
                conn, profile_name, character_manager=character_manager
            )
        return None

    async def switch_active_profile(
        self,
        *,
        profile_id: int | None = None,
        profile_name: str | None = None,
        force: bool = False,
        entity_label: str = "Voice profile",
        id_field_name: str = "profile_id",
        missing_detail: str = "Missing profile_id or profile_name in switch request",
        character_manager: Any | None = None,
        voice_manager: Any | None = None,
        db_factory: Callable[[], Any] | None = None,
    ) -> VoiceProfileResponse:
        from galgame2voice.routers.common import (
            persist_active_voice_profile_or_raise,
            switch_voice_profile_or_raise,
            validate_positive_profile_id,
        )

        clean_name = profile_name.strip() if profile_name else None

        if profile_id is None and not clean_name:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=missing_detail,
            )

        if profile_id is not None:
            validate_positive_profile_id(profile_id, field_name=id_field_name)

        open_db = self._resolve_db_factory(db_factory)
        cm = self._resolve_character_manager(character_manager)
        identifier = profile_id if profile_id is not None else clean_name

        # 1. 404 Precedence: Resolve entity before waiting on manager.switch_lock
        async with open_db() as conn:
            profile = await self.lookup_profile(
                conn, profile_id, clean_name, character_manager=cm
            )
            if not profile:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"{entity_label} '{identifier}' not found",
                )

        manager = self._resolve_voice_manager(voice_manager)

        # 2. Concurrency & TOCTOU Guard under manager.switch_lock
        async with manager.switch_lock:
            async with open_db() as conn:
                verified_profile = await crud.get_voice_profile(conn, profile.id)
                if not verified_profile:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail=f"{entity_label} '{identifier}' not found",
                    )
                profile = verified_profile

            is_already_active = await asyncio.to_thread(
                manager.is_active_profile, profile, force=force
            )

            if is_already_active:
                await persist_active_voice_profile_or_raise(profile.id)
            else:
                await switch_voice_profile_or_raise(manager, profile, force=force)

            return profile


_global_catalog: CharacterVoiceCatalog | None = None


def get_character_voice_catalog() -> CharacterVoiceCatalog:
    """Returns the process-wide CharacterVoiceCatalog coordinator."""
    global _global_catalog
    if _global_catalog is None:
        _global_catalog = CharacterVoiceCatalog()
    return _global_catalog
