"""
Self-Contained Character Package Manager & Auto-Discovery Service for Galgame2Voice.

Handles character package auto-discovery in characters/, manifest validation,
audio duration verification ([3.0s, 10.0s]), dynamic emotion resolution,
and idempotent synchronization with SQLite voice_profiles persistence.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

import aiosqlite
from galgame2voice.config import get_settings
from galgame2voice.schemas.character_manifest import (
    CharacterManifest,
    EmotionConfig,
    VoiceParamsConfig,
)
from galgame2voice.utils.audio_spec import (
    REFERENCE_AUDIO_MIN_SECONDS,
    REFERENCE_AUDIO_MAX_SECONDS,
)
from galgame2voice.utils.path_guard import to_project_relative_path
from galgame2voice.services.character_package import (
    CharacterPackage,
    _AUDIO_PROBE_CACHE,
)

logger = logging.getLogger("galgame2voice.services.character_manager")


# ============================================================================
# Character Manager Service
# ============================================================================

class CharacterManager:
    """
    Singleton service that discovers character packages, validates manifests
    and audio durations, syncs with SQLite database, and resolves emotion audios.
    """

    _instance: CharacterManager | None = None

    def __init__(self, characters_dir: Path | None = None):
        if characters_dir is not None:
            self.characters_dir = Path(characters_dir).resolve()
        else:
            settings = get_settings()
            self.characters_dir = settings.characters_dir.resolve()

        self._packages: dict[str, CharacterPackage] = {}
        self._name_index: dict[str, str] = {}  # Normalized name/alias -> id

    @classmethod
    def get_instance(cls, characters_dir: Path | None = None) -> CharacterManager:
        if cls._instance is None:
            cls._instance = cls(characters_dir)
        elif characters_dir is not None and cls._instance.characters_dir != Path(characters_dir).resolve():
            cls._instance = cls(characters_dir)
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        cls._instance = None

    def discover_characters(self, characters_dir: Path | None = None) -> list[CharacterPackage]:
        """
        Discovers all character packages in the specified directory.
        Validates manifests and verifies reference audio files.
        """
        target_dir = Path(characters_dir).resolve() if characters_dir else self.characters_dir
        self.characters_dir = target_dir
        self._packages.clear()
        self._name_index.clear()

        if not target_dir.exists() or not target_dir.is_dir():
            logger.debug("Characters directory does not exist: %s", target_dir)
            return []

        discovered: list[CharacterPackage] = []

        for item in target_dir.iterdir():
            if not item.is_dir():
                continue

            manifest_path = item / "manifest.json"
            if not manifest_path.is_file():
                continue

            pkg = self._load_and_validate_package(item, manifest_path)
            self._packages[pkg.id] = pkg
            self._register_aliases(pkg)
            if pkg.is_valid:
                discovered.append(pkg)
            else:
                logger.warning(
                    "Character package '%s' at %s failed validation: %s",
                    pkg.id, item, "; ".join(pkg.validation_errors)
                )

        logger.info("Discovered %d valid character package(s) in %s", len(discovered), target_dir)
        if discovered:
            discovered_sorted = sorted(
                discovered,
                key=lambda p: (
                    0 if getattr(p.manifest, "is_default", False) else 1,
                    p.name
                )
            )
            self._name_index["default"] = discovered_sorted[0].id
        return discovered

    def _load_and_validate_package(self, folder: Path, manifest_path: Path) -> CharacterPackage:
        """Loads and thoroughly validates a single character package folder."""
        errors: list[str] = []

        try:
            raw_text = manifest_path.read_text(encoding="utf-8")
            raw_json = json.loads(raw_text)
            manifest = CharacterManifest.model_validate(raw_json)
        except Exception as exc:
            dummy_manifest = CharacterManifest(id=folder.name, name=folder.name)
            return CharacterPackage(
                folder_path=folder,
                manifest=dummy_manifest,
                validation_errors=[f"Invalid manifest.json schema: {exc}"]
            )

        # Resolve system prompt: prefer manifest.system_prompt, fallback to system_prompt.txt
        system_prompt = manifest.system_prompt or ""
        prompt_txt_path = folder / "system_prompt.txt"
        if not system_prompt and prompt_txt_path.is_file():
            try:
                system_prompt = prompt_txt_path.read_text(encoding="utf-8").strip()
            except Exception as exc:
                errors.append(f"Failed to read system_prompt.txt: {exc}")

        # Validate emotions and reference audio files
        if not manifest.emotions:
            errors.append("Manifest contains no emotions definitions")
        else:
            from galgame2voice.utils.path_guard import contains_traversal_payload

            for wf in ("gpt_weights", "sovits_weights"):
                wval = getattr(manifest, wf, None)
                if wval and contains_traversal_payload(str(wval)):
                    errors.append(f"Security error: '{wf}' contains path traversal payload: '{wval}'")

            pkg_container = CharacterPackage(
                folder_path=folder,
                manifest=manifest,
                system_prompt=system_prompt,
            )

            self._validate_package_emotions(pkg_container, manifest, errors)

        return CharacterPackage(
            folder_path=folder,
            manifest=manifest,
            system_prompt=system_prompt,
            validation_errors=errors,
        )

    @staticmethod
    def _probe_audio_file(
        resolved_audio: Path,
        canonical_path: Path,
    ) -> tuple[str | None, float | None, Exception | None]:
        """Computes or retrieves cached MD5 hash and duration for an audio file."""
        from galgame2voice.services.tts_service import TtsService
        try:
            st = resolved_audio.stat()
            cache_key = (str(canonical_path), st.st_size, st.st_mtime)
        except Exception:
            cache_key = None

        cached_probe = _AUDIO_PROBE_CACHE.get(cache_key) if cache_key else None
        if cached_probe is not None:
            return cached_probe[0], cached_probe[1], None

        try:
            file_hash = hashlib.md5(resolved_audio.read_bytes()).hexdigest()
        except Exception as exc:
            return None, None, exc

        duration = TtsService.get_audio_duration(resolved_audio)
        if cache_key:
            _AUDIO_PROBE_CACHE[cache_key] = (file_hash, duration)
        return file_hash, duration, None

    @classmethod
    def _validate_package_emotions(
        cls,
        pkg_container: CharacterPackage,
        manifest: CharacterManifest,
        errors: list[str],
    ) -> None:
        """Validates package emotion audio references, paths, hashes, and durations."""
        from galgame2voice.utils.path_guard import contains_traversal_payload

        seen_audio_paths: dict[Path, str] = {}
        seen_audio_hashes: dict[str, str] = {}

        for emo_name, emo_cfg in manifest.emotions.items():
            audio_rel = emo_cfg.audio
            if contains_traversal_payload(audio_rel):
                errors.append(f"Security error: Emotion '{emo_name}' audio path contains directory traversal: '{audio_rel}'")
                continue

            resolved_audio = pkg_container.resolve_audio_path(audio_rel)
            if not resolved_audio or not resolved_audio.is_file():
                errors.append(f"Emotion '{emo_name}' audio file not found: '{audio_rel}'")
                continue

            # Check duplicate resolved physical path
            canonical_path = resolved_audio.resolve()
            if canonical_path in seen_audio_paths:
                errors.append(
                    f"Duplicate audio path detected: emotion '{emo_name}' resolves to the same file as '{seen_audio_paths[canonical_path]}': '{audio_rel}'"
                )
            else:
                seen_audio_paths[canonical_path] = emo_name

            # Check duplicate MD5 hash across emotions in package and validate duration
            file_hash, duration, read_err = cls._probe_audio_file(resolved_audio, canonical_path)
            if read_err is not None:
                errors.append(f"Failed to read audio file '{audio_rel}' for MD5 verification: {read_err}")
                continue

            if file_hash in seen_audio_hashes:
                errors.append(
                    f"Duplicate audio MD5 detected: emotion '{emo_name}' audio '{audio_rel}' has identical MD5 hash ({file_hash[:8]}) to emotion '{seen_audio_hashes[file_hash]}'"
                )
            else:
                seen_audio_hashes[file_hash] = emo_name

            # Validate duration: must be in [3.0s, 10.0s]
            if duration is None:
                # Could not determine duration (unsupported or corrupted audio)
                errors.append(f"Emotion '{emo_name}' audio '{audio_rel}' could not be decoded or probed for duration")
            elif not (REFERENCE_AUDIO_MIN_SECONDS <= duration <= REFERENCE_AUDIO_MAX_SECONDS):
                errors.append(
                    f"Emotion '{emo_name}' audio '{audio_rel}' duration {duration:.2f}s "
                    f"is out of required [3.0s, 10.0s] range"
                )

    def _ensure_discovered(self) -> None:
        """Ensures character packages have been discovered at least once."""
        if not self._packages:
            self.discover_characters()

    def _register_aliases(self, pkg: CharacterPackage) -> None:
        """Registers alias keys for quick lookup by ID, name, or keywords."""
        pid = pkg.id
        self._name_index[pid.lower()] = pid
        self._name_index[pkg.name.lower()] = pid

        # Specific alias indexing for known character strings
        for token in [pkg.name, pkg.id]:
            cleaned = "".join(token.lower().replace("(", "").replace(")", "").replace("-", "").split())
            self._name_index[cleaned] = pid

        # Register manifest-defined aliases if present
        manifest_aliases = getattr(pkg.manifest, "aliases", None) or []
        for alias in manifest_aliases:
            if alias:
                self._name_index[alias.lower()] = pid
                self._name_index["".join(alias.lower().split())] = pid

    def get_character(self, id_or_name: str) -> CharacterPackage | None:
        """Looks up a character package by ID or name/alias."""
        if not id_or_name or not id_or_name.strip():
            return None
        self._ensure_discovered()
        cleaned = id_or_name.strip().lower()
        cleaned_spaceless = "".join(cleaned.split())

        # Direct ID match
        if id_or_name in self._packages:
            return self._packages[id_or_name]

        # Alias index match
        if cleaned in self._name_index:
            pkg_id = self._name_index[cleaned]
            return self._packages.get(pkg_id)

        if cleaned_spaceless in self._name_index:
            pkg_id = self._name_index[cleaned_spaceless]
            return self._packages.get(pkg_id)

        # Check stripping parenthetical suffixes e.g. "四季夏目 (Shiki Natsume)" -> "四季夏目"
        if "(" in cleaned:
            base_cleaned = cleaned.split("(")[0].strip()
            if base_cleaned in self._name_index:
                return self._packages.get(self._name_index[base_cleaned])
            inside_paren = cleaned.split("(", 1)[1].rstrip(")").strip()
            if inside_paren in self._name_index:
                return self._packages.get(self._name_index[inside_paren])

        # Fuzzy substring match
        for pkg in self._packages.values():
            pkg_id_lower = pkg.id.lower()
            pkg_name_lower = pkg.name.lower()
            pkg_name_spaceless = "".join(pkg_name_lower.split())
            if (
                cleaned in pkg_id_lower
                or cleaned in pkg_name_lower
                or (cleaned_spaceless and (cleaned_spaceless in pkg_id_lower or cleaned_spaceless in pkg_name_spaceless))
                or (len(pkg_id_lower) >= 2 and pkg_id_lower in cleaned)
                or (len(pkg_name_lower) >= 2 and pkg_name_lower in cleaned)
            ):
                return pkg
        return None

    def get_default_character(self) -> CharacterPackage | None:
        """Returns the default character package, or None if none installed."""
        return self.get_character("default")

    def get_available_characters(self) -> list[CharacterPackage]:
        """Returns all valid, discovered character packages."""
        self._ensure_discovered()
        seen = set()
        result = []
        for pkg in self._packages.values():
            if pkg.is_valid and pkg.id not in seen:
                seen.add(pkg.id)
                result.append(pkg)
        return result

    def get_active_character_manifest(self, active_profile_name: str | None = None) -> CharacterManifest | None:
        """Returns the active character manifest, or falls back to default character."""
        self._ensure_discovered()
        if active_profile_name:
            pkg = self.get_character(active_profile_name)
            if pkg and pkg.is_valid:
                return pkg.manifest

        # Fallback to default
        pkg = self.get_character("default")
        if pkg and pkg.is_valid:
            return pkg.manifest

        # Return first valid package if available
        available = self.get_available_characters()
        if available:
            return available[0].manifest

        return None

    def resolve_emotion_audio_path(
        self,
        character_id_or_name: str,
        emotion: str | None,
        base_dir: Path | None = None,
    ) -> dict[str, str] | None:
        """
        Resolves emotion reference audio path, prompt text, and prompt language
        for a given character from its manifest.
        Falls back to default reference if the specific emotion is missing or invalid.
        """
        self._ensure_discovered()
        pkg = self.get_character(character_id_or_name) if character_id_or_name else None
        if not pkg or not pkg.is_valid:
            # Fall back to default character package if name was empty or 'default'
            if not character_id_or_name or character_id_or_name.lower() == "default":
                pkg = self.get_character("default")
            if not pkg or not pkg.is_valid:
                return None

        manifest = pkg.manifest
        emotions = manifest.emotions
        if not emotions:
            return None

        from galgame2voice.services.emotion_references import normalize_emotion
        canonical_emo = normalize_emotion(emotion)

        target_cfg, resolved_audio, matched_emo = self._select_emotion_config(
            pkg, emotions, canonical_emo, emotion
        )
        if target_cfg is None or resolved_audio is None:
            return None

        resolved_path = resolved_audio.resolve()
        if base_dir is not None:
            custom_file = Path(base_dir) / resolved_audio.name
            if custom_file.is_file():
                resolved_path = custom_file.resolve()

        res: dict[str, Any] = {
            "ref_audio_path": str(resolved_path),
            "prompt_text": target_cfg.text,
            "prompt_lang": target_cfg.lang,
            "emotion": matched_emo,
        }
        if target_cfg.voice_params is not None:
            res["voice_params"] = target_cfg.voice_params.model_dump()
        return res

    @staticmethod
    def _select_emotion_config(
        pkg: CharacterPackage,
        emotions: dict[str, Any],
        canonical_emo: str,
        emotion: str | None,
    ) -> tuple[Any | None, Path | None, str]:
        """Resolves target emotion config and valid audio path with gentle/first fallback."""
        target_cfg = emotions.get(canonical_emo)
        matched_emo = canonical_emo
        if target_cfg is None and emotion:
            target_cfg = emotions.get(str(emotion).strip().lower())
            if target_cfg:
                matched_emo = str(emotion).strip().lower()

        if target_cfg is None:
            if "gentle" in emotions:
                target_cfg = emotions["gentle"]
                matched_emo = "gentle"
            else:
                first_key = next(iter(emotions))
                target_cfg = emotions[first_key]
                matched_emo = first_key

        resolved_audio = pkg.resolve_audio_path(target_cfg.audio)
        if resolved_audio is None or not resolved_audio.is_file():
            fallback_cfg = emotions.get("gentle") or next(iter(emotions.values()))
            resolved_audio = pkg.resolve_audio_path(fallback_cfg.audio)
            if resolved_audio is None or not resolved_audio.is_file():
                return None, None, matched_emo
            target_cfg = fallback_cfg
            matched_emo = "gentle"

        return target_cfg, resolved_audio, matched_emo

    @staticmethod
    def _weight_needs_healing(w_path: str) -> bool:
        """Determines if a model weight path is empty, machine-specific, or non-existent."""
        if not w_path:
            return True
        norm_w = w_path.replace("/", "\\")
        if norm_w.startswith(("E:", "E:\\")):
            return True
        return not Path(w_path).exists()

    @staticmethod
    def _is_cross_character_audio(current_ref: str, pkg: CharacterPackage) -> bool:
        """Determines if the referenced audio path belongs to a different character package."""
        if not current_ref:
            return False
        norm_ref = current_ref.replace("\\", "/").lower()
        is_natsume = (pkg.id.lower() in ("natsume", "shiki_natsume") or "夏目" in pkg.name)
        if not is_natsume and "natsume" in norm_ref:
            return True
        if "characters/" in current_ref.replace("\\", "/"):
            ref_char_part = current_ref.replace("\\", "/").split("characters/")[1].split("/")[0]
            if ref_char_part and ref_char_part.lower() != pkg.id.lower() and ref_char_part != pkg.name:
                return True
        return False

    async def _prune_ghost_profiles(self, conn: aiosqlite.Connection) -> int:
        """Prunes ghost voice_profile records whose package directory under characters/ no longer exists."""
        pruned_count = 0
        cur_all = await conn.execute("SELECT id, name, gpt_weights_path, ref_audio_path FROM voice_profiles;")
        all_profiles = await cur_all.fetchall()
        for prof in all_profiles:
            p_id = prof["id"]
            p_name = prof["name"]
            p_gpt = prof["gpt_weights_path"] or ""
            p_ref = prof["ref_audio_path"] or ""

            is_ghost = False
            for path_cand in (p_gpt, p_ref):
                norm_cand = path_cand.replace("\\", "/")
                if norm_cand.startswith("characters/"):
                    parts = norm_cand.split("/")
                    if len(parts) >= 2:
                        sub_folder = parts[1]
                        if not (self.characters_dir / sub_folder).exists():
                            is_ghost = True
                            break

            if not is_ghost and p_name.lower() in ("kazari",) and not (self.characters_dir / p_name).exists():
                is_ghost = True

            if is_ghost:
                logger.info("Pruning ghost voice_profile record id=%s name='%s'", p_id, p_name)
                await conn.execute("DELETE FROM voice_profiles WHERE id = ?;", (p_id,))
                pruned_count += 1
        return pruned_count

    @staticmethod
    async def _find_existing_profile_row(
        conn: aiosqlite.Connection,
        char_name: str,
        aliases: list[str] | None,
    ) -> Any | None:
        """Queries an existing voice_profile row by character name or configured aliases."""
        cursor = await conn.execute(
            "SELECT * FROM voice_profiles WHERE name = ? OR name LIKE ? OR name LIKE ? LIMIT 1;",
            (char_name, f"{char_name}%", f"%{char_name}%" if len(char_name) >= 3 else char_name),
        )
        existing_row = await cursor.fetchone()
        if not existing_row and aliases:
            for alias in aliases:
                if not alias:
                    continue
                cur_alias = await conn.execute(
                    "SELECT * FROM voice_profiles WHERE name = ? OR name LIKE ? LIMIT 1;",
                    (alias, f"%{alias}%"),
                )
                existing_row = await cur_alias.fetchone()
                if existing_row:
                    break
        return existing_row

    @staticmethod
    def _resolve_package_profile_defaults(pkg: CharacterPackage) -> dict[str, Any]:
        """Resolves default reference audio, weights, and prompt settings for a package."""
        manifest = pkg.manifest
        default_emo = manifest.emotions.get("gentle") or (
            next(iter(manifest.emotions.values())) if manifest.emotions else None
        )
        if default_emo:
            resolved_audio = pkg.resolve_audio_path(default_emo.audio)
            ref_audio_str = to_project_relative_path(resolved_audio) if resolved_audio else default_emo.audio
            prompt_text = default_emo.text
            prompt_lang = default_emo.lang
        else:
            ref_audio_str = ""
            prompt_text = ""
            prompt_lang = "ja"

        gpt_weights = to_project_relative_path(pkg.resolve_weight_path("gpt_weights"))
        sovits_weights = to_project_relative_path(pkg.resolve_weight_path("sovits_weights"))
        system_prompt = pkg.system_prompt or manifest.system_prompt or ""

        return {
            "ref_audio_str": ref_audio_str,
            "prompt_text": prompt_text,
            "prompt_lang": prompt_lang,
            "gpt_weights": gpt_weights,
            "sovits_weights": sovits_weights,
            "system_prompt": system_prompt,
        }

    @classmethod
    def _build_profile_update_fields(
        cls,
        existing_row: Any,
        pkg: CharacterPackage,
        defaults: dict[str, Any],
    ) -> tuple[list[str], list[Any]]:
        """Constructs SQL update expressions and bound parameters for an existing profile."""
        manifest = pkg.manifest
        ref_audio_str = defaults["ref_audio_str"]
        prompt_text = defaults["prompt_text"]
        prompt_lang = defaults["prompt_lang"]
        gpt_weights = defaults["gpt_weights"]
        sovits_weights = defaults["sovits_weights"]
        system_prompt = defaults["system_prompt"]

        current_ref = existing_row["ref_audio_path"] or ""
        current_gpt = existing_row["gpt_weights_path"] or ""
        current_sovits = existing_row["sovits_weights_path"] or ""
        current_prompt = existing_row["prompt_text"] or ""
        current_sys = existing_row["system_prompt"] or ""

        update_fields: list[str] = []
        params: list[Any] = []

        ref_needs_update = (
            not current_ref
            or not Path(current_ref).exists()
            or cls._is_cross_character_audio(current_ref, pkg)
        )

        if ref_needs_update and ref_audio_str:
            update_fields.append("ref_audio_path = ?")
            params.append(ref_audio_str)

        if (not current_prompt.strip() or ref_needs_update) and prompt_text:
            update_fields.append("prompt_text = ?")
            params.append(prompt_text)
            if prompt_lang:
                update_fields.append("prompt_lang = ?")
                params.append(prompt_lang)

        if gpt_weights and cls._weight_needs_healing(current_gpt):
            update_fields.append("gpt_weights_path = ?")
            params.append(gpt_weights)

        if sovits_weights and cls._weight_needs_healing(current_sovits):
            update_fields.append("sovits_weights_path = ?")
            params.append(sovits_weights)

        # 角色包是人设提示词的唯一权威源，DB 只是运行时读的镜像：
        # 不一致就覆盖，否则包里的更新永远进不了聊天链路。
        # 包内为空时保留 DB 值，避免同步把设定抹掉。
        if system_prompt and system_prompt != current_sys:
            update_fields.append("system_prompt = ?")
            params.append(system_prompt)

        char_desc = manifest.description or ""
        current_desc = existing_row["description"] or ""
        if char_desc and not current_desc.strip():
            update_fields.append("description = ?")
            params.append(char_desc)

        return update_fields, params

    @classmethod
    async def _update_existing_profile(
        cls,
        conn: aiosqlite.Connection,
        existing_row: Any,
        pkg: CharacterPackage,
        defaults: dict[str, Any],
    ) -> bool:
        """Idempotently updates existing voice_profile row with healed paths and prompt updates."""
        update_fields, params = cls._build_profile_update_fields(existing_row, pkg, defaults)
        if update_fields:
            update_fields.append("updated_at = CURRENT_TIMESTAMP")
            params.append(existing_row["id"])
            query = f"UPDATE voice_profiles SET {', '.join(update_fields)} WHERE id = ?;"
            await conn.execute(query, tuple(params))
            return True
        return False

    @staticmethod
    async def _insert_new_profile(
        conn: aiosqlite.Connection,
        char_name: str,
        manifest: Any,
        defaults: dict[str, Any],
        is_default_val: int,
    ) -> None:
        """Inserts a new voice_profile record from character package manifest and defaults."""
        await conn.execute(
            """
            INSERT INTO voice_profiles (
                name, description, gpt_weights_path, sovits_weights_path,
                ref_audio_path, prompt_text, prompt_lang, text_lang,
                system_prompt, is_default
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                char_name,
                manifest.description,
                defaults["gpt_weights"],
                defaults["sovits_weights"],
                defaults["ref_audio_str"],
                defaults["prompt_text"],
                defaults["prompt_lang"],
                defaults["prompt_lang"],
                defaults["system_prompt"],
                is_default_val,
            ),
        )

    async def sync_with_db(self, conn: aiosqlite.Connection) -> int:
        """
        Idempotently syncs/upserts discovered character packages into SQLite voice_profiles
        table without mutating or corrupting existing user configurations or settings.
        Returns the count of synced/updated profiles.
        """
        self._ensure_discovered()

        valid_pkgs = self.get_available_characters()
        if not valid_pkgs:
            return 0

        # Deterministic default order: prioritize packages marked with is_default=True, then by name
        def _pkg_sort_key(p: CharacterPackage) -> tuple[int, str]:
            is_def = getattr(p.manifest, "is_default", False)
            return (0 if is_def else 1, p.name)

        valid_pkgs = sorted(valid_pkgs, key=_pkg_sort_key)

        cur = await conn.execute("SELECT COUNT(*) FROM voice_profiles;")
        count_row = await cur.fetchone()
        existing_count = count_row[0] if count_row else 0

        # 1. Prune ghost profiles whose package directory under characters/ no longer exists
        pruned_count = await self._prune_ghost_profiles(conn)
        synced_count = pruned_count
        existing_count = max(0, existing_count - pruned_count)

        # Check if an active default already exists
        cur_def = await conn.execute("SELECT COUNT(*) FROM voice_profiles WHERE is_default = 1;")
        has_default_row = await cur_def.fetchone()
        has_default = bool(has_default_row and has_default_row[0] > 0)

        for pkg in valid_pkgs:
            manifest = pkg.manifest
            char_name = manifest.name
            existing_row = await self._find_existing_profile_row(conn, char_name, getattr(manifest, "aliases", None))
            defaults = self._resolve_package_profile_defaults(pkg)

            if existing_row:
                if await self._update_existing_profile(conn, existing_row, pkg, defaults):
                    synced_count += 1
            else:
                is_default_val = 0
                if not has_default and (getattr(manifest, "is_default", False) or existing_count == 0):
                    is_default_val = 1
                    has_default = True

                await self._insert_new_profile(conn, char_name, manifest, defaults, is_default_val)
                existing_count += 1
                synced_count += 1

        await conn.commit()
        return synced_count


def get_character_manager(characters_dir: Path | None = None) -> CharacterManager:
    """Returns singleton instance of CharacterManager."""
    return CharacterManager.get_instance(characters_dir)


__all__ = [
    "CharacterManager",
    "CharacterManifest",
    "CharacterPackage",
    "EmotionConfig",
    "VoiceParamsConfig",
    "_AUDIO_PROBE_CACHE",
    "get_character_manager",
]

