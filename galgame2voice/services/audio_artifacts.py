"""Ownership and retention identity of newly generated, non-cache audio files."""

import asyncio
import logging
from pathlib import Path
import re
import threading

from galgame2voice.utils.async_tasks import await_protected

logger = logging.getLogger(__name__)
REPAIR_AUDIO_PREFIX = "repair"
_REPAIR_AUDIO_NAME = re.compile(re.escape(REPAIR_AUDIO_PREFIX) + r"_[0-9a-f]{12}\.wav\Z")


def is_repair_audio_name(name: str) -> bool:
    return _REPAIR_AUDIO_NAME.fullmatch(name) is not None


def _unlink_owned_file(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        # Preserve the original failure/cancellation. The periodic cleaner can
        # retry when Windows releases the file or the directory becomes writable.
        logger.warning("Deferred cleanup of unpublished audio (%s).", type(exc).__name__)


async def write_new_audio_file(path: Path, audio: bytes) -> None:
    """Write exclusively; a cancelled worker may finish only to discard its file.

    Cancelling to_thread cannot stop an in-progress OS write. The worker checks
    abandonment after closing the file; the caller also cleans up a write which
    finished just before cancellation was delivered. Neither owns a preexisting
    filename, including when exclusive open fails.
    """
    abandoned, created, closed = threading.Event(), threading.Event(), threading.Event()

    def discard_created():
        if created.is_set():
            _unlink_owned_file(path)

    def write():
        try:
            with path.open("xb") as output:
                created.set()
                if not abandoned.is_set() and output.write(audio) != len(audio):
                    raise OSError("Incomplete synthesized audio write")
        except BaseException:
            discard_created()
            raise
        finally:
            closed.set()
            if abandoned.is_set():
                discard_created()

    try:
        await asyncio.to_thread(write)
    except asyncio.CancelledError:
        abandoned.set()
        if closed.is_set():
            await await_protected(asyncio.to_thread(discard_created))
        raise


async def discard_repair_audio(audio_dir: Path, path: Path) -> None:
    """Discard a completed repair owned by a rejected request, never shared cache."""
    def discard():
        try:
            candidate = Path(path)
            if (not is_repair_audio_name(candidate.name) or candidate.is_symlink()
                    or candidate.parent.resolve() != Path(audio_dir).resolve()):
                logger.warning("Skipped audio cleanup outside the owned repair namespace.")
                return
            _unlink_owned_file(candidate)
        except OSError as exc:
            logger.warning("Deferred cleanup of unpublished repair (%s).", type(exc).__name__)
    await await_protected(asyncio.to_thread(discard))


async def discard_new_audio_file(path: Path) -> None:
    """Reap a unique file created by the caller's own uncached synthesis job."""
    await await_protected(asyncio.to_thread(_unlink_owned_file, path))
