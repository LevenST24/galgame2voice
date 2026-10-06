"""
Typed error contract for galgame2voice.

Why this exists
---------------
When a dependency is missing or an external tool fails, the caller needs to know
*what* went wrong without string-matching a message. Before this module the audio
path signalled every failure with a bare ``RuntimeError``, so a missing ffmpeg
install, a corrupt payload and a non-zero ffmpeg exit were indistinguishable to
anything except a human reading the text — and the CLI, the API layer and the
tests could not agree on a stable contract.

Every subclass keeps the base class callers already catch, so existing
``except RuntimeError`` / ``except ValueError`` handlers and tests keep working
while callers migrate to the semantic types. ``code`` is a stable,
machine-readable identifier suitable for API payloads, logs and support tickets.
"""

__all__ = [
    "Galgame2VoiceError",
    "ExternalDependencyError",
    "FFmpegUnavailable",
    "AudioTranscodeFailed",
]


class Galgame2VoiceError(RuntimeError):
    """Base class for every error this application raises deliberately."""

    code = "INTERNAL_ERROR"


class ExternalDependencyError(Galgame2VoiceError):
    """A required external program or service is missing or unusable."""

    code = "EXTERNAL_DEPENDENCY_UNAVAILABLE"


class FFmpegUnavailable(ExternalDependencyError):
    """ffmpeg could not be located, so audio conversion cannot run at all.

    Recoverable by the user: install ffmpeg or point ``FFMPEG_PATH`` at it.

    This is deliberately *not* a ``ValueError``: before this module a missing
    dependency and a corrupt payload reached callers through the same generic
    audio error, so nothing could tell "install ffmpeg" apart from "that file is
    broken" without matching the message.
    """

    code = "FFMPEG_UNAVAILABLE"


class AudioTranscodeFailed(Galgame2VoiceError, ValueError):
    """ffmpeg ran but the conversion did not produce usable audio.

    Also a ``ValueError`` for backward compatibility: the audio helpers have
    always surfaced conversion failures as ``ValueError("Audio conversion
    failed: ...")``, and callers (plus the test suite) catch exactly that. The
    semantic type and its ``code`` are additive.
    """

    code = "AUDIO_TRANSCODE_FAILED"
