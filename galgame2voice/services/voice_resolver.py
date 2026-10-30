"""
Voice Profile Resolver and In-Memory Resolved Context for galgame2voice.

Caches committed voice snapshots within their database and complete selector.
Cold loads coalesce, and reference/model path preparation runs in a worker.
Repository writes invalidate all affected selectors only after a durable commit.
"""

import asyncio
from collections import OrderedDict
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any
from weakref import WeakValueDictionary

from galgame2voice.services.character_identity import AmbiguousProfileName, owned_package_for_profile

from galgame2voice.utils.path_guard import resolve_weight_file_path, resolve_existing_audio_path
from galgame2voice.utils.audio_spec import (
    REFERENCE_AUDIO_MAX_SECONDS,
    REFERENCE_AUDIO_MIN_SECONDS,
)
from galgame2voice.services.gpt_sovits_client import probe_audio_duration_seconds
from galgame2voice.services.emotion_references import resolve_emotion_reference

logger = logging.getLogger("galgame2voice.services.voice_resolver")

_PACKAGE_UNSET = object()

class VoiceContextChangedError(RuntimeError):
    """A stable committed voice snapshot could not be obtained in time."""


class VoiceContextUnavailableError(ValueError):
    """A voice context could not be read or built; absence is not established."""


DEFAULT_EMOTION_KEYS: tuple[str, ...] = (
    "joy",
    "anger",
    "sorrow",
    "fun",
    "surprise",
    "fear",
    "shyness",
    "neutral",
    "tsundere",
    "yandere",
    "gentle",
)


@dataclass
class ResolvedVoiceContext:
    """Pre-resolved snapshot of a committed voice profile and its references."""
    profile_id: int | None
    name: str
    gpt_weights_path: str
    sovits_weights_path: str
    ref_audio_path: str
    prompt_text: str
    prompt_lang: str
    text_lang: str
    emotions: dict[str, dict[str, str]] = field(default_factory=dict)
    resolved_at: float = field(default_factory=time.monotonic)

    def get_emotion_ref(self, emotion: str) -> dict[str, str] | None:
        """Returns pre-validated emotion reference audio options if available."""
        if not emotion:
            return None
        return self.emotions.get(str(emotion).strip().lower())


def _extract_field(raw: Any, key: str, default: Any = "") -> Any:
    """Safely extracts a field from a model, dataclass, object or dict."""
    if hasattr(raw, key):
        return getattr(raw, key)
    if isinstance(raw, dict):
        return raw.get(key, default)
    return default


def database_scope(db_path: str | os.PathLike | None = None) -> str:
    """Use the same default DB as get_db, independent of selector and path spelling."""
    from galgame2voice.database.session import get_database_path

    return os.path.normcase(os.path.realpath(os.path.abspath(os.fspath(db_path or get_database_path()))))


@dataclass(frozen=True)
class _CacheEntry:
    expires_at: float
    context: ResolvedVoiceContext | None


@dataclass
class _ScopeGeneration:
    value: int = 0


class VoiceProfileResolver:
    """
    Maintains in-memory memoization of resolved voice profiles.
    Hot TTS paths query this resolver to avoid repeated SQLite transactions
    and filesystem duration probes.
    """

    def __init__(self, ttl_seconds: float = 300.0, *, negative_ttl_seconds: float = 1.0, max_entries: int = 256) -> None:
        self._ttl = ttl_seconds
        self._negative_ttl = negative_ttl_seconds
        self._max_entries = max_entries
        self._cache: OrderedDict[tuple[str, int | None, str | None], _CacheEntry] = OrderedDict()
        self._load_locks: WeakValueDictionary = WeakValueDictionary()
        self._scope_generations: WeakValueDictionary = WeakValueDictionary()
        self._generation = 0

    def invalidate(self, profile_id: int | None = None, name: str | None = None, *, db_path: str | None = None) -> None:
        """Evict a DB's selectors together; legacy unscoped calls evict all scopes."""
        if db_path is None:
            self._generation += 1
            self._cache.clear()
        else:
            scope = database_scope(db_path)
            pending = self._scope_generations.get(scope)
            if pending is not None:
                pending.value += 1
            for key in list(self._cache):
                if key[0] == scope:
                    self._cache.pop(key, None)

    def _cached(self, key: tuple[str, int | None, str | None]) -> tuple[bool, ResolvedVoiceContext | None]:
        entry = self._cache.get(key)
        if entry is not None:
            if time.monotonic() < entry.expires_at:
                self._cache.move_to_end(key)
                return True, entry.context
            self._cache.pop(key, None)
        return False, None

    async def resolve_context(
        self,
        db_path: str | None = None,
        profile_id: int | None = None,
        character_name: str | None = None,
    ) -> ResolvedVoiceContext | None:
        """
        Resolves a voice profile from memory cache, falling back to SQLite and package manifests.
        """
        scope = database_scope(db_path)
        cache_key = (scope, profile_id, character_name.strip() if character_name else None)
        found, context = self._cached(cache_key)
        if found:
            return context

        lock_key = (asyncio.get_running_loop(), cache_key)
        lock = self._load_locks.get(lock_key)
        if lock is None:
            lock = asyncio.Lock()
            self._load_locks[lock_key] = lock
        async with lock:
            found, context = self._cached(cache_key)
            if found:
                return context
            scope_generation = self._scope_generations.get(scope)
            if scope_generation is None:
                scope_generation = _ScopeGeneration()
                self._scope_generations[scope] = scope_generation
            # A commit may land during either the DB read or the worker's
            # filesystem probes. Never return that superseded snapshot to TTS.
            for _ in range(3):
                generation = (self._generation, scope_generation.value)
                try:
                    raw_profile = await self._fetch_raw_profile(db_path, profile_id, character_name)
                    context = None
                    if raw_profile is not None:
                        package = await self._package_for_profile(db_path, raw_profile)
                        context = await asyncio.to_thread(self._build_context, raw_profile, package)
                except AmbiguousProfileName:
                    if generation != (self._generation, scope_generation.value):
                        continue
                    raise
                except Exception as exc:
                    if generation != (self._generation, scope_generation.value):
                        continue
                    logger.warning("Voice context unavailable; the next request will retry: %s", exc)
                    raise VoiceContextUnavailableError("音色配置暂时无法读取，请稍后重试。") from exc
                if generation != (self._generation, scope_generation.value):
                    continue
                ttl = self._ttl if context is not None else self._negative_ttl
                self._cache[cache_key] = _CacheEntry(time.monotonic() + ttl, context)
                self._cache.move_to_end(cache_key)
                while len(self._cache) > max(0, self._max_entries):
                    self._cache.popitem(last=False)
                return context
            raise VoiceContextChangedError("音色配置正在更新，请稍后重试。")

    async def _fetch_raw_profile(
        self,
        db_path: str | None,
        profile_id: int | None,
        character_name: str | None,
    ) -> Any | None:
        from galgame2voice.database.session import get_db
        from galgame2voice.services.voice_selection import select_voice_profile

        async with get_db(db_path) as conn:
            return await select_voice_profile(conn, profile_id, character_name)

    async def _package_for_profile(self, db_path: str | None, raw: Any) -> Any | None:
        """Package resources belong to the profile the package owns, not a lookalike name."""
        from galgame2voice.database import crud
        from galgame2voice.database.session import get_db
        from galgame2voice.services.character_manager import get_character_manager

        manager = get_character_manager()
        name = _extract_field(raw, "name", "") or ""
        try:
            if manager.resolve_identity(name) is None:
                return None  # No package claims this name, so no profile list is needed.
        except Exception as exc:
            # Same as _build_context: an unreadable catalog leaves the voice without package resources.
            logger.debug("Failed looking up character package for '%s': %s", name, exc)
            return None
        async with get_db(db_path) as conn:
            profiles = await crud.list_voice_profiles(conn)
        return owned_package_for_profile(raw, profiles, manager)

    def _build_context(self, raw: Any, package: Any = _PACKAGE_UNSET) -> ResolvedVoiceContext:
        """Constructs a validated ResolvedVoiceContext from a raw DB model or dict.

        ``package`` is the package that owns this profile (None for a separate voice).
        """
        pid = _extract_field(raw, "id", None)
        name = (_extract_field(raw, "name", "Default") or "Default").strip()

        gpt_path = _extract_field(raw, "gpt_weights_path", "") or ""
        sovits_path = _extract_field(raw, "sovits_weights_path", "") or ""
        ref_audio = _extract_field(raw, "ref_audio_path", "") or ""
        prompt_text = _extract_field(raw, "prompt_text", "") or ""
        prompt_lang = _extract_field(raw, "prompt_lang", "ja") or "ja"
        text_lang = _extract_field(raw, "text_lang", "ja") or "ja"

        # Absolutize weight paths
        abs_gpt = resolve_weight_file_path(gpt_path)
        abs_sovits = resolve_weight_file_path(sovits_path)

        pkg = None
        is_conflicting = False
        try:
            from galgame2voice.services.character_manager import CharacterManager, get_character_manager
            mgr = get_character_manager()
            pkg = mgr.get_character(name) if package is _PACKAGE_UNSET else package
            is_conflicting = bool(
                pkg is None
                and getattr(mgr, "_identity_index", None)
                and mgr._identity_index.is_conflicting(name)
            )
        except Exception as exc:
            logger.debug("Failed looking up character package for '%s': %s", name, exc)

        if pkg is not None and pkg.is_valid:
            is_cross_char = bool(ref_audio and CharacterManager._is_cross_character_audio(ref_audio, pkg))
            own_ref = (
                CharacterManager._resolve_profile_reference(ref_audio, pkg)
                if ref_audio and not is_cross_char
                else None
            )
            if own_ref is None and ref_audio and not is_cross_char:
                cand = resolve_existing_audio_path(ref_audio)
                if cand is not None and not CharacterManager._is_cross_character_audio(str(cand), pkg):
                    own_ref = cand

            if own_ref is not None:
                ref_audio = str(own_ref.resolve())
            elif pkg.manifest and pkg.manifest.emotions:
                emo = pkg.manifest.emotions.get("gentle") or next(iter(pkg.manifest.emotions.values()), None)
                if emo:
                    found_path = pkg.resolve_audio_path(emo.audio)
                    if found_path and found_path.is_file():
                        ref_audio = str(found_path.resolve())
                        if not prompt_text or is_cross_char:
                            prompt_text = emo.text
                            prompt_lang = emo.lang
        elif not is_conflicting:
            resolved_ref = resolve_existing_audio_path(ref_audio)
            if resolved_ref is not None:
                ref_audio = str(resolved_ref.resolve())

        display_name = pkg.name if (pkg and pkg.is_valid) else name
        emotion_target = pkg.id if (pkg and pkg.is_valid) else ""

        # Pre-resolve known emotion references for this character
        emotions_map: dict[str, dict[str, str]] = {}
        if emotion_target:
            for emo_name in DEFAULT_EMOTION_KEYS:
                ref = resolve_emotion_reference(emotion_target, emo_name)
                if ref:
                    cand_audio = ref["ref_audio_path"]
                    cand_dur = probe_audio_duration_seconds(cand_audio)
                    if cand_dur is not None and REFERENCE_AUDIO_MIN_SECONDS <= cand_dur <= REFERENCE_AUDIO_MAX_SECONDS:
                        emotions_map[emo_name] = {
                            "ref_audio_path": cand_audio,
                            "prompt_text": ref["prompt_text"],
                            "prompt_lang": ref["prompt_lang"],
                        }

        return ResolvedVoiceContext(
            profile_id=pid,
            name=display_name,
            gpt_weights_path=abs_gpt,
            sovits_weights_path=abs_sovits,
            ref_audio_path=ref_audio,
            prompt_text=prompt_text,
            prompt_lang=prompt_lang,
            text_lang=text_lang,
            emotions=emotions_map,
        )


_GLOBAL_VOICE_RESOLVER: VoiceProfileResolver | None = None


def get_voice_resolver() -> VoiceProfileResolver:
    """Returns application singleton VoiceProfileResolver."""
    global _GLOBAL_VOICE_RESOLVER
    if _GLOBAL_VOICE_RESOLVER is None:
        _GLOBAL_VOICE_RESOLVER = VoiceProfileResolver()
    return _GLOBAL_VOICE_RESOLVER
