"""
Audio conversion utilities wrapping ffmpeg asynchronously for galgame2voice.
Converts OGG (Opus) to WAV (16kHz mono 16-bit PCM) for STT, and WAV to OGG for Telegram voice notes.
Includes persistent executable discovery, caching, process cleanup, and format short-circuiting.
"""

import asyncio
import io
import logging
import os
import shutil
import sys
import tempfile
import wave
from pathlib import Path
from typing import Optional, Union

logger = logging.getLogger("galgame2voice.utils.audio_converter")

_cached_ffmpeg_bin: Optional[str] = None


def reset_ffmpeg_cache() -> None:
    """Clears the cached ffmpeg executable path (useful for testing)."""
    global _cached_ffmpeg_bin
    _cached_ffmpeg_bin = None


def find_ffmpeg(custom_path: Optional[str] = None) -> Optional[str]:
    """
    Discovers and caches the ffmpeg executable location.
    Checks:
    1. custom_path (if provided and resolvable)
    2. Environment variable FFMPEG_PATH or FFMPEG_BIN
    3. Cached path from previous discovery
    4. System PATH via shutil.which("ffmpeg")
    5. Local virtualenv (sys.prefix/Scripts/ffmpeg.exe or bin/ffmpeg)
    6. Bundled / project tools directories (tools/ffmpeg, runtime/ffmpeg, etc.)
    """
    if custom_path:
        # If an explicit path was passed, check it directly without caching as global default
        resolved = shutil.which(custom_path)
        if resolved:
            return resolved
        p = Path(custom_path)
        if p.is_file():
            return str(p.resolve())
        return None

    global _cached_ffmpeg_bin
    if _cached_ffmpeg_bin is not None:
        return _cached_ffmpeg_bin

    # 1. Environment variables
    for env_var in ("FFMPEG_PATH", "FFMPEG_BIN"):
        env_val = os.environ.get(env_var)
        if env_val:
            resolved = shutil.which(env_val) or (str(Path(env_val).resolve()) if Path(env_val).is_file() else None)
            if resolved:
                _cached_ffmpeg_bin = resolved
                return _cached_ffmpeg_bin

    # 2. System PATH
    system_ffmpeg = shutil.which("ffmpeg") or (shutil.which("ffmpeg.exe") if sys.platform == "win32" else None)
    if system_ffmpeg:
        _cached_ffmpeg_bin = system_ffmpeg
        return _cached_ffmpeg_bin

    # 3. Virtualenv scripts
    is_win = sys.platform == "win32"
    exe_name = "ffmpeg.exe" if is_win else "ffmpeg"
    scripts_dir = "Scripts" if is_win else "bin"
    venv_candidate = Path(sys.prefix) / scripts_dir / exe_name
    if venv_candidate.is_file():
        _cached_ffmpeg_bin = str(venv_candidate.resolve())
        return _cached_ffmpeg_bin

    base_candidate = Path(sys.base_prefix) / scripts_dir / exe_name
    if base_candidate.is_file():
        _cached_ffmpeg_bin = str(base_candidate.resolve())
        return _cached_ffmpeg_bin

    # 4. Project root & bundled tools
    try:
        from galgame2voice.config import get_settings
        root = get_settings().project_root
    except Exception:
        root = Path(__file__).resolve().parent.parent.parent

    for candidate_dir in ("tools", "runtime", "bin", "ffmpeg"):
        bundled = root / candidate_dir / exe_name
        if bundled.is_file():
            _cached_ffmpeg_bin = str(bundled.resolve())
            return _cached_ffmpeg_bin

    return None


def is_ffmpeg_available(ffmpeg_path: Optional[str] = None) -> bool:
    """Checks if ffmpeg executable is installed and available."""
    return find_ffmpeg(ffmpeg_path) is not None


def _is_known_non_audio(data: bytes) -> bool:
    """Checks if payload starts with distinct non-audio file magic headers."""
    if not data:
        return True
    return (
        data.startswith(b"\x89PNG")
        or data.startswith(b"<!DOCTYPE")
        or data.startswith(b"<html")
        or data.startswith(b"{\n")
        or data.startswith(b'{"')
        or data.startswith(b"\x7fELF")
        or data.startswith(b"PK\x03\x04")
        or data.startswith(b"%PDF")
    )


def is_target_wav_pcm(
    data: bytes,
    sample_rate: int = 16000,
    channels: int = 1,
    sample_width: int = 2,
) -> bool:
    """
    Checks if audio bytes are already a valid uncompressed PCM WAV matching the target parameters.
    Target format: 16-bit mono PCM WAV at target sample rate (default 16000 Hz, 1 channel, 16-bit).
    """
    if not data or len(data) < 44 or not data.startswith(b"RIFF"):
        return False
    try:
        with wave.open(io.BytesIO(data), "rb") as wf:
            return (
                wf.getcomptype() == "NONE"
                and wf.getnchannels() == channels
                and wf.getframerate() == sample_rate
                and wf.getsampwidth() == sample_width
            )
    except Exception:
        return False


async def run_ffmpeg_command(*args: str, timeout: float = 30.0) -> None:
    """
    Runs ffmpeg command asynchronously with bounded timeout and process cleanup.
    Raises RuntimeError on nonzero exit, or TimeoutError if execution exceeds timeout.
    """
    cmd_args = list(args)
    if cmd_args and cmd_args[0] == "ffmpeg":
        discovered = find_ffmpeg()
        if discovered:
            cmd_args[0] = discovered

    if "-nostdin" not in cmd_args and len(cmd_args) > 1:
        cmd_args.insert(1, "-nostdin")

    proc = await asyncio.create_subprocess_exec(
        *cmd_args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except (asyncio.TimeoutError, TimeoutError):
        logger.error("ffmpeg conversion timed out after %.1f seconds: %s", timeout, cmd_args[:4])
        try:
            proc.kill()
        except (ProcessLookupError, OSError):
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=3.0)
        except Exception:
            pass
        raise TimeoutError(f"ffmpeg conversion timed out after {timeout} seconds")
    except asyncio.CancelledError:
        try:
            proc.kill()
        except (ProcessLookupError, OSError):
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=3.0)
        except Exception:
            pass
        raise

    if proc.returncode != 0:
        err_msg = stderr.decode("utf-8", errors="replace")
        logger.error("ffmpeg failed (code %d): %s", proc.returncode, err_msg)
        raise RuntimeError(f"ffmpeg conversion failed (code {proc.returncode}): {err_msg[:200]}")


async def convert_ogg_to_wav(
    ogg_bytes: bytes,
    sample_rate: int = 16000,
    channels: int = 1,
    ffmpeg_path: Optional[str] = None,
    timeout: float = 30.0,
) -> bytes:
    """
    Converts OGG/Opus audio bytes from Telegram to 16kHz mono 16-bit PCM WAV bytes for STT.
    Raises ValueError if input bytes are invalid or corrupt.
    """
    if not ogg_bytes or len(ogg_bytes) < 12:
        raise ValueError("Audio payload is empty or too short")

    if _is_known_non_audio(ogg_bytes):
        raise ValueError("Corrupted or unsupported audio format")

    if not (
        ogg_bytes.startswith(b"OggS") or ogg_bytes.startswith(b"RIFF") or len(ogg_bytes) > 44
    ):
        raise ValueError("Corrupted or unsupported audio format")

    # Short-circuit transcoding when input is already in target format (16-bit mono PCM WAV at target sample rate)
    if is_target_wav_pcm(ogg_bytes, sample_rate=sample_rate, channels=channels, sample_width=2):
        return ogg_bytes

    ffmpeg_bin = find_ffmpeg(ffmpeg_path)
    if not ffmpeg_bin:
        raise RuntimeError(
            f"ffmpeg executable not found: '{ffmpeg_path or 'ffmpeg'}'. "
            "Install ffmpeg and ensure it is on PATH, or provide ffmpeg_path."
        )

    in_path: Optional[Path] = None
    out_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as in_file:
            in_path = Path(in_file.name)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as out_file:
            out_path = Path(out_file.name)

        in_path.write_bytes(ogg_bytes)
        cmd = [
            ffmpeg_bin, "-y",
            "-i", str(in_path),
            "-ar", str(sample_rate),
            "-ac", str(channels),
            "-f", "wav",
            str(out_path),
        ]
        await run_ffmpeg_command(*cmd, timeout=timeout)
        wav_bytes = out_path.read_bytes()
        if not wav_bytes or not wav_bytes.startswith(b"RIFF"):
            raise ValueError("ffmpeg output is not valid WAV audio")
        return wav_bytes
    except (RuntimeError, TimeoutError, ValueError) as exc:
        raise ValueError(f"Audio conversion failed: {exc}") from exc
    finally:
        for p in (in_path, out_path):
            if p is not None:
                for _ in range(10):
                    try:
                        if p.exists():
                            p.unlink(missing_ok=True)
                        break
                    except OSError:
                        try:
                            await asyncio.sleep(0.05)
                        except asyncio.CancelledError:
                            pass



async def convert_wav_to_ogg(
    wav_bytes: bytes,
    bitrate: str = "64k",
    ffmpeg_path: Optional[str] = None,
    timeout: float = 30.0,
) -> bytes:
    """
    Converts WAV audio bytes to OGG/Opus bytes for Telegram SendVoice.
    Raises ValueError if input bytes are invalid, corrupt, or empty.
    """
    if not wav_bytes:
        raise ValueError("WAV bytes cannot be empty")
    if len(wav_bytes) < 12:
        raise ValueError("Audio payload is empty or too short")

    # Fast passthrough if already Ogg Opus
    if wav_bytes.startswith(b"OggS"):
        return wav_bytes

    if _is_known_non_audio(wav_bytes):
        raise ValueError("Corrupted or unsupported audio format")

    ffmpeg_bin = find_ffmpeg(ffmpeg_path)
    if not ffmpeg_bin:
        raise RuntimeError(
            f"ffmpeg executable not found: '{ffmpeg_path or 'ffmpeg'}'. "
            "Install ffmpeg and ensure it is on PATH, or provide ffmpeg_path."
        )

    in_path: Optional[Path] = None
    out_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as in_file:
            in_path = Path(in_file.name)
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as out_file:
            out_path = Path(out_file.name)

        in_path.write_bytes(wav_bytes)
        cmd = [
            ffmpeg_bin, "-y",
            "-i", str(in_path),
            "-c:a", "libopus",
            "-b:a", str(bitrate),
            "-f", "ogg",
            str(out_path),
        ]
        await run_ffmpeg_command(*cmd, timeout=timeout)
        ogg_bytes = out_path.read_bytes()
        if not ogg_bytes or not ogg_bytes.startswith(b"OggS"):
            raise ValueError("ffmpeg output is not valid OGG audio")
        return ogg_bytes
    except (RuntimeError, TimeoutError, ValueError) as exc:
        raise ValueError(f"Audio conversion failed: {exc}") from exc
    finally:
        for p in (in_path, out_path):
            if p is not None:
                for _ in range(10):
                    try:
                        if p.exists():
                            p.unlink(missing_ok=True)
                        break
                    except OSError:
                        try:
                            await asyncio.sleep(0.05)
                        except asyncio.CancelledError:
                            pass



__all__ = [
    "find_ffmpeg",
    "reset_ffmpeg_cache",
    "is_ffmpeg_available",
    "is_target_wav_pcm",
    "run_ffmpeg_command",
    "convert_ogg_to_wav",
    "convert_wav_to_ogg",
]
