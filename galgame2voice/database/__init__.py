"""
Database module for galgame2voice.
"""

from galgame2voice.database.session import get_db, init_db, configure_connection
from galgame2voice.database.models import (
    SettingsBase, SettingsInDB, SettingsResponse, SettingsUpdate,
    ProviderBase, ProviderInDB, ProviderResponse, ProviderCreate, ProviderUpdate,
    VoiceProfileBase, VoiceProfileInDB, VoiceProfileResponse, VoiceProfileCreate, VoiceProfileUpdate,
    SessionBase, SessionInDB, SessionResponse, SessionCreate, SessionUpdate,
    MessageBase, MessageInDB, MessageResponse, MessageCreate,
    TtsOptions,
    UserMemoryBase, UserMemoryCreate, UserMemoryUpdate, UserMemoryInDB, UserMemoryResponse,
    CharacterAffectionBase, CharacterAffectionCreate, CharacterAffectionUpdate, CharacterAffectionInDB, CharacterAffectionResponse
)
from galgame2voice.database.crud import (
    mask_api_key, is_masked_key,
    get_settings, get_settings_raw, update_settings, verify_console_token,
    list_providers, get_provider, get_provider_raw, get_active_provider, get_active_provider_raw,
    create_provider, update_provider, set_active_provider, delete_provider,
    list_voice_profiles, get_voice_profile, get_active_voice_profile,
    create_voice_profile, update_voice_profile, set_active_voice_profile, delete_voice_profile,
    get_or_create_session, get_session, list_sessions, delete_session, clear_session_messages,
    add_message, get_recent_messages, count_session_messages,
    create_memory, get_memory, list_memories, update_memory, delete_memory, clear_memories,
    upsert_memory, record_memory_recall,
    calculate_affection_level, get_or_create_character_affection, get_character_affection,
    update_character_affection, reset_character_affection, increment_affection
)

__all__ = [
    "get_db", "init_db", "configure_connection",
    "SettingsBase", "SettingsInDB", "SettingsResponse", "SettingsUpdate",
    "ProviderBase", "ProviderInDB", "ProviderResponse", "ProviderCreate", "ProviderUpdate",
    "VoiceProfileBase", "VoiceProfileInDB", "VoiceProfileResponse", "VoiceProfileCreate", "VoiceProfileUpdate",
    "SessionBase", "SessionInDB", "SessionResponse", "SessionCreate", "SessionUpdate",
    "MessageBase", "MessageInDB", "MessageResponse", "MessageCreate",
    "TtsOptions",
    "UserMemoryBase", "UserMemoryCreate", "UserMemoryUpdate", "UserMemoryInDB", "UserMemoryResponse",
    "CharacterAffectionBase", "CharacterAffectionCreate", "CharacterAffectionUpdate", "CharacterAffectionInDB", "CharacterAffectionResponse",
    "mask_api_key", "is_masked_key",
    "get_settings", "get_settings_raw", "update_settings", "verify_console_token",
    "list_providers", "get_provider", "get_provider_raw", "get_active_provider", "get_active_provider_raw",
    "create_provider", "update_provider", "set_active_provider", "delete_provider",
    "list_voice_profiles", "get_voice_profile", "get_active_voice_profile",
    "create_voice_profile", "update_voice_profile", "set_active_voice_profile", "delete_voice_profile",
    "get_or_create_session", "get_session", "list_sessions", "delete_session", "clear_session_messages",
    "add_message", "get_recent_messages", "count_session_messages",
    "create_memory", "get_memory", "list_memories", "update_memory", "delete_memory", "clear_memories",
    "upsert_memory", "record_memory_recall",
    "calculate_affection_level", "get_or_create_character_affection", "get_character_affection",
    "update_character_affection", "reset_character_affection", "increment_affection"
]

