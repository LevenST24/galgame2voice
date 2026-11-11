"""
Pydantic data models and schemas for SQLite entities in galgame2voice.
Includes DB representations, Create/Update DTOs, and Safe Response models.
"""

import json
from typing import Any, Literal
from pydantic import BaseModel, Field, ConfigDict, ValidationError, model_validator


# ==================== Settings Models ====================

class SettingsBase(BaseModel):
    active_provider_id: str = "deepseek"
    active_voice_profile_id: int | None = None
    gpt_sovits_url: str = "http://127.0.0.1:9880"
    audio_output_dir: str = "audio"
    audio_retention_minutes: int = Field(default=30, ge=1)
    audio_cleanup_interval_sec: int = Field(default=600, ge=10)
    speed_factor: float = Field(default=1.0, ge=0.1, le=3.0)
    temperature: float = Field(default=1.0, ge=0.0, le=2.0)
    top_k: int = Field(default=15, ge=1, le=100)
    top_p: float = Field(default=1.0, ge=0.0, le=1.0)
    seed: int = Field(default=-1)
    batch_size: int = Field(default=1, ge=1, le=16)
    text_split_method: str = "cut1"
    fragment_interval: float = Field(default=0.3, ge=0.0, le=5.0)
    allow_private_llm_endpoints: bool = False  # Permit private/loopback LLM provider base URLs
    console_url: str = ""
    max_history_messages: int = Field(default=10, ge=1, le=100)
    inference_precision: str = "auto"  # "auto" (probe), "fp16" (half), "fp32" (single), or "cpu" (force CPU fp32)
    stt_engine: str | None = "browser"


class SettingsUpdate(BaseModel):
    active_provider_id: str | None = None
    active_voice_profile_id: int | None = None
    gpt_sovits_url: str | None = None
    audio_output_dir: str | None = None
    audio_retention_minutes: int | None = Field(default=None, ge=1)
    audio_cleanup_interval_sec: int | None = Field(default=None, ge=10)
    speed_factor: float | None = Field(default=None, ge=0.1, le=3.0)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    top_k: int | None = Field(default=None, ge=1, le=100)
    top_p: float | None = Field(default=None, ge=0.0, le=1.0)
    seed: int | None = None
    batch_size: int | None = Field(default=None, ge=1, le=16)
    text_split_method: str | None = None
    fragment_interval: float | None = Field(default=None, ge=0.0, le=5.0)
    allow_private_llm_endpoints: bool | None = None
    console_url: str | None = None
    max_history_messages: int | None = Field(default=None, ge=1, le=100)
    inference_precision: str | None = None
    stt_engine: str | None = None
    console_token: str | None = None


class SettingsInDB(SettingsBase):
    id: int = 1
    console_token: str = ""
    created_at: str | None = None
    updated_at: str | None = None
    model_config = ConfigDict(from_attributes=True)


class SettingsResponse(SettingsBase):
    console_token: str = ""
    stt_engine: str | None = "browser"
    updated_at: str | None = None
    model_config = ConfigDict(from_attributes=True)


# ==================== Provider Models ====================

class ProviderBase(BaseModel):
    id: str
    name: str
    api_base_url: str
    chat_model: str
    stt_model: str = ""
    is_active: bool = False
    custom_headers: dict[str, Any] = Field(default_factory=dict)
    reasoning_effort: Literal["none", "low", "medium", "high"] | None = None


class ProviderCreate(ProviderBase):
    api_key: str = ""


class ProviderUpdate(BaseModel):
    name: str | None = None
    api_base_url: str | None = None
    api_key: str | None = None
    chat_model: str | None = None
    stt_model: str | None = None
    is_active: bool | None = None
    custom_headers: dict[str, Any] | None = None
    reasoning_effort: Literal["none", "low", "medium", "high"] | None = None


class ProviderInDB(ProviderBase):
    api_key: str = ""
    created_at: str | None = None
    updated_at: str | None = None
    model_config = ConfigDict(from_attributes=True)


class ProviderResponse(ProviderBase):
    api_key: str = ""  # Masked
    updated_at: str | None = None
    model_config = ConfigDict(from_attributes=True)


# ==================== Voice Profile Models ====================

class VoiceProfileBase(BaseModel):
    name: str
    description: str = ""
    gpt_weights_path: str
    sovits_weights_path: str
    ref_audio_path: str = ""
    prompt_text: str = ""
    prompt_lang: str = "ja"
    text_lang: str = "ja"
    system_prompt: str = ""
    is_default: bool = False

    @model_validator(mode="before")
    @classmethod
    def remap_legacy_fields(cls, data: Any) -> Any:
        if isinstance(data, dict):
            d = dict(data)
            if "refer_audio_path" in d and ("ref_audio_path" not in d or not d["ref_audio_path"]):
                d["ref_audio_path"] = d["refer_audio_path"]
            if "refer_text" in d and ("prompt_text" not in d or not d["prompt_text"]):
                d["prompt_text"] = d["refer_text"]
            if "refer_language" in d and ("prompt_lang" not in d or not d["prompt_lang"]):
                d["prompt_lang"] = d["refer_language"]
            if "prompt_language" in d and ("prompt_lang" not in d or not d["prompt_lang"]):
                d["prompt_lang"] = d["prompt_language"]
            if "text_language" in d and ("text_lang" not in d or not d["text_lang"]):
                d["text_lang"] = d["text_language"]
            return d
        return data


class VoiceProfileCreate(VoiceProfileBase):
    pass


class VoiceProfileUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    gpt_weights_path: str | None = None
    sovits_weights_path: str | None = None
    ref_audio_path: str | None = None
    prompt_text: str | None = None
    prompt_lang: str | None = None
    text_lang: str | None = None
    system_prompt: str | None = None
    is_default: bool | None = None

    @model_validator(mode="before")
    @classmethod
    def remap_legacy_fields(cls, data: Any) -> Any:
        if isinstance(data, dict):
            d = dict(data)
            if "refer_audio_path" in d and "ref_audio_path" not in d:
                d["ref_audio_path"] = d["refer_audio_path"]
            if "refer_text" in d and "prompt_text" not in d:
                d["prompt_text"] = d["refer_text"]
            if "refer_language" in d and "prompt_lang" not in d:
                d["prompt_lang"] = d["refer_language"]
            if "prompt_language" in d and "prompt_lang" not in d:
                d["prompt_lang"] = d["prompt_language"]
            if "text_language" in d and "text_lang" not in d:
                d["text_lang"] = d["text_language"]
            return d
        return data


class VoiceProfileInDB(VoiceProfileBase):
    id: int
    created_at: str | None = None
    updated_at: str | None = None
    model_config = ConfigDict(from_attributes=True)


class VoiceProfileResponse(VoiceProfileInDB):
    @property
    def refer_audio_path(self) -> str:
        return self.ref_audio_path

    @property
    def refer_text(self) -> str:
        return self.prompt_text

    @property
    def refer_language(self) -> str:
        return self.prompt_lang


# ==================== Session Models ====================

class SessionBase(BaseModel):
    id: str
    channel: str = "web"
    user_id: str = ""
    voice_profile_id: int | None = 1
    custom_system_prompt: str | None = None
    title: str = ""
    settings_json: str | None = None
    token_budget: int = 4096


class SessionCreate(SessionBase):
    pass


class SessionUpdate(BaseModel):
    voice_profile_id: int | None = None
    custom_system_prompt: str | None = None
    title: str | None = None
    settings_json: str | None = None
    token_budget: int | None = Field(default=None, ge=128, le=131072)


class SessionInDB(SessionBase):
    created_at: str | None = None
    updated_at: str | None = None
    model_config = ConfigDict(from_attributes=True)


class SessionResponse(SessionInDB):
    pass


# ==================== Message Models ====================

class ReplyLineAct(BaseModel):
    model_config = ConfigDict(strict=True)

    face: str = ""
    pose: str = ""
    motion: Literal["nod", "bounce", "startle", "sigh", "fidget", "head-shake", "lean-in", "step-back"] | None = None
    blush: bool = False
    fx: Literal["sweat", "anger", "heart", "sparkle", "question", "exclamation"] | None = None


class ReplyLineChunk(BaseModel):
    model_config = ConfigDict(strict=True)

    index: int = Field(ge=0)
    line: int = Field(ge=0)
    audio_url: str
    sentence: str


class ReplyLineAudio(BaseModel):
    model_config = ConfigDict(strict=True)

    status: Literal["ready", "skipped", "failed"]
    chunks: list[ReplyLineChunk]
    generated_at: int | None = Field(default=None, ge=0)
    voice_profile_id: int | None = Field(default=None, ge=1)
    voice_name: str | None = Field(default=None, max_length=100)


class ReplyLineRecord(BaseModel):
    """Shared complete record shape for persistence, history and backup export."""

    model_config = ConfigDict(strict=True)

    index: int = Field(ge=0)
    zh: str = Field(max_length=10000)
    ja: str = Field(max_length=10000)
    emotion: str = Field(max_length=32)
    act: ReplyLineAct
    audio: ReplyLineAudio


def decode_lines(raw: Any) -> list[dict[str, Any]] | None:
    """Validate the complete array, falling back to legacy text on malformed data."""
    if raw is None:
        return None
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(parsed, list):
            return None
        return [ReplyLineRecord.model_validate(item).model_dump(exclude_unset=True) for item in parsed]
    except (ValueError, TypeError, ValidationError):
        return None


class MessageBase(BaseModel):
    session_id: str
    role: str
    content_chinese: str
    content_japanese: str = ""
    audio_url: str = ""
    latency_ms: int = 0
    lines: list[dict[str, Any]] | None = None
    reply_to_message_id: int | None = None

    @model_validator(mode="before")
    @classmethod
    def _validate_lines(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if "lines" not in data and "lines_json" in data:
                data = {**data, "lines": decode_lines(data.get("lines_json"))}
            elif "lines" in data:
                data = {**data, "lines": decode_lines(data["lines"])}
        return data


class MessageCreate(MessageBase):
    pass


class MessageInDB(MessageBase):
    id: int
    created_at: str | None = None
    model_config = ConfigDict(from_attributes=True)


class MessageResponse(MessageInDB):
    pass


# ==================== TTS Options Helper ====================

class TtsOptions(BaseModel):
    speed_factor: float = Field(default=1.0, ge=0.1, le=3.0)
    temperature: float = Field(default=1.0, ge=0.0, le=2.0)
    top_k: int = Field(default=15, ge=1, le=100)
    top_p: float = Field(default=1.0, ge=0.0, le=1.0)
    seed: int = -1
    batch_size: int = Field(default=1, ge=1, le=16)
    text_split_method: str = "cut1"
    fragment_interval: float = Field(default=0.3, ge=0.0, le=5.0)


# ==================== TTS Cache & Metrics Models ====================

class TtsCacheEntry(BaseModel):
    cache_key: str
    text: str
    clean_text: str
    voice_profile_id: int | None = 1
    params_hash: str
    file_path: str
    file_size: int
    duration_ms: int = 0
    hit_count: int = 0
    created_at: str | None = None
    last_accessed_at: str | None = None
    model_config = ConfigDict(from_attributes=True)


class CacheStatsResponse(BaseModel):
    total_files: int = 0
    # Alias consumed by the dashboard (global_settings.js); kept in sync with
    # total_files so the frontend's `total_entries` read resolves correctly.
    total_entries: int = 0
    total_size_bytes: int = 0
    total_size_mb: float = 0.0
    total_hits: int = 0
    total_misses: int = 0
    hit_rate_percent: float = 0.0
    memory_hits: int = 0
    db_hits: int = 0
    estimated_saved_seconds: float = 0.0


# ==================== Memory & Affection Models ====================

class UserMemoryBase(BaseModel):
    user_id: str = "default_user"
    character_id: int | None = 1
    category: str = "preference"  # 'nickname', 'preference', 'promise', 'identity', 'event', 'taboo'
    fact_key: str
    fact_value: str
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    source_message_id: int | None = None
    recall_count: int = 0
    last_recalled_at: str | None = None


class UserMemoryCreate(UserMemoryBase):
    pass


class UserMemoryUpdate(BaseModel):
    category: str | None = None
    fact_key: str | None = None
    fact_value: str | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    recall_count: int | None = None
    last_recalled_at: str | None = None


class UserMemoryInDB(UserMemoryBase):
    id: int
    created_at: str | None = None
    updated_at: str | None = None
    model_config = ConfigDict(from_attributes=True)


class UserMemoryResponse(UserMemoryInDB):
    pass


class CharacterAffectionBase(BaseModel):
    user_id: str = "default_user"
    character_id: int = 1
    affection_score: int = Field(default=0, ge=0, le=100)
    affection_level: int = Field(default=1, ge=1, le=5)
    current_emotion: str = "normal"
    interaction_count: int = 0
    daily_points_earned: int = 0
    last_interaction_date: str = ""
    unlocked_dialogues: list[str] = Field(default_factory=list)
    custom_nickname: str | None = None


class CharacterAffectionCreate(CharacterAffectionBase):
    pass


class CharacterAffectionUpdate(BaseModel):
    affection_score: int | None = Field(default=None, ge=0, le=100)
    affection_level: int | None = Field(default=None, ge=1, le=5)
    current_emotion: str | None = None
    interaction_count: int | None = None
    daily_points_earned: int | None = None
    last_interaction_date: str | None = None
    unlocked_dialogues: list[str] | None = None
    custom_nickname: str | None = None


class CharacterAffectionInDB(BaseModel):
    id: int
    user_id: str = "default_user"
    character_id: int = 1
    affection_score: int = 0
    affection_level: int = 1
    current_emotion: str = "normal"
    interaction_count: int = 0
    daily_points_earned: int = 0
    last_interaction_date: str = ""
    unlocked_dialogues: str = "[]"
    custom_nickname: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    model_config = ConfigDict(from_attributes=True)


class CharacterAffectionResponse(CharacterAffectionBase):
    id: int
    level_name: str = "初识/生疏"
    created_at: str | None = None
    updated_at: str | None = None
    model_config = ConfigDict(from_attributes=True)


