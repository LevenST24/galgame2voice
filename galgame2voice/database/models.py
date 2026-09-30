"""
Pydantic data models and schemas for SQLite entities in galgame2voice.
Includes DB representations, Create/Update DTOs, and Safe Response models.
"""

from typing import Any
from pydantic import BaseModel, Field, ConfigDict, model_validator


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
    telegram_bot_username: str = "galgame2voice_bot"
    telegram_proxy_host: str = "127.0.0.1"
    telegram_proxy_port: int = Field(default=10809, ge=1, le=65535)
    telegram_proxy_enabled: bool = False
    telegram_enabled: bool = False  # Explicit toggle: False by default to prevent polling/terminal spam
    telegram_admin_ids: str = ""  # Comma-separated Telegram user IDs allowed to run admin commands
    allow_private_llm_endpoints: bool = False  # Permit private/loopback LLM provider base URLs
    console_url: str = ""
    max_history_messages: int = Field(default=10, ge=1, le=100)
    inference_precision: str = "auto"  # "auto" (probe), "fp16" (half), or "fp32" (single)
    stt_engine: str | None = "browser"
    telegram_chat_id: str | None = None

    @model_validator(mode="after")
    def sync_telegram_chat_id(self) -> "SettingsBase":
        if self.telegram_chat_id is None:
            self.telegram_chat_id = self.telegram_admin_ids
        return self


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
    telegram_enabled: bool | None = None
    telegram_bot_token: str | None = None
    telegram_bot_username: str | None = None
    telegram_proxy_host: str | None = None
    telegram_proxy_port: int | None = Field(default=None, ge=1, le=65535)
    telegram_proxy_enabled: bool | None = None
    telegram_admin_ids: str | None = None
    telegram_chat_id: str | None = None
    allow_private_llm_endpoints: bool | None = None
    console_url: str | None = None
    max_history_messages: int | None = Field(default=None, ge=1, le=100)
    inference_precision: str | None = None
    stt_engine: str | None = None
    console_token: str | None = None

    @model_validator(mode="after")
    def sync_telegram_ids(self) -> "SettingsUpdate":
        if self.telegram_admin_ids is None and self.telegram_chat_id is not None:
            self.telegram_admin_ids = self.telegram_chat_id
        elif self.telegram_chat_id is None and self.telegram_admin_ids is not None:
            self.telegram_chat_id = self.telegram_admin_ids
        return self


class SettingsInDB(SettingsBase):
    id: int = 1
    telegram_bot_token: str = ""
    console_token: str = ""
    created_at: str | None = None
    updated_at: str | None = None
    model_config = ConfigDict(from_attributes=True)


class SettingsResponse(SettingsBase):
    telegram_bot_token: str = ""  # Masked
    console_token: str = ""
    stt_engine: str | None = "browser"
    telegram_chat_id: str | None = None
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

class MessageBase(BaseModel):
    session_id: str
    role: str
    content_chinese: str
    content_japanese: str = ""
    audio_url: str = ""
    latency_ms: int = 0


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
    total_size_bytes: int = 0
    total_size_mb: float = 0.0
    total_hits: int = 0
    total_misses: int = 0
    hit_rate_percent: float = 0.0
    memory_hits: int = 0
    db_hits: int = 0
    estimated_saved_seconds: float = 0.0


class MetricsOverviewResponse(BaseModel):
    total_requests: int = 0
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_usd: float = 0.0
    estimated_cost_cny: float = 0.0
    avg_ttft_ms: float = 0.0
    avg_tts_first_chunk_ms: float = 0.0
    avg_total_latency_ms: float = 0.0
    cache_stats: CacheStatsResponse = Field(default_factory=CacheStatsResponse)
    tts_speed: dict[str, Any] | None = None


class ProviderMetricItem(BaseModel):
    provider_id: str
    name: str
    request_count: int = 0
    total_tokens: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    estimated_cost_usd: float = 0.0
    percentage: float = 0.0


class ProvidersMetricsResponse(BaseModel):
    providers: list[ProviderMetricItem] = Field(default_factory=list)


class LatencyTrendItem(BaseModel):
    timestamp: str
    ttft_ms: float
    tts_first_chunk_ms: float
    total_latency_ms: float
    model_name: str = ""
    provider_id: str = ""


class LatencyTrendResponse(BaseModel):
    trend: list[LatencyTrendItem] = Field(default_factory=list)


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


