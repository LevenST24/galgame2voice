"""
Voice Profile Resolver and In-Memory Resolved Context for galgame2voice.

Provides zero-IO, in-memory caching of fully resolved voice profiles and emotion
reference audios. Pre-validates paths, durations, and weights to eliminate
database and filesystem latency from the TTS critical path (TTFA < 1s).
Supports clean cache invalidation upon profile creation, update, or deletion.
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

from galgame2voice.utils.path_guard import resolve_weight_file_path, resolve_existing_audio_path
from galgame2voice.utils.audio_spec import (
    REFERENCE_AUDIO_MAX_SECONDS,
    REFERENCE_AUDIO_MIN_SECONDS,
)
from galgame2voice.services.gpt_sovits_client import probe_audio_duration_seconds
from galgame2voice.services.emotion_references import resolve_emotion_reference

logger = logging.getLogger("galgame2voice.services.voice_resolver")

DEFAULT_EMOTION_KEYS: Tuple[str, ...] = (
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
    """Pre-resolved, immutable-in-memory representation of an active voice profile."""
    profile_id: Optional[int]
    name: str
    gpt_weights_path: str
    sovits_weights_path: str
    ref_audio_path: str
    prompt_text: str
    prompt_lang: str
    text_lang: str
    emotions: Dict[str, Dict[str, str]] = field(default_factory=dict)
    resolved_at: float = field(default_factory=time.monotonic)

    def get_emotion_ref(self, emotion: str) -> Optional[Dict[str, str]]:
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


class VoiceProfileResolver:
    """
    Maintains in-memory memoization of resolved voice profiles.
    Hot TTS paths query this resolver to avoid repeated SQLite transactions
    and filesystem duration probes.
    """

    def __init__(self, ttl_seconds: float = 300.0) -> None:
        self._ttl = ttl_seconds
        # Keys: "id:<int>", "name:<str>", "active"
        self._cache: Dict[str, Tuple[float, ResolvedVoiceContext]] = {}

    def invalidate(self, profile_id: Optional[int] = None, name: Optional[str] = None) -> None:
        """Invalidates cached contexts. If no arguments provided, clears all."""
        if profile_id is None and name is None:
            self._cache.clear()
            logger.debug("VoiceProfileResolver: Cleared all cached voice contexts")
            return

        keys_to_delete = ["active"]
        if profile_id is not None:
            keys_to_delete.append(f"id:{profile_id}")
        if name:
            clean_name = name.split("(")[0].strip()
            keys_to_delete.append(f"name:{clean_name}")

        for k in keys_to_delete:
            self._cache.pop(k, None)
        logger.debug("VoiceProfileResolver: Invalidated keys %s", keys_to_delete)

    async def resolve_context(
        self,
        db_path: Optional[str] = None,
        profile_id: Optional[int] = None,
        character_name: Optional[str] = None,
    ) -> Optional[ResolvedVoiceContext]:
        """
        Resolves a voice profile from memory cache, falling back to SQLite and package manifests.
        """
        now = time.monotonic()
        cache_key = (
            f"id:{profile_id}"
            if profile_id is not None
            else (f"name:{character_name.split('(')[0].strip()}" if character_name else "active")
        )

        cached = self._cache.get(cache_key)
        if cached is not None:
            ts, ctx = cached
            if now - ts < self._ttl:
                return ctx

        # Cache miss: query database
        raw_profile = await self._fetch_raw_profile(db_path, profile_id, character_name)
        if not raw_profile:
            return None

        ctx = self._build_context(raw_profile)
        self._cache[cache_key] = (now, ctx)
        if cache_key == "active":
            if ctx.profile_id is not None:
                self._cache[f"id:{ctx.profile_id}"] = (now, ctx)
            if ctx.name:
                self._cache[f"name:{ctx.name}"] = (now, ctx)
        return ctx

    async def _fetch_raw_profile(
        self,
        db_path: Optional[str],
        profile_id: Optional[int],
        character_name: Optional[str],
    ) -> Optional[Any]:
        from galgame2voice.database import crud
        from galgame2voice.database.session import get_db

        raw = None
        # Tiered profile resolution: each tier catches exceptions so failures do not block subsequent fallbacks
        if profile_id is not None:
            try:
                async with get_db(db_path) as conn:
                    raw = await crud.get_voice_profile(conn, int(profile_id))
            except Exception as exc:
                logger.debug("Resolver could not fetch profile_id %s: %s", profile_id, exc)

        if not raw and character_name:
            try:
                clean_name = character_name.split("(")[0].strip()
                async with get_db(db_path) as conn:
                    raw = await crud.get_voice_profile_by_name(conn, clean_name)
            except Exception as exc:
                logger.debug("Resolver could not fetch character_name %s: %s", character_name, exc)

        if not raw:
            try:
                async with get_db(db_path) as conn:
                    raw = await crud.get_active_voice_profile(conn)
            except Exception as exc:
                logger.debug("Resolver could not fetch active profile: %s", exc)

        if not raw:
            try:
                from galgame2voice.services.voice_manager import get_voice_manager
                vm = get_voice_manager()
                raw = vm.active_profile or await vm.get_active_profile()
            except Exception as exc:
                logger.debug("Resolver fallback to voice_manager active profile failed: %s", exc)

        return raw

    def _build_context(self, raw: Any) -> ResolvedVoiceContext:
        """Constructs a validated ResolvedVoiceContext from a raw DB model or dict."""
        pid = _extract_field(raw, "id", None)
        name = _extract_field(raw, "name", "Default") or "Default"
        clean_name = name.split("(")[0].strip()

        gpt_path = _extract_field(raw, "gpt_weights_path", "") or ""
        sovits_path = _extract_field(raw, "sovits_weights_path", "") or ""
        ref_audio = _extract_field(raw, "ref_audio_path", "") or ""
        prompt_text = _extract_field(raw, "prompt_text", "") or ""
        prompt_lang = _extract_field(raw, "prompt_lang", "ja") or "ja"
        text_lang = _extract_field(raw, "text_lang", "ja") or "ja"

        # Absolutize weight paths
        abs_gpt = resolve_weight_file_path(gpt_path)
        abs_sovits = resolve_weight_file_path(sovits_path)

        # Fallback to character package manifest if audio path is missing or relative
        resolved_ref = resolve_existing_audio_path(ref_audio)
        if not resolved_ref and clean_name:
            try:
                from galgame2voice.services.character_manager import get_character_manager
                mgr = get_character_manager()
                pkg = mgr.get_character(clean_name)
                if pkg and pkg.manifest and pkg.manifest.emotions:
                    emo = pkg.manifest.emotions.get("gentle") or next(iter(pkg.manifest.emotions.values()), None)
                    if emo:
                        found_path = pkg.resolve_audio_path(emo.audio)
                        if found_path and found_path.is_file():
                            ref_audio = str(found_path.resolve())
                            if not prompt_text:
                                prompt_text = emo.text
                                prompt_lang = emo.lang
            except Exception as exc:
                logger.debug("Failed resolving audio path from character package '%s': %s", clean_name, exc)
        elif resolved_ref:
            ref_audio = resolved_ref

        # Pre-resolve known emotion references for this character
        emotions_map: Dict[str, Dict[str, str]] = {}
        if clean_name:
            for emo_name in DEFAULT_EMOTION_KEYS:
                ref = resolve_emotion_reference(clean_name, emo_name)
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
            name=clean_name,
            gpt_weights_path=abs_gpt,
            sovits_weights_path=abs_sovits,
            ref_audio_path=ref_audio,
            prompt_text=prompt_text,
            prompt_lang=prompt_lang,
            text_lang=text_lang,
            emotions=emotions_map,
        )


_GLOBAL_VOICE_RESOLVER: Optional[VoiceProfileResolver] = None


def get_voice_resolver() -> VoiceProfileResolver:
    """Returns application singleton VoiceProfileResolver."""
    global _GLOBAL_VOICE_RESOLVER
    if _GLOBAL_VOICE_RESOLVER is None:
        _GLOBAL_VOICE_RESOLVER = VoiceProfileResolver()
    return _GLOBAL_VOICE_RESOLVER
