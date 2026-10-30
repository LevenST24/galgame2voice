"""Confirm the saved default voice without claiming a cross-engine transaction."""

import logging

from galgame2voice.database import crud
from galgame2voice.database.session import get_db

logger = logging.getLogger("galgame2voice.services.voice_state")


class ActiveVoicePersistenceError(RuntimeError):
    code = "VOICE_PROFILE_SAVE_FAILED"

    def __init__(self, profile_id: int):
        self.profile_id = profile_id
        super().__init__(
            "启用音色尚未确认保存。请检查数据目录是否可写、磁盘是否有剩余空间，"
            "稍后重新选择该音色重试。已加载的模型保持当前状态，文字聊天仍可继续使用。"
        )


class VoiceProfileRemoved(Exception):
    """The voice row disappeared after its models loaded and before it was saved."""

    def __init__(self, profile_id: int):
        self.profile_id = profile_id
        super().__init__("所选音色已不存在，请在角色与会话中重新选择音色。")


async def persist_active_voice_profile(profile_id: int, db_path: str | None = None) -> None:
    """Raise on rejected/failed writes; loaded models remain available for retry."""
    try:
        async with get_db(db_path) as connection:
            if not await crud.set_active_voice_profile(connection, profile_id):
                raise RuntimeError("Database did not confirm the active voice update")
    except Exception as error:
        logger.error("Could not confirm saved voice profile %s", profile_id, exc_info=True)
        raise ActiveVoicePersistenceError(profile_id) from error
    logger.info("Confirmed saved voice profile %s", profile_id)
