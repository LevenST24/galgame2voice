"""
Shared HTTP validation helpers for API routers in galgame2voice.
"""

from typing import Any
from fastapi import HTTPException, status

from galgame2voice.services.voice_manager import InsufficientMemoryError

__all__ = [
    "validate_user_id",
    "validate_positive_profile_id",
    "switch_voice_profile_or_raise",
]


def validate_positive_profile_id(profile_id: int, field_name: str = "Profile ID") -> int:
    """Validates that a profile ID is a positive integer >= 1."""
    if profile_id < 1:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"{field_name} must be a positive integer >= 1",
        )
    return profile_id



def validate_user_id(user_id: str) -> str:
    """Validates user_id parameter, ensuring non-empty stripped value."""
    clean_user = user_id.strip() if user_id else ""
    if not clean_user:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="user_id cannot be empty",
        )
    return clean_user


async def switch_voice_profile_or_raise(manager: Any, profile: Any, force: bool = False) -> None:
    """
    Switches active voice profile via VoiceManager under its lock, translating
    engine errors to standardized HTTPExceptions (503 for memory, 502 for load failure).
    """
    try:
        success = await manager.switch_profile(profile, persist=True, _already_locked=True, force=force)
    except InsufficientMemoryError as mem_err:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(mem_err),
        ) from mem_err
    if not success:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to load GPT/SoVITS model weights onto backend service",
        )
