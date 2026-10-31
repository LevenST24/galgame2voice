"""
Shared HTTP validation helpers for API routers in galgame2voice.
"""

from typing import Any
from fastapi import HTTPException, status

from galgame2voice.services.voice_manager import InsufficientMemoryError
from galgame2voice.services.voice_state import (
    ActiveVoicePersistenceError,
    VoiceProfileRemoved,
    persist_active_voice_profile,
)

__all__ = [
    "validate_user_id",
    "validate_positive_profile_id",
    "switch_voice_profile_or_raise",
    "persist_active_voice_profile_or_raise",
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


def _voice_persistence_http_error(error: ActiveVoicePersistenceError) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail={
            "error_code": error.code,
            "message": str(error),
            "voice_profile_id": error.profile_id,
        },
    )


async def persist_active_voice_profile_or_raise(profile_id: int) -> None:
    try:
        await persist_active_voice_profile(profile_id)
    except ActiveVoicePersistenceError as error:
        raise _voice_persistence_http_error(error) from error


async def switch_voice_profile_or_raise(manager: Any, profile: Any, force: bool = False) -> None:
    """
    Switches active voice profile via VoiceManager under its lock, translating
    failures to HTTP errors (503 memory, 502 load, 500 unconfirmed persistence,
    404 when the voice row is gone before it can be saved).
    """
    try:
        success = await manager.switch_profile(profile, persist=True, _already_locked=True, force=force)
    except VoiceProfileRemoved as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except ActiveVoicePersistenceError as error:
        raise _voice_persistence_http_error(error) from error
    except InsufficientMemoryError as mem_err:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(mem_err),
        ) from mem_err
    if not success:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="语音模型尚未加载。请在「设置中心 → 运行状态」启动语音引擎，再在「角色与会话」重新选择模型权重和参考音频。文字聊天仍可继续使用。",
        )
