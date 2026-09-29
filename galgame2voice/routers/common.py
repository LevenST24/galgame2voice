"""
Shared HTTP validation helpers for API routers in galgame2voice.
"""

from fastapi import HTTPException, status


def validate_user_id(user_id: str) -> str:
    """Validates user_id parameter, ensuring non-empty stripped value."""
    clean_user = user_id.strip() if user_id else ""
    if not clean_user:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="user_id cannot be empty",
        )
    return clean_user
