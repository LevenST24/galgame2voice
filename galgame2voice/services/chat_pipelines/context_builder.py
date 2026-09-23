"""
Context Builder Pipeline Step.
Constructs system prompts, character lore, memory RAG blocks, and conversational history
into model-ready ChatMessage lists.
"""

from __future__ import annotations

import logging
from typing import Any, List, Optional
import aiosqlite

from galgame2voice.adapters.base import ChatMessage
from galgame2voice.database import crud

logger = logging.getLogger(__name__)


def upgrade_legacy_system_prompt(prompt: str) -> str:
    """
    Upgrades legacy prompt formats that lack dynamic TTS parameters.
    Ensures modern JSON output instructions include speed, temperature, and emotion.
    """
    if not prompt:
        return prompt

    if '"tts":' not in prompt and '{"chinese":' in prompt:
        prompt = prompt.replace(
            '{"chinese": "显示给玩家的中文台词", "japanese": "对应的口语化日文台词"}',
            '{"tts": {"speed": 1.05, "temp": 0.95, "emotion": "gentle"}, "chinese": "显示给玩家的中文台词", "japanese": "对应的口语化日文台词"}'
        )
        if "动态决定语音推理参数" not in prompt:
            prompt = prompt.replace(
                "你必须严格输出如下 JSON 格式",
                "你必须严格输出如下 JSON 格式，在最开头根据语境动态决定语音推理参数（speed 语速: 0.5~1.5 请大胆调节！激动时可设为1.3以上，低落时设为0.7以下, temp 温度: 0.60~1.20, emotion 情绪: gentle|shy|happy|tsundere|cool|sad|angry）"
            )
    return prompt


async def build_chat_context(
    conn: aiosqlite.Connection,
    session_id: str,
    user_prompt: str,
    session_manager: Any,
    memory_service: Any,
    character_name: Optional[str] = None,
    system_prompt_override: Optional[str] = None,
    max_history_override: Optional[int] = None,
    active_profile: Optional[Any] = None,
    session: Optional[Any] = None,
) -> List[ChatMessage]:
    """
    Builds the complete message history and prompt context for LLM execution:
    1. Resolves session and character voice profile.
    2. Resolves effective system prompt (override -> custom -> profile template -> default).
    3. Retrieves relevant long-term memory facts and affection status.
    4. Truncates dialogue history within the configured window.
    """
    if session is None:
        session = await crud.get_session(conn, session_id)

    if active_profile is None:
        target_profile_id = session.voice_profile_id if session else None
        if target_profile_id is not None:
            active_profile = await crud.get_voice_profile(conn, int(target_profile_id))
        if active_profile is None and character_name:
            active_profile = await crud.get_voice_profile_by_name(conn, character_name)
        if active_profile is None:
            active_profile = await crud.get_active_voice_profile(conn)

    # 人设提示词只有一个权威源：角色包 manifest.json（DB voice_profiles 是它的镜像）。
    # 会话级 custom_system_prompt 已废弃：它让同一角色在不同会话里说不同人设，
    # 而且设置面板不再展示它，留着只会造成看不见的设定分叉。
    if system_prompt_override and system_prompt_override.strip():
        system_prompt = system_prompt_override.strip()
    else:
        system_prompt = (
            active_profile.system_prompt
            if active_profile and active_profile.system_prompt
            else session_manager.DEFAULT_SYSTEM_TEMPLATE
        )

    system_prompt = upgrade_legacy_system_prompt(system_prompt)
    char_name = character_name or (active_profile.name if active_profile else "Character")

    settings_raw = await crud.get_settings_raw(conn)
    max_history = max_history_override or (settings_raw.max_history_messages if settings_raw else 10)

    if session is None:
        session = await crud.get_session(conn, session_id)
    user_id = session.user_id if session and session.user_id else "default_user"
    profile_id = active_profile.id if active_profile else 1

    # RAG memory retrieval & affection context injection
    memory_block = None
    if memory_service is not None:
        try:
            recalled_memories = await memory_service.retrieve_relevant_memories(
                user_id=user_id,
                character_id=profile_id,
                prompt=user_prompt,
                top_k=5,
                conn=conn,
            )
            affection = await crud.get_or_create_character_affection(conn, user_id=user_id, character_id=profile_id)
            aff_info = {
                "score": affection.affection_score,
                "level": affection.affection_level,
                "level_name": affection.level_name,
                "emotion": affection.current_emotion,
                "nickname": affection.custom_nickname,
            }
            memory_block = memory_service.format_memory_prompt_block(recalled_memories, aff_info)
        except Exception as e:
            logger.warning("Failed to retrieve memories for prompt injection: %s", e)
            memory_block = None

    return await session_manager.build_chat_messages(
        session_id=session_id,
        user_prompt=user_prompt,
        character_name=char_name,
        custom_system_prompt=system_prompt,
        max_messages=max_history,
        memory_prompt_block=memory_block,
        conn=conn,
    )
