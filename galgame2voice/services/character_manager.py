"""
Self-Contained Character Package Manager & Auto-Discovery Service for Galgame2Voice.

Handles character package auto-discovery in characters/, manifest validation,
audio duration verification ([3.0s, 10.0s]), dynamic emotion resolution,
and idempotent synchronization with SQLite voice_profiles persistence.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import aiosqlite
from pydantic import BaseModel, Field, field_validator

from galgame2voice.config import get_settings

logger = logging.getLogger("galgame2voice.services.character_manager")


# ============================================================================
# 1. Pydantic Manifest Schemas
# ============================================================================

class VoiceParamsConfig(BaseModel):
    """Default voice generation parameters for a character."""
    speed: float = Field(default=1.0, ge=0.1, le=3.0, description="Default speech speed factor")
    temperature: float = Field(default=0.8, ge=0.0, le=2.0, description="Default sampling temperature")
    top_k: Optional[int] = Field(default=15, ge=1, le=100, description="Top-k sampling parameter")
    top_p: Optional[float] = Field(default=1.0, ge=0.0, le=1.0, description="Top-p sampling parameter")


class EmotionConfig(BaseModel):
    """Reference audio definition and metadata for a specific emotion."""
    audio: str = Field(..., min_length=1, description="Relative path to reference audio, e.g., 'refs/gentle.ogg'")
    text: str = Field(..., min_length=1, description="Transcript of the reference audio")
    lang: str = Field(default="ja", description="Language code of reference audio (ja, zh, en)")
    description: Optional[str] = Field(default=None, description="Human-readable description of this emotion")


class CharacterManifest(BaseModel):
    """Validated schema for character package manifest.json."""
    id: str = Field(..., min_length=1, max_length=100, description="Unique machine-readable character identifier")
    name: str = Field(..., min_length=1, max_length=100, description="Display name of the character")
    version: str = Field(default="1.0.0", description="Character package semantic version")
    description: str = Field(default="", description="Character background or lore description")
    system_prompt: Optional[str] = Field(default="", description="Personality prompt template")
    default_voice_params: VoiceParamsConfig = Field(default_factory=VoiceParamsConfig)
    gpt_weights: Optional[str] = Field(default=None, description="Path or pointer to GPT model weights")
    sovits_weights: Optional[str] = Field(default=None, description="Path or pointer to SoVITS model weights")
    emotions: Dict[str, EmotionConfig] = Field(default_factory=dict, description="Emotion to reference audio mapping")


# ============================================================================
# 2. Character Package Container
# ============================================================================

class CharacterPackage:
    """Represents a fully self-contained character package on disk."""

    def __init__(
        self,
        folder_path: Path,
        manifest: CharacterManifest,
        system_prompt: str = "",
        validation_errors: Optional[List[str]] = None,
    ):
        self.folder_path = folder_path.resolve()
        self.manifest = manifest
        self.system_prompt = system_prompt or manifest.system_prompt or ""
        self.validation_errors: List[str] = validation_errors or []
        self.is_valid: bool = len(self.validation_errors) == 0

    @property
    def id(self) -> str:
        return self.manifest.id

    @property
    def name(self) -> str:
        return self.manifest.name

    def resolve_audio_path(self, relative_or_absolute: str) -> Optional[Path]:
        """
        Resolves an audio path relative to the character package directory.
        Strictly guarded against directory traversal and out-of-boundary references.
        """
        if not relative_or_absolute:
            return None
        from galgame2voice.utils.path_guard import contains_traversal_payload, get_authorized_roots

        clean_str = str(relative_or_absolute).strip().strip("\"'")
        if contains_traversal_payload(clean_str):
            logger.warning("Directory traversal rejected in resolve_audio_path: %s", clean_str)
            return None

        p = Path(clean_str)
        settings = get_settings()
        char_dir = getattr(settings, "characters_dir", settings.project_root / "characters")
        authorized = [self.folder_path, Path(char_dir)] + get_authorized_roots()

        if p.is_absolute():
            if p.is_file():
                res = p.resolve()
                if any(res == root or res.is_relative_to(root) for root in authorized):
                    return res
            return None

        # 1. Check relative to character package folder
        norm_rel = clean_str.lstrip("/\\")
        cand = (self.folder_path / norm_rel).resolve()
        if cand.is_file() and (cand == self.folder_path or cand.is_relative_to(self.folder_path)):
            return cand

        # 2. Check relative to project root
        root_cand = (settings.project_root / norm_rel).resolve()
        if root_cand.is_file() and (root_cand == settings.project_root or root_cand.is_relative_to(settings.project_root)):
            return root_cand

        # 3. Check relative to audio_dir
        audio_cand = (settings.audio_dir / norm_rel).resolve()
        if audio_cand.is_file() and (audio_cand == settings.audio_dir or audio_cand.is_relative_to(settings.audio_dir)):
            return audio_cand

        return None

    def resolve_weight_path(self, field_name: str) -> str:
        """
        Resolves gpt_weights or sovits_weights. Supports text pointer files.
        If the file exists and is small (<4KB) and contains a path, returns that path.
        Otherwise returns the resolved absolute or relative path string.
        """
        val = getattr(self.manifest, field_name, None)
        if not val:
            return ""
        from galgame2voice.utils.path_guard import contains_traversal_payload
        if contains_traversal_payload(str(val)):
            logger.warning("Directory traversal rejected in resolve_weight_path: %s", val)
            return ""

        clean_str = str(val).strip().strip("\"'")
        p = Path(clean_str)
        target_file = (self.folder_path / clean_str.lstrip("/\\")) if not p.is_absolute() else p

        if target_file.is_file():
            # Check if it's a pointer file (e.g. text containing path to .ckpt or .pth)
            try:
                if target_file.stat().st_size < 4096:
                    content = target_file.read_text(encoding="utf-8").strip()
                    if content and (content.endswith(".ckpt") or content.endswith(".pth")):
                        if contains_traversal_payload(content):
                            return ""
                        # Pointer points to target
                        settings = get_settings()
                        ptr_path = Path(content)
                        if not ptr_path.is_absolute():
                            ptr_target = settings.project_root / ptr_path
                            if ptr_target.exists():
                                from galgame2voice.utils.path_guard import to_project_relative_path
                                return to_project_relative_path(ptr_target)
                        return content
            except Exception:
                pass
            from galgame2voice.utils.path_guard import to_project_relative_path
            return to_project_relative_path(target_file)

        # If file does not exist directly in package folder, check project root
        settings = get_settings()
        root_target = settings.project_root / clean_str.lstrip("/\\")
        if root_target.exists():
            from galgame2voice.utils.path_guard import to_project_relative_path
            return to_project_relative_path(root_target)

        return str(val)


# ============================================================================
# 3. Character Manager Service
# ============================================================================

class CharacterManager:
    """
    Singleton service that discovers character packages, validates manifests
    and audio durations, syncs with SQLite database, and resolves emotion audios.
    """

    _instance: Optional[CharacterManager] = None

    def __init__(self, characters_dir: Optional[Path] = None):
        if characters_dir is not None:
            self.characters_dir = Path(characters_dir).resolve()
        else:
            settings = get_settings()
            self.characters_dir = settings.characters_dir.resolve()

        self._packages: Dict[str, CharacterPackage] = {}
        self._name_index: Dict[str, str] = {}  # Normalized name/alias -> id

    @classmethod
    def get_instance(cls, characters_dir: Optional[Path] = None) -> CharacterManager:
        if cls._instance is None:
            cls._instance = cls(characters_dir)
        elif characters_dir is not None and cls._instance.characters_dir != Path(characters_dir).resolve():
            cls._instance = cls(characters_dir)
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        cls._instance = None

    def discover_characters(self, characters_dir: Optional[Path] = None) -> List[CharacterPackage]:
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

        discovered: List[CharacterPackage] = []

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
        return discovered

    def _load_and_validate_package(self, folder: Path, manifest_path: Path) -> CharacterPackage:
        """Loads and thoroughly validates a single character package folder."""
        errors: List[str] = []

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
            from galgame2voice.services.tts_service import TtsService
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

            for emo_name, emo_cfg in manifest.emotions.items():
                audio_rel = emo_cfg.audio
                if contains_traversal_payload(audio_rel):
                    errors.append(f"Security error: Emotion '{emo_name}' audio path contains directory traversal: '{audio_rel}'")
                    continue

                resolved_audio = pkg_container.resolve_audio_path(audio_rel)
                if not resolved_audio or not resolved_audio.is_file():
                    errors.append(f"Emotion '{emo_name}' audio file not found: '{audio_rel}'")
                    continue

                # Validate duration: must be in [3.0s, 10.0s]
                duration = TtsService.get_audio_duration(resolved_audio)
                if duration is None:
                    # Could not determine duration (unsupported or corrupted audio)
                    errors.append(f"Emotion '{emo_name}' audio '{audio_rel}' could not be decoded or probed for duration")
                elif duration < 3.0 or duration > 10.0:
                    errors.append(
                        f"Emotion '{emo_name}' audio '{audio_rel}' duration {duration:.2f}s "
                        f"is out of required [3.0s, 10.0s] range"
                    )

        return CharacterPackage(
            folder_path=folder,
            manifest=manifest,
            system_prompt=system_prompt,
            validation_errors=errors,
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
            cleaned = token.lower().replace(" ", "").replace("(", "").replace(")", "").replace("-", "")
            self._name_index[cleaned] = pid

        # Natsume specific aliases for full backward compatibility
        if "夏目" in pkg.name or "natsume" in pid.lower():
            for alias in ["四季夏目", "四季ナツメ", "natsume", "siki", "四季ナツメ (shiki natsume)", "default"]:
                self._name_index[alias.lower()] = pid

    def get_character(self, id_or_name: str) -> Optional[CharacterPackage]:
        """Looks up a character package by ID or name/alias."""
        if not id_or_name:
            return None
        self._ensure_discovered()
        cleaned = id_or_name.strip().lower()

        # Direct ID match
        if id_or_name in self._packages:
            return self._packages[id_or_name]

        # Alias index match
        if cleaned in self._name_index:
            pkg_id = self._name_index[cleaned]
            return self._packages.get(pkg_id)

        # Fuzzy substring match
        for pkg in self._packages.values():
            if cleaned in pkg.id.lower() or cleaned in pkg.name.lower():
                return pkg

        return None

    def get_available_characters(self) -> List[CharacterPackage]:
        """Returns all valid, discovered character packages."""
        self._ensure_discovered()
        seen = set()
        result = []
        for pkg in self._packages.values():
            if pkg.is_valid and pkg.id not in seen:
                seen.add(pkg.id)
                result.append(pkg)
        return result

    def get_active_character_manifest(self, active_profile_name: Optional[str] = None) -> Optional[CharacterManifest]:
        """Returns the active character manifest, or falls back to default character."""
        self._ensure_discovered()
        if active_profile_name:
            pkg = self.get_character(active_profile_name)
            if pkg and pkg.is_valid:
                return pkg.manifest

        # Fallback to default / natsume
        pkg = self.get_character("default") or self.get_character("四季夏目") or self.get_character("natsume")
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
        emotion: Optional[str],
        base_dir: Optional[Path] = None,
    ) -> Optional[Dict[str, str]]:
        """
        Resolves emotion reference audio path, prompt text, and prompt language
        for a given character from its manifest.
        Falls back to default reference if the specific emotion is missing or invalid.
        """
        self._ensure_discovered()
        pkg = self.get_character(character_id_or_name)
        if not pkg or not pkg.is_valid:
            # Fall back to default character package if name was empty, 'default', or natsume
            if not character_id_or_name or character_id_or_name.lower() in ("default", "四季夏目", "natsume", "siki"):
                pkg = self.get_character("default")
            if not pkg or not pkg.is_valid:
                return None

        manifest = pkg.manifest
        emotions = manifest.emotions
        if not emotions:
            return None

        from galgame2voice.services.emotion_references import normalize_emotion
        canonical_emo = normalize_emotion(emotion)

        # 1. Try canonical emotion or raw emotion string
        target_cfg: Optional[EmotionConfig] = emotions.get(canonical_emo)
        matched_emo = canonical_emo
        if target_cfg is None and emotion:
            target_cfg = emotions.get(str(emotion).strip().lower())
            if target_cfg:
                matched_emo = str(emotion).strip().lower()

        # Handle tsundere/angry cross-matching if one exists
        if target_cfg is None:
            if canonical_emo == "tsundere" and "angry" in emotions:
                target_cfg = emotions["angry"]
                matched_emo = "angry"
            elif canonical_emo == "angry" and "tsundere" in emotions:
                target_cfg = emotions["tsundere"]
                matched_emo = "tsundere"

        # 2. Fallback to gentle or first emotion in manifest
        if target_cfg is None:
            if "gentle" in emotions:
                target_cfg = emotions["gentle"]
                matched_emo = "gentle"
            else:
                first_key = next(iter(emotions))
                target_cfg = emotions[first_key]
                matched_emo = first_key

        resolved_audio = pkg.resolve_audio_path(target_cfg.audio)

        # 3. If resolved audio is invalid, fallback to gentle or first valid audio
        if resolved_audio is None or not resolved_audio.is_file():
            fallback_cfg = emotions.get("gentle") or next(iter(emotions.values()))
            resolved_audio = pkg.resolve_audio_path(fallback_cfg.audio)
            if resolved_audio is None or not resolved_audio.is_file():
                return None
            target_cfg = fallback_cfg
            matched_emo = "gentle"

        resolved_path = resolved_audio.resolve()
        if base_dir is not None:
            custom_file = Path(base_dir) / resolved_audio.name
            if custom_file.is_file():
                resolved_path = custom_file.resolve()

        return {
            "ref_audio_path": str(resolved_path),
            "prompt_text": target_cfg.text,
            "prompt_lang": target_cfg.lang,
            "emotion": matched_emo,
        }

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

        cur = await conn.execute("SELECT COUNT(*) FROM voice_profiles;")
        count_row = await cur.fetchone()
        existing_count = count_row[0] if count_row else 0

        synced_count = 0
        from galgame2voice.utils.path_guard import to_project_relative_path

        for pkg in valid_pkgs:
            manifest = pkg.manifest
            char_name = manifest.name

            # Query existing row by name or close match/alias
            cursor = await conn.execute(
                "SELECT * FROM voice_profiles WHERE name = ? OR name LIKE ? OR name LIKE ? LIMIT 1;",
                (char_name, f"{char_name}%", f"%{char_name}%" if len(char_name) >= 3 else char_name)
            )
            existing_row = await cursor.fetchone()
            if not existing_row and ("夏目" in char_name or "natsume" in pkg.id.lower()):
                cursor = await conn.execute(
                    "SELECT * FROM voice_profiles WHERE name LIKE '%夏目%' OR name LIKE '%ナツメ%' OR name LIKE '%natsume%' LIMIT 1;"
                )
                existing_row = await cursor.fetchone()

            # Resolve default reference audio & text from package
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

            if existing_row:
                # Existing profile: update paths idempotently without overriding user modifications
                p_id = existing_row["id"]
                current_ref = existing_row["ref_audio_path"]
                # Only heal ref_audio_path if current points to a non-existent or empty path
                ref_needs_update = not current_ref or not Path(current_ref).exists()
                update_fields = []
                params = []

                if ref_needs_update and ref_audio_str:
                    update_fields.append("ref_audio_path = ?")
                    params.append(ref_audio_str)
                    if not existing_row["prompt_text"] and prompt_text:
                        update_fields.append("prompt_text = ?")
                        params.append(prompt_text)

                if gpt_weights and not existing_row["gpt_weights_path"]:
                    update_fields.append("gpt_weights_path = ?")
                    params.append(gpt_weights)

                if sovits_weights and not existing_row["sovits_weights_path"]:
                    update_fields.append("sovits_weights_path = ?")
                    params.append(sovits_weights)

                if system_prompt and not existing_row["system_prompt"]:
                    update_fields.append("system_prompt = ?")
                    params.append(system_prompt)

                if update_fields:
                    update_fields.append("updated_at = CURRENT_TIMESTAMP")
                    params.append(p_id)
                    query = f"UPDATE voice_profiles SET {', '.join(update_fields)} WHERE id = ?;"
                    await conn.execute(query, tuple(params))
                    synced_count += 1
            else:
                # Insert new voice profile
                is_default_val = 1 if existing_count == 0 else 0
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
                        gpt_weights,
                        sovits_weights,
                        ref_audio_str,
                        prompt_text,
                        prompt_lang,
                        prompt_lang,
                        system_prompt,
                        is_default_val,
                    )
                )
                existing_count += 1
                synced_count += 1

        await conn.commit()
        return synced_count


def get_character_manager(characters_dir: Optional[Path] = None) -> CharacterManager:
    """Returns singleton instance of CharacterManager."""
    return CharacterManager.get_instance(characters_dir)
