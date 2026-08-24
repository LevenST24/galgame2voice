"""
Configuration module for galgame2voice.
Provides type-safe environment and runtime settings via Pydantic V2.
"""

from functools import lru_cache
from pathlib import Path
from typing import List, Optional
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings with defaults and environment variable overrides."""

    # Application Metadata
    app_name: str = Field(default="galgame2voice", description="Application Name")
    app_version: str = Field(default="2.0.0", description="Application SemVer Version")
    debug: bool = Field(default=False, description="Enable debug mode and reload")

    # Server Network Binding
    host: str = Field(default="127.0.0.1", description="Server listening host")
    port: int = Field(default=8080, description="Server listening port")

    # Logging
    log_level: str = Field(default="INFO", description="Logging level")
    log_to_file: bool = Field(default=True, description="Enable rotating file logging")

    # Project Root & Directory Paths
    # Project root defaults to the parent directory of the inner package
    project_root: Path = Field(
        default_factory=lambda: Path(__file__).resolve().parent.parent
    )
    data_dir_name: str = "data"
    audio_dir_name: str = "audio"
    logs_dir_name: str = "logs"
    static_dir_name: str = "galgame2voice/static"

    # GPT-SoVITS Integration
    gpt_sovits_base_url: str = Field(
        default="http://127.0.0.1:9880", description="GPT-SoVITS API server URL"
    )

    # CORS Security Configuration
    cors_origins: List[str] = Field(
        default=["*"], description="Allowed origins for CORS middleware"
    )
    cors_allow_credentials: bool = Field(
        default=True, description="Allow credentials in CORS requests"
    )
    cors_allow_methods: List[str] = Field(
        default=["*"], description="Allowed HTTP methods"
    )
    cors_allow_headers: List[str] = Field(
        default=["*"], description="Allowed HTTP headers"
    )

    # Audio Retention & Cleanup
    audio_retention_minutes: int = Field(
        default=30, description="Retention duration for generated audio files"
    )
    audio_cleanup_interval_seconds: int = Field(
        default=600, description="Interval between audio cleanup runs"
    )

    # Telegram Bot Configuration
    telegram_enabled: bool = Field(
        default=False, description="Enable Telegram bot service"
    )
    telegram_token: Optional[str] = Field(
        default=None, description="Telegram Bot API token"
    )
    telegram_proxy: Optional[str] = Field(
        default=None, description="HTTP/SOCKS5 proxy URL for Telegram bot"
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


@lru_cache()
def get_settings() -> Settings:
    """Returns a cached singleton instance of Settings."""
    return Settings()
