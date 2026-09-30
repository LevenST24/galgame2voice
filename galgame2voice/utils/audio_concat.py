"""
Audio concatenation utilities for multi-chunk WAV synthesis.
Provides micro-fade boundary smoothing and configurable pause duration insertion.
"""

from __future__ import annotations

import array
import logging
from pathlib import Path
from typing import List, Optional, Tuple, Union
import wave

logger = logging.getLogger(__name__)


def _collect_valid_chunks(
    chunk_paths: List[Union[str, Path]],
) -> Tuple[List[Path], Optional[wave._wave_params]]:
    """Filters chunk paths to existing readable WAV files matching the base format."""
    valid_files: List[Path] = []
    base_params = None

    for local_p in chunk_paths:
        if not local_p:
            continue
        p = Path(local_p)
        if not p.is_file():
            continue
        try:
            with wave.open(str(p), "rb") as w:
                cur_params = w.getparams()
                if base_params is None:
                    base_params = cur_params
                    valid_files.append(p)
                elif (
                    w.getnchannels() == base_params.nchannels
                    and w.getsampwidth() == base_params.sampwidth
                    and w.getframerate() == base_params.framerate
                ):
                    valid_files.append(p)
                else:
                    logger.warning(
                        "Skipping WAV chunk %s: mismatched audio parameters (channels=%d, sampwidth=%d, framerate=%d vs base channels=%d, sampwidth=%d, framerate=%d)",
                        p,
                        w.getnchannels(),
                        w.getsampwidth(),
                        w.getframerate(),
                        base_params.nchannels,
                        base_params.sampwidth,
                        base_params.framerate,
                    )
        except Exception as exc:
            logger.debug("Skipping unreadable WAV chunk %s: %s", p, exc)

    return valid_files, base_params


def _apply_micro_fade(raw_frames: bytes, base_params: wave._wave_params) -> bytes:
    """Applies micro-fade smoothing to 16-bit PCM chunk boundaries to eliminate pop/click artifacts."""
    if base_params.sampwidth != 2 or len(raw_frames) % (2 * base_params.nchannels) != 0:
        return raw_frames

    total_frames = len(raw_frames) // (base_params.sampwidth * base_params.nchannels)
    fade_frames = min(int(base_params.framerate * 0.005), total_frames // 4)
    if fade_frames <= 0:
        return raw_frames

    samples = array.array("h")
    samples.frombytes(raw_frames)
    n_ch = base_params.nchannels
    for i in range(fade_frames):
        factor = i / fade_frames
        for c in range(n_ch):
            idx_start = i * n_ch + c
            samples[idx_start] = int(samples[idx_start] * factor)
            idx_end = (total_frames - 1 - i) * n_ch + c
            samples[idx_end] = int(samples[idx_end] * factor)
    return samples.tobytes()


def _build_silence_bytes(base_params: wave._wave_params, pause_duration: float) -> bytes:
    """Calculates silence byte padding for pause duration between chunks."""
    silence_frames_count = int(base_params.framerate * pause_duration)
    if silence_frames_count <= 0:
        return b""
    sample_silence = b"\x80" if base_params.sampwidth == 1 else b"\x00"
    return (sample_silence * base_params.sampwidth * base_params.nchannels) * silence_frames_count


def concat_wav_files(
    chunk_paths: List[Union[str, Path]],
    output_path: Union[str, Path],
    pause_duration: float = 0.0,
) -> bool:
    """
    Synchronously concatenates a sequence of WAV chunks into a single WAV file.

    Validates audio format parameters (channels, sample width, framerate) across all chunks,
    applies micro-fade smoothing to 16-bit PCM chunk boundaries to avoid pop/click artifacts,
    and injects silence frames corresponding to `pause_duration` between chunks.

    Should be executed off the main event loop via `asyncio.to_thread()`.
    """
    if not chunk_paths:
        return False

    out_p = Path(output_path)
    valid_files, base_params = _collect_valid_chunks(chunk_paths)
    if not valid_files or base_params is None:
        return False

    try:
        out_p.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(out_p), "wb") as w_out:
            w_out.setparams(base_params)
            for idx, p in enumerate(valid_files):
                if pause_duration > 0 and idx > 0:
                    silence_bytes = _build_silence_bytes(base_params, pause_duration)
                    if silence_bytes:
                        w_out.writeframes(silence_bytes)
                raw_frames = b""
                try:
                    with wave.open(str(p), "rb") as w_in:
                        n_frames = w_in.getnframes()
                        raw_frames = w_in.readframes(n_frames)
                        if not raw_frames:
                            continue
                        raw_frames = _apply_micro_fade(raw_frames, base_params)
                except Exception as err:
                    logger.warning("Error reading frames from chunk %s: %s", p, err)
                    continue

                if raw_frames:
                    w_out.writeframes(raw_frames)
        return True
    except Exception as exc:
        logger.error("Failed to write concatenated WAV to %s: %s", out_p, exc)
        try:
            if out_p.exists():
                out_p.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    except BaseException:
        try:
            if out_p.exists():
                out_p.unlink(missing_ok=True)
        except OSError:
            pass
        raise
