"""
Voice Profile and TTS Synthesis Router for galgame2voice.
Provides REST endpoints for listing, creating, updating, deleting voice profiles,
switching active character models with auto-rollback, and synthesizing audio.
"""

import asyncio
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Union
from fastapi import APIRouter, HTTPException, Query, status, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from galgame2voice.config import get_settings
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
    validate_user_tts_options,
    TTS_PRESETS,
    SLICING_METHODS,
)
from galgame2voice.services.voice_manager import get_voice_manager
from galgame2voice.utils.logger import sanitize_error_detail
from galgame2voice.utils.path_guard import (
    PathTraversalError,
    contains_traversal_payload,
    is_windows_device_name,
    safe_resolve_audio_path,
    validate_voice_profile_paths,
)

logger = logging.getLogger("galgame2voice.routers.voice")
router = APIRouter(prefix="/api/voice", tags=["Voice Profiles & TTS"])


# ============================================================================
# Request & Response DTOs
# ============================================================================

class VoiceProfileCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: Optional[str] = Field(default="", max_length=1000)
    gpt_weights_path: str = Field(..., min_length=1, max_length=1000)
    sovits_weights_path: str = Field(..., min_length=1, max_length=1000)
    refer_audio_path: Optional[str] = Field(default=None, max_length=1000)
    ref_audio_path: Optional[str] = Field(default=None, max_length=1000)
    refer_text: Optional[str] = Field(default=None, max_length=1000)
    prompt_text: Optional[str] = Field(default=None, max_length=1000)
    refer_language: Optional[str] = Field(default="ja", max_length=32)
    prompt_lang: Optional[str] = Field(default="ja", max_length=32)
    text_lang: Optional[str] = Field(default="ja", max_length=32)
    system_prompt: Optional[str] = Field(default="", max_length=10000)
    is_default: bool = False


class VoiceSwitchRequest(BaseModel):
    profile_id: Optional[int] = Field(default=None, ge=1)
    profile_name: Optional[str] = Field(default=None, max_length=100)
    id: Optional[int] = Field(default=None, ge=1)
    name: Optional[str] = Field(default=None, max_length=100)
    force: bool = False


class SynthesizeRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=2000)
    voice_profile_id: Optional[int] = Field(default=None, ge=1)
    options: Optional[Dict[str, Any]] = None
    speed: Optional[float] = Field(default=None, ge=0.1, le=3.0)
    top_k: Optional[int] = Field(default=None, ge=1, le=100)
    temperature: Optional[float] = Field(default=None, ge=0.0, le=2.0)
    top_p: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    text_language: Optional[str] = Field(default=None, max_length=32)
    cut_option: Optional[str] = Field(default=None, max_length=64)
    preset: Optional[str] = Field(default=None, max_length=64)
    stream: bool = False
    ai_adaptive_voice: Optional[bool] = Field(default=None, description="Whether AI-driven dynamic voice inference is enabled")


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
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Profile name cannot be empty",
        )

    try:
        validate_voice_profile_paths(
            gpt_weights_path=req.gpt_weights_path.strip(),
            sovits_weights_path=req.sovits_weights_path.strip(),
            ref_audio_path=ref_audio.strip() if ref_audio else None,
        )
    except PathTraversalError as pte:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid file path: {pte}",
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
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=sanitize_error_detail(exc))


@router.get(
    "/profiles/{profile_id}",
    summary="Get Voice Profile by ID",
    description="Returns detailed parameters of a single voice profile.",
)
async def get_voice_profile(profile_id: int):
    if profile_id < 1:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Profile ID must be a positive integer >= 1",
        )
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
    if profile_id < 1:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Profile ID must be a positive integer >= 1",
        )

    try:
        validate_voice_profile_paths(
            gpt_weights_path=req.gpt_weights_path.strip() if req.gpt_weights_path else None,
            sovits_weights_path=req.sovits_weights_path.strip() if req.sovits_weights_path else None,
            ref_audio_path=req.ref_audio_path.strip() if req.ref_audio_path else None,
        )
    except PathTraversalError as pte:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid file path: {pte}",
        )

    async with get_db() as conn:
        try:
            updated = await crud.update_voice_profile(conn, profile_id, req)
            if not updated:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Voice profile ID {profile_id} not found",
                )
            return {"status": "updated", "profile": updated.model_dump()}
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=sanitize_error_detail(exc),
            )


@router.delete(
    "/profiles/{profile_id}",
    summary="Delete Voice Profile",
    description="Deletes a voice profile by ID.",
)
async def delete_voice_profile(profile_id: int):
    if profile_id < 1:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Profile ID must be a positive integer >= 1",
        )
    async with get_db() as conn:
        try:
            success = await crud.delete_voice_profile(conn, profile_id)
            if not success:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Voice profile ID {profile_id} not found",
                )
            return {"status": "deleted", "profile_id": profile_id}
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=sanitize_error_detail(exc),
            )


# ============================================================================
# 2. Atomic Voice Model Switching & Memory Protection
# ============================================================================

def _resolve_cgroup_paths(root_path: Path) -> List[Path]:
    """
    Returns a list of candidate cgroup directory paths to inspect for the current process,
    ordered from most specific (container subpath via /proc/self/cgroup) to root_path.
    """
    candidates: List[Path] = []
    # 1. Inspect /proc/self/cgroup to detect specific container slices in K8s, Docker, systemd
    proc_cgroup = Path("/proc/self/cgroup")
    if proc_cgroup.is_file():
        try:
            for line in proc_cgroup.read_text(encoding="utf-8").splitlines():
                parts = line.strip().split(":")
                if len(parts) == 3:
                    subpath = parts[2].lstrip("/")
                    if subpath:
                        # cgroups v2 entry: 0::<path>
                        if parts[0] == "0" and parts[1] == "":
                            cand = root_path / subpath
                            if cand.is_dir() and cand not in candidates:
                                candidates.append(cand)
                        # cgroups v1 entry: <num>:memory:<path>
                        elif "memory" in parts[1].split(","):
                            # On cgroups v1, controllers are submounted under root_path/memory/
                            cand_mem = root_path / "memory" / subpath
                            if cand_mem.is_dir() and cand_mem not in candidates:
                                candidates.append(cand_mem)
                            cand_direct = root_path / subpath
                            if cand_direct.is_dir() and cand_direct not in candidates:
                                candidates.append(cand_direct)
        except Exception:
            pass

    # 2. Add root_path as fallback (for container environments with private cgroup namespaces)
    if root_path not in candidates:
        candidates.append(root_path)

    return candidates


def _get_cgroup_memory_available_gb(cgroup_root: Optional[str] = None) -> Optional[float]:
    """
    Detects container memory quota limits via Linux cgroups (v2 and v1).
    Inspects container-specific cgroup hierarchies (e.g. Kubernetes, Docker) via /proc/self/cgroup
    as well as container root cgroup mounts.
    Returns available memory in GB within the container limit, or None if no quota is configured.
    """
    try:
        from pathlib import Path
        root_path = Path(cgroup_root or os.getenv("GALGAME2VOICE_CGROUP_ROOT", "/sys/fs/cgroup"))
        if not root_path.exists():
            return None

        candidate_dirs = _resolve_cgroup_paths(root_path)

        # 1. Check cgroups v2 (memory.max & memory.current)
        for cdir in candidate_dirs:
            cg2_max = cdir / "memory.max"
            cg2_curr = cdir / "memory.current"
            if cg2_max.is_file() and cg2_curr.is_file():
                max_val = cg2_max.read_text(encoding="utf-8").strip()
                if max_val and max_val != "max":
                    limit_bytes = int(max_val)
                    curr_bytes = int(cg2_curr.read_text(encoding="utf-8").strip())
                    return max(0.0, (limit_bytes - curr_bytes) / (1024 ** 3))

        # 2. Check cgroups v1 (memory.limit_in_bytes & memory.usage_in_bytes)
        for cdir in candidate_dirs:
            cg1_candidates = [
                (cdir / "memory.limit_in_bytes", cdir / "memory.usage_in_bytes"),
                (cdir / "memory" / "memory.limit_in_bytes", cdir / "memory" / "memory.usage_in_bytes"),
            ]
            for lim_p, use_p in cg1_candidates:
                if lim_p.is_file() and use_p.is_file():
                    raw_lim = lim_p.read_text(encoding="utf-8").strip()
                    if raw_lim:
                        limit_bytes = int(raw_lim)
                        # cgroups v1 unlimited sentinel is typically >= 1 << 60 (e.g. 0x7FFFFFFFFFFFF000)
                        if limit_bytes < (1 << 60):
                            usage_bytes = int(use_p.read_text(encoding="utf-8").strip())
                            return max(0.0, (limit_bytes - usage_bytes) / (1024 ** 3))
    except Exception as exc:
        logger.debug("Failed to read cgroup memory limit: %s", exc)

    return None


def _free_memory_gb(cgroup_root: Optional[str] = None) -> Optional[float]:
    """
    Returns free physical memory in GB (cross-platform via psutil with OS-level fallbacks).
    In containerized environments (Docker, Kubernetes, cgroups v1/v2), compares host memory
    with container cgroup limits, returning min(host_available, cgroup_available).
    """
    host_avail = None
    try:
        import psutil
        host_avail = psutil.virtual_memory().available / (1024 ** 3)
    except Exception:
        pass

    # Windows fallback
    if host_avail is None and sys.platform == "win32":
        try:
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]
            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                host_avail = stat.ullAvailPhys / (1024 ** 3)
        except Exception:
            pass

    # Linux fallback (/proc/meminfo)
    if host_avail is None and sys.platform.startswith("linux"):
        try:
            with open("/proc/meminfo", "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("MemAvailable:"):
                        parts = line.split()
                        host_avail = float(parts[1]) / (1024 * 1024)
                        break
        except Exception:
            pass

    # Container / cgroups quota detection (Docker, Kubernetes, cgroups v1 & v2)
    # If cgroup limit is configured, takes min(host_available, cgroup_available)
    if sys.platform.startswith("linux") or cgroup_root or os.getenv("GALGAME2VOICE_CGROUP_ROOT") or os.path.exists("/sys/fs/cgroup"):
        cgroup_avail = _get_cgroup_memory_available_gb(cgroup_root=cgroup_root)
        if cgroup_avail is not None:
            if host_avail is not None:
                return min(host_avail, cgroup_avail)
            return cgroup_avail

    return host_avail


# 切换权重时新旧模型会短暂同时驻留内存；低于此阈值大概率触发引擎 OOM 崩溃
_DEFAULT_SWITCH_MIN_FREE_MEMORY_GB = 1.5


def _get_switch_min_free_memory_gb() -> float:
    try:
        val = os.getenv("GALGAME2VOICE_MIN_FREE_MEM_GB")
        if val is not None:
            return float(val)
    except (ValueError, TypeError):
        pass
    return _DEFAULT_SWITCH_MIN_FREE_MEMORY_GB


_SWITCH_MIN_FREE_MEMORY_GB = _DEFAULT_SWITCH_MIN_FREE_MEMORY_GB


@router.post(
    "/switch",
    summary="Switch Active Voice Profile",
    description="Atomically switches GPT-SoVITS weights to selected profile with automatic rollback.",
)
async def switch_voice(req: VoiceSwitchRequest):
    profile_id = req.profile_id if req.profile_id is not None else req.id
    raw_name = req.profile_name or req.name
    profile_name = raw_name.strip() if raw_name else None

    if profile_id is None and not profile_name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing profile_id or profile_name in switch request",
        )

    if profile_id is not None and profile_id < 1:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="profile_id must be a positive integer >= 1",
        )

    # 1. 404 Precedence: Resolve voice profile entity first from database
    async with get_db() as conn:
        profile = None
        if profile_id is not None:
            profile = await crud.get_voice_profile(conn, profile_id)
        elif profile_name:
            profile = await crud.get_voice_profile_by_name(conn, profile_name)
            if not profile:
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

    # 2. Concurrency & State Guard:
    # Acquire manager.switch_lock to serialize memory checking, model loading, and persistence.
    # This prevents concurrent requests from racing through memory checks or corrupting active profile states.
    async with manager.switch_lock:
        # Re-verify profile still exists in SQLite under the lock (TOCTOU guard against concurrent deletion)
        async with get_db() as conn:
            verified_profile = await crud.get_voice_profile(conn, profile.id)
            if not verified_profile:
                identifier = profile_id if profile_id is not None else profile_name
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Voice profile '{identifier}' not found",
                )
            profile = verified_profile

        active_prof = manager.active_profile
        if active_prof is None and not req.force:
            try:
                active_prof = await manager.get_active_profile()
                if active_prof:
                    manager.active_profile = active_prof
            except Exception:
                pass

        is_already_active = False
        if active_prof and not req.force:
            active_id = getattr(active_prof, "id", None) or (active_prof.get("id") if isinstance(active_prof, dict) else None)
            active_gpt = getattr(active_prof, "gpt_weights_path", None) or (active_prof.get("gpt_weights_path") if isinstance(active_prof, dict) else None)
            active_sovits = getattr(active_prof, "sovits_weights_path", None) or (active_prof.get("sovits_weights_path") if isinstance(active_prof, dict) else None)
            active_ref = getattr(active_prof, "ref_audio_path", None) or getattr(active_prof, "refer_audio_path", None) or (active_prof.get("ref_audio_path") if isinstance(active_prof, dict) else (active_prof.get("refer_audio_path") if isinstance(active_prof, dict) else None))
            active_prompt = getattr(active_prof, "prompt_text", None) or getattr(active_prof, "refer_text", None) or (active_prof.get("prompt_text") if isinstance(active_prof, dict) else (active_prof.get("refer_text") if isinstance(active_prof, dict) else None))

            prof_ref = getattr(profile, "ref_audio_path", None) or getattr(profile, "refer_audio_path", None)
            prof_prompt = getattr(profile, "prompt_text", None) or getattr(profile, "refer_text", None)

            if (
                active_id == profile.id
                and active_gpt == profile.gpt_weights_path
                and active_sovits == profile.sovits_weights_path
                and active_ref == prof_ref
                and active_prompt == prof_prompt
            ):
                is_already_active = True

        if is_already_active:
            # Sync SQLite persistence under the lock to ensure DB reflects active profile
            try:
                async with get_db() as conn:
                    await crud.set_active_voice_profile(conn, profile.id)
            except Exception as exc:
                logger.debug("Failed syncing active voice profile to settings: %s", exc)
        else:
            # 内存预检：实体存在且需切换时，若空闲内存不足则友好拒绝，避免 GPT-SoVITS 引擎加载权重时 OOM 崩溃
            if not req.force and not os.getenv("GALGAME2VOICE_SKIP_MEM_CHECK"):
                free_gb = _free_memory_gb()
                min_free_gb = _get_switch_min_free_memory_gb()
                if free_gb is not None and free_gb < min_free_gb:
                    raise HTTPException(
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                        detail=(
                            f"系统空闲内存不足（{free_gb:.1f} GB < {min_free_gb:.1f} GB），"
                            "加载新模型权重可能导致语音引擎崩溃。请关闭占内存的程序后重试。"
                        ),
                    )

            success = await manager.switch_profile(profile, persist=True, _already_locked=True, force=req.force)
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
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Text is empty after cleaning stage directions",
        )

    # Collect and normalize options
    options: Dict[str, Any] = dict(req.options or {})
    if req.voice_profile_id is not None:
        options["voice_profile_id"] = req.voice_profile_id
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
    if req.ai_adaptive_voice is not None:
        options["ai_adaptive_voice"] = req.ai_adaptive_voice

    try:
        validate_user_tts_options(options)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc))

    ref_audio = options.get("ref_audio_path") or options.get("refer_audio_path")
    if ref_audio:
        try:
            safe_resolve_audio_path(ref_audio)
        except PathTraversalError as pte:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid reference audio path: {pte}",
            )

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
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=sanitize_error_detail(val_err))
    except Exception as exc:
        logger.error("Synthesis error: %s", exc, exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=sanitize_error_detail(exc))


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
    result = await asyncio.to_thread(_fs_browse_sync, path, file_type)
    return result


def _fs_browse_sync(path: Optional[str], file_type: Optional[str]) -> Dict[str, Any]:
    """Blocking directory listing (runs in a worker thread)."""
    import string

    # Path traversal and device name safety check
    if path:
        if contains_traversal_payload(path) or is_windows_device_name(path):
            return {
                "current_path": path,
                "parent_path": None,
                "drives": [],
                "directories": [],
                "files": [],
                "error": "Invalid or unsafe directory path",
            }

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


# ============================================================================
# Model Scanning (thread-offloaded + TTL cached)
# ============================================================================

_scan_cache: Dict[str, Any] = {}
_SCAN_TTL_SECONDS = 60.0


def _discover_gpt_sovits_roots() -> List[str]:
    """Builds candidate GPT-SoVITS install roots from env + known layouts."""
    import glob
    roots: List[str] = []

    env_dir = os.environ.get("GPT_SOVITS_DIR")
    if env_dir:
        roots.append(env_dir)

    # Known user installation + generic drive layouts.
    roots.extend([
        r"E:\GPT-SoVITS-v2pro-20250604\GPT-SoVITS-v2pro-20250604",
        r"D:\GPT-SoVITS-v2pro-20250604\GPT-SoVITS-v2pro-20250604",
        r"C:\GPT-SoVITS-v2pro-20250604\GPT-SoVITS-v2pro-20250604",
    ])
    # Generic drive fallbacks for renamed versions.
    for drive in ("C", "D", "E", "F"):
        roots.extend(glob.glob(rf"{drive}:\GPT-SoVITS*\GPT-SoVITS*"))
        roots.extend(glob.glob(rf"{drive}:\GPT-SoVITS*"))
    return roots


def _scan_models_sync() -> Dict[str, List[Dict[str, Any]]]:
    """Blocking filesystem scan for weights & reference audio (worker thread)."""
    candidate_roots: List[str] = []
    for root in _discover_gpt_sovits_roots():
        if root not in candidate_roots and os.path.isdir(root):
            candidate_roots.append(root)

    # Also scan the app's own directories for user-copied assets.
    settings = get_settings()
    candidate_roots.extend([
        str(settings.project_root),
        str(settings.project_root / "data"),
        str(settings.project_root / "audio"),
    ])

    gpt_weights: List[Dict[str, Any]] = []
    sovits_weights: List[Dict[str, Any]] = []
    audio_files: List[Dict[str, Any]] = []
    seen = set()

    for root_dir in candidate_roots:
        try:
            for root, dirs, filenames in os.walk(root_dir):
                # Skip venvs, repos and caches — huge and irrelevant.
                dirs[:] = [d for d in dirs if d not in (".git", "node_modules", ".venv", "venv", "__pycache__", "runtime", ".pytest_cache")]
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
        except OSError:
            continue

    return {
        "gpt_weights": gpt_weights[:50],
        "sovits_weights": sovits_weights[:50],
        "audio_files": audio_files[:50],
    }


@router.get(
    "/scan-models",
    summary="Auto-Scan Discovered Models & Audios",
    description="Scans standard GPT-SoVITS and data directories for model weights and audio samples. Results cached 60s.",
)
async def scan_discovered_models():
    now = time.monotonic()
    cached = _scan_cache.get("result")
    if cached is not None and now - cached[0] < _SCAN_TTL_SECONDS:
        return cached[1]

    result = await asyncio.to_thread(_scan_models_sync)
    _scan_cache["result"] = (now, result)
    return result


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
