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
