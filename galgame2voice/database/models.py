"""
Pydantic data models and schemas for SQLite entities in galgame2voice.
Includes DB representations, Create/Update DTOs, and Safe Response models.
"""

from datetime import datetime
from typing import Optional, Dict, Any, List
from pydantic import BaseModel, Field, ConfigDict, model_validator


# ==================== Settings Models ====================

class SettingsBase(BaseModel):
    active_provider_id: str = "deepseek"
    active_voice_profile_id: Optional[int] = 1
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
    telegram_bot_username: str = "natsume_siki_bot"
    telegram_proxy_host: str = "127.0.0.1"
    telegram_proxy_port: int = Field(default=10809, ge=1, le=65535)
    telegram_proxy_enabled: bool = False
    telegram_admin_ids: str = ""  # Comma-separated Telegram user IDs allowed to run admin commands
    allow_private_llm_endpoints: bool = False  # Permit private/loopback LLM provider base URLs
    console_url: str = ""
    max_history_messages: int = Field(default=10, ge=1, le=100)


class SettingsUpdate(BaseModel):
    active_provider_id: Optional[str] = None
    active_voice_profile_id: Optional[int] = None
    gpt_sovits_url: Optional[str] = None
    audio_output_dir: Optional[str] = None
    audio_retention_minutes: Optional[int] = Field(default=None, ge=1)
    audio_cleanup_interval_sec: Optional[int] = Field(default=None, ge=10)
    speed_factor: Optional[float] = Field(default=None, ge=0.1, le=3.0)
    temperature: Optional[float] = Field(default=None, ge=0.0, le=2.0)
    top_k: Optional[int] = Field(default=None, ge=1, le=100)
    top_p: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    seed: Optional[int] = None
    batch_size: Optional[int] = Field(default=None, ge=1, le=16)
    text_split_method: Optional[str] = None
    fragment_interval: Optional[float] = Field(default=None, ge=0.0, le=5.0)
    telegram_bot_token: Optional[str] = None
    telegram_bot_username: Optional[str] = None
    telegram_proxy_host: Optional[str] = None
    telegram_proxy_port: Optional[int] = Field(default=None, ge=1, le=65535)
    telegram_proxy_enabled: Optional[bool] = None
    telegram_admin_ids: Optional[str] = None
    allow_private_llm_endpoints: Optional[bool] = None
    console_url: Optional[str] = None
    max_history_messages: Optional[int] = Field(default=None, ge=1, le=100)


class SettingsInDB(SettingsBase):
    id: int = 1
    telegram_bot_token: str = ""
    console_token: str = ""
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    model_config = ConfigDict(from_attributes=True)


class SettingsResponse(SettingsBase):
    telegram_bot_token: str = ""  # Masked
    console_token: str = ""
    updated_at: Optional[str] = None
    model_config = ConfigDict(from_attributes=True)


# ==================== Provider Models ====================

class ProviderBase(BaseModel):
    id: str
    name: str
    api_base_url: str
    chat_model: str
    stt_model: str = ""
    is_active: bool = False
    custom_headers: Dict[str, Any] = Field(default_factory=dict)


class ProviderCreate(ProviderBase):
    api_key: str = ""


class ProviderUpdate(BaseModel):
    name: Optional[str] = None
    api_base_url: Optional[str] = None
    api_key: Optional[str] = None
    chat_model: Optional[str] = None
    stt_model: Optional[str] = None
    is_active: Optional[bool] = None
    custom_headers: Optional[Dict[str, Any]] = None


class ProviderInDB(ProviderBase):
    api_key: str = ""
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    model_config = ConfigDict(from_attributes=True)


class ProviderResponse(ProviderBase):
    api_key: str = ""  # Masked
    updated_at: Optional[str] = None
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
    name: Optional[str] = None
    description: Optional[str] = None
    gpt_weights_path: Optional[str] = None
    sovits_weights_path: Optional[str] = None
    ref_audio_path: Optional[str] = None
    prompt_text: Optional[str] = None
    prompt_lang: Optional[str] = None
    text_lang: Optional[str] = None
    system_prompt: Optional[str] = None
    is_default: Optional[bool] = None

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
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
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
    voice_profile_id: Optional[int] = 1
    custom_system_prompt: Optional[str] = None
    token_budget: int = 4096


class SessionCreate(SessionBase):
    pass


class SessionUpdate(BaseModel):
    voice_profile_id: Optional[int] = None
    custom_system_prompt: Optional[str] = None
    token_budget: Optional[int] = Field(default=None, ge=128, le=131072)


class SessionInDB(SessionBase):
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
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
    created_at: Optional[str] = None
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
    voice_profile_id: Optional[int] = 1
    params_hash: str
    file_path: str
    file_size: int
    duration_ms: int = 0
    hit_count: int = 0
    created_at: Optional[str] = None
    last_accessed_at: Optional[str] = None
    model_config = ConfigDict(from_attributes=True)


class CacheStatsResponse(BaseModel):
    total_files: int = 0
    total_size_bytes: int = 0
    total_size_mb: float = 0.0
    total_hits: int = 0
    total_misses: int = 0
    hit_rate_percent: float = 0.0


class TokenUsageMetric(BaseModel):
    id: Optional[int] = None
    timestamp: Optional[str] = None
    session_id: str = "default"
    channel: str = "web"
    provider_id: str = "deepseek"
    model_name: str = "deepseek-chat"
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    estimated_cost: float = 0.0
    ttft_ms: float = 0.0
    tts_first_chunk_ms: float = 0.0
    total_latency_ms: float = 0.0
    tts_cached_chunks: int = 0
    tts_generated_chunks: int = 0
    model_config = ConfigDict(from_attributes=True)


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
    providers: List[ProviderMetricItem] = Field(default_factory=list)


class LatencyTrendItem(BaseModel):
    timestamp: str
    ttft_ms: float
    tts_first_chunk_ms: float
    total_latency_ms: float
    model_name: str = ""
    provider_id: str = ""


class LatencyTrendResponse(BaseModel):
    trend: List[LatencyTrendItem] = Field(default_factory=list)


# ==================== Memory & Affection Models ====================

class UserMemoryBase(BaseModel):
    user_id: str = "default_user"
    character_id: Optional[int] = 1
    category: str = "preference"  # 'nickname', 'preference', 'promise', 'identity', 'event', 'taboo'
    fact_key: str
    fact_value: str
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    source_message_id: Optional[int] = None
    recall_count: int = 0
    last_recalled_at: Optional[str] = None


class UserMemoryCreate(UserMemoryBase):
    pass


class UserMemoryUpdate(BaseModel):
    category: Optional[str] = None
    fact_key: Optional[str] = None
    fact_value: Optional[str] = None
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    recall_count: Optional[int] = None
    last_recalled_at: Optional[str] = None


class UserMemoryInDB(UserMemoryBase):
    id: int
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
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
    unlocked_dialogues: List[str] = Field(default_factory=list)
    custom_nickname: Optional[str] = None


class CharacterAffectionCreate(CharacterAffectionBase):
    pass


class CharacterAffectionUpdate(BaseModel):
    affection_score: Optional[int] = Field(default=None, ge=0, le=100)
    affection_level: Optional[int] = Field(default=None, ge=1, le=5)
    current_emotion: Optional[str] = None
    interaction_count: Optional[int] = None
    daily_points_earned: Optional[int] = None
    last_interaction_date: Optional[str] = None
    unlocked_dialogues: Optional[List[str]] = None
    custom_nickname: Optional[str] = None


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
    custom_nickname: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    model_config = ConfigDict(from_attributes=True)


class CharacterAffectionResponse(CharacterAffectionBase):
    id: int
    level_name: str = "初识/生疏"
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    model_config = ConfigDict(from_attributes=True)


