"""
Configuration module for galgame2voice.
Provides type-safe environment and runtime settings via Pydantic V2.
"""

import codecs
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from galgame2voice import __version__
from galgame2voice.runtime_paths import get_install_root

# Imported after the main pydantic lines above so a missing or outdated package
# is still reported by its own name (see the launcher's dependency check).
from pydantic import field_validator
from pydantic_settings import NoDecode

_DEFAULT_ENV_FILE = object()


def detect_env_file_encoding(path: Path) -> str:
    """The encoding a Windows editor most likely saved .env in.

    PowerShell 5.1's `>` writes UTF-16, and Notepad on Chinese Windows may save
    as ANSI (GBK). Reading those as UTF-8 crashed startup.
    """
    try:
        raw = path.read_bytes()
    except OSError:
        return "utf-8"
    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return "utf-16"
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        return encoding
    return "latin-1"


class Settings(BaseSettings):
    """Application settings with defaults and environment variable overrides."""

    # Application Metadata
    app_name: str = Field(
        default="galgame2voice",
        validation_alias=AliasChoices("GALGAME2VOICE_APP_NAME", "APP_NAME"),
        description="Application Name",
    )
    app_version: str = Field(
        default=__version__,
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
    console_token: str | None = Field(
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
    host_header_validation_disabled: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "GALGAME2VOICE_HOST_HEADER_VALIDATION_DISABLED",
            "HOST_HEADER_VALIDATION_DISABLED",
        ),
        description=(
            "Disable the /api Host-header allowlist that blocks DNS-rebinding attacks "
            "in local zero-config mode (tests only; never enable in a browser-facing deployment)"
        ),
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
        default_factory=get_install_root,
        validation_alias=AliasChoices("GALGAME2VOICE_PROJECT_ROOT", "PROJECT_ROOT"),
    )
    data_dir_name: str = "data"
    audio_dir_name: str = "audio"
    logs_dir_name: str = "logs"
    characters_dir_name: str = "characters"
    backgrounds_dir_name: str = "backgrounds"
    static_dir_name: str = "galgame2voice/static"

    # GPT-SoVITS Integration
    gpt_sovits_base_url: str = Field(
        default="http://127.0.0.1:9880",
        validation_alias=AliasChoices("GALGAME2VOICE_GPT_SOVITS_BASE_URL", "GPT_SOVITS_BASE_URL"),
        description="GPT-SoVITS API server URL",
    )

    # CORS Security Configuration
    # NoDecode: an env value is split by _split_list_setting instead of being
    # required to be JSON.
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default=["http://127.0.0.1:8080", "http://localhost:8080"],
        validation_alias=AliasChoices("GALGAME2VOICE_CORS_ORIGINS", "CORS_ORIGINS"),
        description="Allowed origins for CORS middleware",
    )
    cors_allow_credentials: bool = Field(
        default=True,
        validation_alias=AliasChoices("GALGAME2VOICE_CORS_ALLOW_CREDENTIALS", "CORS_ALLOW_CREDENTIALS"),
        description="Allow credentials in CORS requests",
    )
    cors_allow_methods: Annotated[list[str], NoDecode] = Field(
        default=["*"], description="Allowed HTTP methods"
    )
    cors_allow_headers: Annotated[list[str], NoDecode] = Field(
        default=["*"], description="Allowed HTTP headers"
    )

    @field_validator("cors_origins", "cors_allow_methods", "cors_allow_headers", mode="before")
    @classmethod
    def _split_list_setting(cls, value):
        """Accept a comma-separated list as well as JSON.

        GALGAME2VOICE_CORS_ORIGINS=http://192.168.1.5:8080,http://localhost:8080 is how
        most people write it, but pydantic-settings wanted a JSON array and stopped
        startup with a SettingsError for anything else.
        """
        if not isinstance(value, str):
            return value
        text = value.strip()
        if text.startswith("["):
            import json

            try:
                items = json.loads(text)
            except ValueError as exc:
                raise ValueError(f"无法解析列表：{text}") from exc
        else:
            items = text.split(",")
        return [
            cleaned
            for item in items
            if (cleaned := item.strip().rstrip("/") if isinstance(item, str) else item) != ""
        ]

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

    # Pydantic Settings Config
    # Default .env is resolved against get_install_root() in __init__ so
    # BaseSettings never pre-reads a .env from the current working directory.
    model_config = SettingsConfigDict(
        env_file=None,
        env_file_encoding="utf-8",
        extra="ignore",
        # GALGAME2VOICE_PORT= left empty in .env means "not set", as auth.py already
        # treats an empty GALGAME2VOICE_AUTH_DISABLED; it used to stop startup.
        env_ignore_empty=True,
        case_sensitive=False,
    )

    def __init__(
        self,
        *,
        _env_file: Any = _DEFAULT_ENV_FILE,
        _env_file_encoding: str | None = None,
        **values: Any,
    ) -> None:
        if _env_file is _DEFAULT_ENV_FILE:
            candidate = get_install_root() / ".env"
            _env_file = candidate if candidate.is_file() else None
        if _env_file_encoding is None and isinstance(_env_file, (str, Path)):
            path = Path(_env_file).expanduser()
            if path.is_file():
                _env_file_encoding = detect_env_file_encoding(path)
        super().__init__(_env_file=_env_file, _env_file_encoding=_env_file_encoding, **values)

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

    @property
    def backgrounds_dir(self) -> Path:
        """Resolved absolute path to the local background library."""
        return self.project_root / self.backgrounds_dir_name


_SETTING_ERROR_REASONS = {
    "int_parsing": "不是有效的整数",
    "int_from_float": "不是有效的整数",
    "float_parsing": "不是有效的数字",
    "bool_parsing": "不是有效的开关值，请填 true 或 false",
    "list_type": '不是有效的列表，请填 JSON 数组（例如 ["http://localhost:3000"]）',
}


class InvalidSettingsError(ValueError):
    """A setting from the environment or .env cannot be used."""


def _describe_invalid_settings(exc: Exception) -> str:
    """Lists each bad setting from a pydantic ValidationError."""
    lines = ["galgame2voice 无法启动：以下配置项的值无效（来自环境变量，或程序目录下的 .env 文件）："]
    aliases: dict[str, str] = {}
    for k, f in Settings.model_fields.items():
        names = [str(c) for c in f.validation_alias.choices] if isinstance(f.validation_alias, AliasChoices) else [k.upper()]
        aliases.update(dict.fromkeys((k, *names), " / ".join(names)))
    errs = exc.errors() if hasattr(exc, "errors") else [{"loc": (next((k for k in Settings.model_fields if f'"{k}"' in str(exc)), str(exc)),), "type": "list_type" if hasattr(exc.__cause__, "doc") else "", "input": getattr(exc.__cause__, "doc", ""), "msg": str(exc)}]
    for err in errs:
        name = ".".join(str(part) for part in err.get("loc", ())) or "?"
        reason = _SETTING_ERROR_REASONS.get(err.get("type", ""), err.get("msg", "格式不对"))
        lines.append(f"- {aliases.get(name, name)} = {str(err.get('input', ''))[:80]!r}：{reason}")
    lines.append("请改正或删除这些配置项后重新启动。")
    return "\n".join(lines)


@lru_cache()
def get_settings() -> Settings:
    """Returns a cached singleton instance of Settings."""
    from pydantic import ValidationError
    from pydantic_settings import SettingsError

    try:
        return Settings()
    except (ValidationError, SettingsError) as exc:
        raise InvalidSettingsError(_describe_invalid_settings(exc)) from None


def require_settings() -> Settings:
    """Returns settings or exits with status 1 with the formatted Chinese message."""
    try:
        return get_settings()
    except InvalidSettingsError as exc:
        raise SystemExit(str(exc)) from None
