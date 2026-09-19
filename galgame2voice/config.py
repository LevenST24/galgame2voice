"""
Configuration module for galgame2voice.
Provides type-safe environment and runtime settings via Pydantic V2.
"""

from functools import lru_cache
from pathlib import Path
from typing import List, Optional
from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings with defaults and environment variable overrides."""

    # Application Metadata
    app_name: str = Field(
        default="galgame2voice",
        validation_alias=AliasChoices("GALGAME2VOICE_APP_NAME", "APP_NAME"),
        description="Application Name",
    )
    app_version: str = Field(
        default="2.0.0",
        validation_alias=AliasChoices("GALGAME2VOICE_APP_VERSION", "APP_VERSION"),
        description="Application SemVer Version",
    )
    debug: bool = Field(
        default=False,
        validation_alias=AliasChoices("GALGAME2VOICE_DEBUG", "DEBUG"),
        description="Enable debug mode and reload",
    )

    # Server Network Binding
    host: str = Field(
        default="127.0.0.1",
        validation_alias=AliasChoices("GALGAME2VOICE_HOST", "HOST", "GALGAME_HOST"),
        description="Server listening host",
    )
    port: int = Field(
        default=8080,
        validation_alias=AliasChoices("GALGAME2VOICE_PORT", "PORT", "GALGAME_PORT"),
        description="Server listening port",
    )

    # Logging
    log_level: str = Field(
        default="INFO",
        validation_alias=AliasChoices("GALGAME2VOICE_LOG_LEVEL", "LOG_LEVEL"),
        description="Logging level",
    )
    log_to_file: bool = Field(
        default=True,
        validation_alias=AliasChoices("GALGAME2VOICE_LOG_TO_FILE", "LOG_TO_FILE"),
        description="Enable rotating file logging",
    )

    # Security & Auth
    console_token: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("GALGAME2VOICE_CONSOLE_TOKEN", "CONSOLE_TOKEN"),
        description="Console access token override (GALGAME2VOICE_CONSOLE_TOKEN); DB token used when unset",
    )
    enable_docs: bool = Field(
        default=False,
        validation_alias=AliasChoices("GALGAME2VOICE_ENABLE_DOCS", "ENABLE_DOCS"),
        description="Expose /docs and /redoc (disable in production)",
    )
    auth_disabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("GALGAME2VOICE_AUTH_DISABLED", "AUTH_DISABLED"),
        description=(
            "Disable console token auth for local zero-config desktop use (default True). "
            "Container/production environments require authentication enabled "
            "(enforced via GALGAME2VOICE_AUTH_DISABLED=0 in docker-compose.yml)."
        ),
    )
    rate_limit_disabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("GALGAME2VOICE_RATE_LIMIT_DISABLED", "RATE_LIMIT_DISABLED"),
        description="Disable request rate limiting (tests only)",
    )

    # Privacy Mode (Directive 10)
    privacy_mode: bool = Field(
        default=False,
        validation_alias=AliasChoices("GALGAME2VOICE_PRIVACY_MODE", "PRIVACY_MODE"),
        description="Privacy mode: bypass persistent TTS disk caching and purge related audio upon session deletion",
    )

    # Project Root & Directory Paths
    # Project root defaults to the parent directory of the inner package
    project_root: Path = Field(
        default_factory=lambda: Path(__file__).resolve().parent.parent,
        validation_alias=AliasChoices("GALGAME2VOICE_PROJECT_ROOT", "PROJECT_ROOT"),
    )
    data_dir_name: str = "data"
    audio_dir_name: str = "audio"
    logs_dir_name: str = "logs"
    characters_dir_name: str = "characters"
    static_dir_name: str = "galgame2voice/static"

    # GPT-SoVITS Integration
    gpt_sovits_base_url: str = Field(
        default="http://127.0.0.1:9880",
        validation_alias=AliasChoices("GALGAME2VOICE_GPT_SOVITS_BASE_URL", "GPT_SOVITS_BASE_URL"),
        description="GPT-SoVITS API server URL",
    )

    # CORS Security Configuration
    cors_origins: List[str] = Field(
        default=["http://127.0.0.1:8080", "http://localhost:8080"],
        validation_alias=AliasChoices("GALGAME2VOICE_CORS_ORIGINS", "CORS_ORIGINS"),
        description="Allowed origins for CORS middleware",
    )
    cors_allow_credentials: bool = Field(
        default=True,
        validation_alias=AliasChoices("GALGAME2VOICE_CORS_ALLOW_CREDENTIALS", "CORS_ALLOW_CREDENTIALS"),
        description="Allow credentials in CORS requests",
    )
    cors_allow_methods: List[str] = Field(
        default=["*"], description="Allowed HTTP methods"
    )
    cors_allow_headers: List[str] = Field(
        default=["*"], description="Allowed HTTP headers"
    )

    # Audio Retention & Cleanup
    audio_retention_minutes: int = Field(
        default=30,
        validation_alias=AliasChoices("GALGAME2VOICE_AUDIO_RETENTION_MINUTES", "AUDIO_RETENTION_MINUTES"),
        description="Retention duration for generated audio files",
    )
    audio_cleanup_interval_seconds: int = Field(
        default=600,
        validation_alias=AliasChoices("GALGAME2VOICE_AUDIO_CLEANUP_INTERVAL_SECONDS", "AUDIO_CLEANUP_INTERVAL_SECONDS"),
        description="Interval between audio cleanup runs",
    )

    # Telegram Bot Configuration
    telegram_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("GALGAME2VOICE_TELEGRAM_ENABLED", "TELEGRAM_ENABLED"),
        description="Enable Telegram bot service",
    )
    telegram_token: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("GALGAME2VOICE_TELEGRAM_TOKEN", "TELEGRAM_TOKEN"),
        description="Telegram Bot API token",
    )
    telegram_proxy: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("GALGAME2VOICE_TELEGRAM_PROXY", "TELEGRAM_PROXY"),
        description="HTTP/SOCKS5 proxy URL for Telegram bot",
    )

    # Pydantic Settings Config
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    @property
    def data_dir(self) -> Path:
        """Resolved absolute path to persistent data directory."""
        return self.project_root / self.data_dir_name

    @property
    def audio_dir(self) -> Path:
        """Resolved absolute path to audio output directory."""
        return self.project_root / self.audio_dir_name

    @property
    def logs_dir(self) -> Path:
        """Resolved absolute path to log files directory."""
        return self.project_root / self.logs_dir_name

    @property
    def db_path(self) -> Path:
        """Resolved absolute path to the SQLite database file."""
        return self.data_dir / "galgame2voice.db"

    @property
    def static_dir(self) -> Path:
        """Resolved absolute path to frontend static assets directory."""
        return self.project_root / self.static_dir_name

    @property
    def characters_dir(self) -> Path:
        """Resolved absolute path to characters directory."""
        return self.project_root / self.characters_dir_name


@lru_cache()
def get_settings() -> Settings:
    """Returns a cached singleton instance of Settings."""
    return Settings()
