"""
Voice Profile and TTS Synthesis Router for galgame2voice.
Provides REST endpoints for listing, creating, updating, deleting voice profiles,
switching active character models with auto-rollback, and synthesizing audio.
"""

import asyncio
import logging
import os
import sys
from typing import Any, Dict, List, Optional, Union
from fastapi import APIRouter, HTTPException, Query, status, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from galgame2voice.database import crud
from galgame2voice.database.session import get_db
from galgame2voice.database.models import (
    VoiceProfileCreate,
    VoiceProfileUpdate,
    VoiceProfileResponse,
)
from galgame2voice.services.gpt_sovits_client import (
    clean_japanese_parentheses,
    resolve_tts_options,
    TTS_PRESETS,
    SLICING_METHODS,
)
from galgame2voice.services.voice_manager import get_voice_manager

logger = logging.getLogger("galgame2voice.routers.voice")
router = APIRouter(prefix="/api/voice", tags=["Voice Profiles & TTS"])


# ============================================================================
# Request & Response DTOs
# ============================================================================

class VoiceProfileCreateRequest(BaseModel):
    name: str = Field(..., min_length=1)
    description: Optional[str] = ""
    gpt_weights_path: str = Field(..., min_length=1)
    sovits_weights_path: str = Field(..., min_length=1)
    refer_audio_path: Optional[str] = None
    ref_audio_path: Optional[str] = None
    refer_text: Optional[str] = None
    prompt_text: Optional[str] = None
    refer_language: Optional[str] = "ja"
    prompt_lang: Optional[str] = "ja"
    text_lang: Optional[str] = "ja"
    system_prompt: Optional[str] = ""
    is_default: bool = False


class VoiceSwitchRequest(BaseModel):
    profile_id: Optional[int] = None
    profile_name: Optional[str] = None
    id: Optional[int] = None
    name: Optional[str] = None


class SynthesizeRequest(BaseModel):
    text: str = Field(..., min_length=1)
    options: Optional[Dict[str, Any]] = None
    speed: Optional[float] = None
    top_k: Optional[int] = None
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    text_language: Optional[str] = None
    cut_option: Optional[str] = None
    preset: Optional[str] = None
    stream: bool = False


# ============================================================================
# 1. Voice Profile CRUD Endpoints
# ============================================================================

@router.get(
    "/profiles",
    summary="List Voice Profiles",
    description="Returns all character voice profiles and active profile ID.",
)
async def list_voice_profiles():
    async with get_db() as conn:
        profiles = await crud.list_voice_profiles(conn)
        active = await crud.get_active_voice_profile(conn)
        return {
            "profiles": [p.model_dump() for p in profiles],
            "active_profile_id": active.id if active else 1,
        }


@router.post(
    "/profiles",
    status_code=status.HTTP_201_CREATED,
    summary="Create Voice Profile",
    description="Creates a new character voice profile with GPT/SoVITS weights and reference audio.",
)
async def create_voice_profile(req: VoiceProfileCreateRequest):
    ref_audio = req.refer_audio_path or req.ref_audio_path or ""
    prompt_txt = req.refer_text or req.prompt_text or ""
    prompt_l = req.refer_language or req.prompt_lang or "ja"
    text_l = req.text_lang or "ja"

    if not req.name.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Profile name cannot be empty",
        )

    profile_dto = VoiceProfileCreate(
        name=req.name.strip(),
        description=req.description or "",
        gpt_weights_path=req.gpt_weights_path.strip(),
        sovits_weights_path=req.sovits_weights_path.strip(),
        ref_audio_path=ref_audio.strip(),
        prompt_text=prompt_txt.strip(),
        prompt_lang=prompt_l.strip(),
        text_lang=text_l.strip(),
        system_prompt=req.system_prompt or "",
        is_default=req.is_default,
    )

    async with get_db() as conn:
        try:
            created = await crud.create_voice_profile(conn, profile_dto)
            return {
                "id": created.id,
                "name": created.name,
                "status": "created",
                "profile": created.model_dump(),
            }
        except Exception as exc:
            logger.error("Failed to create voice profile '%s': %s", req.name, exc)
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@router.get(
    "/profiles/{profile_id}",
    summary="Get Voice Profile by ID",
    description="Returns detailed parameters of a single voice profile.",
)
async def get_voice_profile(profile_id: int):
    async with get_db() as conn:
        profile = await crud.get_voice_profile(conn, profile_id)
        if not profile:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Voice profile ID {profile_id} not found",
            )
        return {"profile": profile.model_dump()}


@router.put(
    "/profiles/{profile_id}",
    summary="Update Voice Profile",
    description="Updates existing voice profile weights and prompt parameters.",
)
async def update_voice_profile(profile_id: int, req: VoiceProfileUpdate):
    async with get_db() as conn:
        updated = await crud.update_voice_profile(conn, profile_id, req)
        if not updated:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Voice profile ID {profile_id} not found",
            )
        return {"status": "updated", "profile": updated.model_dump()}


@router.delete(
    "/profiles/{profile_id}",
    summary="Delete Voice Profile",
    description="Deletes a voice profile by ID.",
)
async def delete_voice_profile(profile_id: int):
    async with get_db() as conn:
        success = await crud.delete_voice_profile(conn, profile_id)
        if not success:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Voice profile ID {profile_id} not found",
            )
        return {"status": "deleted", "profile_id": profile_id}


# ============================================================================
# 2. Atomic Voice Model Switching
# ============================================================================

@router.post(
    "/switch",
    summary="Switch Active Voice Profile",
    description="Atomically switches GPT-SoVITS weights to selected profile with automatic rollback.",
)
async def switch_voice(req: VoiceSwitchRequest):
    profile_id = req.profile_id if req.profile_id is not None else req.id
    profile_name = req.profile_name or req.name

    if profile_id is None and not profile_name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing profile_id or profile_name in switch request",
        )

    async with get_db() as conn:
        profile = None
        if profile_id is not None:
            profile = await crud.get_voice_profile(conn, profile_id)
        elif profile_name:
            profiles = await crud.list_voice_profiles(conn)
            for p in profiles:
                if p.name == profile_name:
                    profile = p
                    break

        if not profile:
            identifier = profile_id if profile_id is not None else profile_name
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Voice profile '{identifier}' not found",
            )

    manager = get_voice_manager()
    success = await manager.switch_profile(profile, persist=True)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to load GPT/SoVITS model weights onto backend service",
        )

    return {
        "status": "switched",
        "profile": profile.name,
        "profile_id": profile.id,
    }


# ============================================================================
# 3. Speech Synthesis Endpoints
# ============================================================================

@router.post(
    "/synthesize",
    summary="Synthesize Text to Speech",
    description="Synthesizes text into WAV audio using active voice profile and specified parameters.",
)
async def synthesize_speech(req: SynthesizeRequest):
    cleaned_text = clean_japanese_parentheses(req.text)
    if not cleaned_text:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Text is empty after cleaning stage directions",
        )

    # Collect and normalize options
    options: Dict[str, Any] = dict(req.options or {})
    if req.speed is not None:
        options["speed"] = req.speed
    if req.top_k is not None:
        options["top_k"] = req.top_k
    if req.temperature is not None:
        options["temperature"] = req.temperature
    if req.top_p is not None:
        options["top_p"] = req.top_p
    if req.text_language is not None:
        options["text_language"] = req.text_language
    if req.cut_option is not None:
        options["cut_option"] = req.cut_option
    if req.preset is not None:
        options["preset"] = req.preset

    manager = get_voice_manager()

    try:
        if req.stream:
            return StreamingResponse(
                manager.stream_tts(cleaned_text, options=options),
                media_type="audio/wav",
            )
        else:
            audio_bytes = await manager.synthesize(cleaned_text, options=options)
            return Response(content=audio_bytes, media_type="audio/wav")

    except ValueError as val_err:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(val_err))
    except Exception as exc:
        logger.error("Synthesis error: %s", exc, exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))


class BrowseFileRequest(BaseModel):
    file_type: str = Field(default="all", description="'gpt', 'sovits', 'audio', or 'all'")
    initial_dir: Optional[str] = None


@router.post(
    "/browse-file",
    summary="Open Native Windows File Browser",
    description="Opens native OS file dialog to let the user select a file (.ckpt, .pth, audio).",
)
async def open_native_file_dialog(req: BrowseFileRequest):
    def _run_picker():
        try:
            import tkinter as tk
            from tkinter import filedialog

            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)

            title = "选择模型文件"
            filetypes = [("所有文件 (*.*)", "*.*")]
            if req.file_type == "gpt":
                title = "选择 GPT 权重文件 (.ckpt)"
                filetypes = [("GPT 权重 (*.ckpt)", "*.ckpt"), ("所有文件 (*.*)", "*.*")]
            elif req.file_type == "sovits":
                title = "选择 SoVITS 权重文件 (.pth)"
                filetypes = [("SoVITS 权重 (*.pth)", "*.pth"), ("所有文件 (*.*)", "*.*")]
            elif req.file_type == "audio":
                title = "选择参考音频文件 (.wav, .ogg, .mp3, .flac)"
                filetypes = [("音频文件 (*.wav;*.ogg;*.mp3;*.flac)", "*.wav;*.ogg;*.mp3;*.flac"), ("所有文件 (*.*)", "*.*")]

            init_dir = req.initial_dir if req.initial_dir and os.path.exists(req.initial_dir) else None
            selected = filedialog.askopenfilename(title=title, filetypes=filetypes, initialdir=init_dir)
            root.destroy()
            return selected or ""
        except Exception as err:
            logger.warning("Native file dialog failed or unavailable: %s", err)
            return ""

    path = await asyncio.to_thread(_run_picker)
    return {"selected_path": path}


@router.get(
    "/fs-browse",
    summary="Web Directory Browser",
    description="Lists drives, directories, and files with filtering for in-browser selection.",
)
async def fs_browse(
    path: Optional[str] = Query(None, description="Directory path to explore"),
    file_type: Optional[str] = Query("all", description="'gpt', 'sovits', 'audio', or 'all'"),
):
    import string

    # 1. Available drives (Windows)
    drives = []
    if sys.platform == "win32":
        for letter in string.ascii_uppercase:
            drive_path = f"{letter}:\\"
            if os.path.exists(drive_path):
                drives.append(drive_path)
    else:
        drives = ["/"]

    current_path = os.path.abspath(path) if path and os.path.exists(path) else (drives[0] if drives else "/")
    if os.path.isfile(current_path):
        current_path = os.path.dirname(current_path)

    parent_path = os.path.dirname(current_path) if current_path != os.path.dirname(current_path) else None

    # Filter extensions
    exts = None
    if file_type == "gpt":
        exts = {".ckpt"}
    elif file_type == "sovits":
        exts = {".pth"}
    elif file_type == "audio":
        exts = {".wav", ".ogg", ".mp3", ".flac", ".m4a"}

    directories = []
    files = []
    try:
        with os.scandir(current_path) as it:
            for entry in it:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if not entry.name.startswith("."):
                            directories.append({
                                "name": entry.name,
                                "path": entry.path,
                            })
                    elif entry.is_file(follow_symlinks=False):
                        ext = os.path.splitext(entry.name)[1].lower()
                        if exts is None or ext in exts:
                            files.append({
                                "name": entry.name,
                                "path": entry.path,
                                "size_bytes": entry.stat().st_size,
                            })
                except (PermissionError, OSError):
                    continue
    except (PermissionError, OSError) as exc:
        return {
            "current_path": current_path,
            "parent_path": parent_path,
            "drives": drives,
            "directories": [],
            "files": [],
            "error": f"无法访问目录: {exc}",
        }

    directories.sort(key=lambda x: x["name"].lower())
    files.sort(key=lambda x: x["name"].lower())

    return {
        "current_path": current_path,
        "parent_path": parent_path,
        "drives": drives,
        "directories": directories,
        "files": files,
    }


@router.get(
    "/scan-models",
    summary="Auto-Scan Discovered Models & Audios",
    description="Scans standard GPT-SoVITS and data directories for model weights and audio samples.",
)
async def scan_discovered_models():
    # Common search roots
    candidate_roots = [
        r"E:\GPT-SoVITS-v2pro-20250604\GPT-SoVITS-v2pro-20250604",
        r"E:\GPT-SoVITS-v2pro-20250604",
        r"C:\GPT-SoVITS",
        r"E:\yuzusoft",
        os.path.abspath("."),
        os.path.abspath("data"),
        os.path.abspath("audio"),
    ]

    gpt_weights = []
    sovits_weights = []
    audio_files = []

    seen = set()

    for root_dir in candidate_roots:
        if not os.path.exists(root_dir):
            continue
        try:
            for root, dirs, filenames in os.walk(root_dir):
                # Avoid scanning deep venvs or .git
                dirs[:] = [d for d in dirs if d not in (".git", "node_modules", ".venv", "__pycache__", "runtime")]
                for fname in filenames:
                    ext = os.path.splitext(fname)[1].lower()
                    full_path = os.path.join(root, fname)
                    if full_path in seen:
                        continue
                    seen.add(full_path)

                    if ext == ".ckpt":
                        gpt_weights.append({"name": fname, "path": full_path})
                    elif ext == ".pth" and ("siki" in fname.lower() or "sovits" in root.lower() or "weight" in root.lower()):
                        sovits_weights.append({"name": fname, "path": full_path})
                    elif ext in (".wav", ".ogg", ".mp3", ".flac"):
                        audio_files.append({"name": fname, "path": full_path})
        except Exception:
            continue

    return {
        "gpt_weights": gpt_weights[:50],
        "sovits_weights": sovits_weights[:50],
        "audio_files": audio_files[:50],
    }


@router.get(
    "/presets",
    summary="List Voice Presets & Slicing Methods",
    description="Returns built-in inference presets (High Quality, Balanced, Low Latency) and cut options.",
)
async def get_presets_and_slicing():
    return {
        "presets": TTS_PRESETS,
        "slicing_methods": SLICING_METHODS,
    }
