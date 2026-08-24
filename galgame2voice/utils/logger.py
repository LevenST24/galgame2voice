"""
Security-hardened logging module for galgame2voice.
Provides zero-leakage masking filter and structured logging initialization.
"""

import logging
import logging.handlers
import re
import sys
from pathlib import Path
from typing import Optional


class MaskingFilter(logging.Filter):
    """
    Logging filter that sanitizes log records, masking API keys,
    bearer tokens, Telegram bot tokens, passwords, and sensitive fields.
    """

    # Compiled patterns for high-performance string sanitization
    PATTERNS = [
        # OpenAI / generic API keys with length > 8
        (
            re.compile(r"\b(sk-[a-zA-Z0-9_\-]{3})[a-zA-Z0-9_\-]{5,}([a-zA-Z0-9_\-]{4})\b"),
            r"\1****\2",
        ),
        # Generic sk- tokens without visible prefix
        (
            re.compile(r"\b(sk-[a-zA-Z0-9_\-]{8,})\b"),
            r"sk-****[MASKED]",
        ),
        # Bearer Authorization headers / tokens
        (
            re.compile(r"(Bearer\s+)[a-zA-Z0-9_\-\.]{8,}", re.IGNORECASE),
            r"\1[MASKED_TOKEN]",
        ),
        # Telegram bot tokens: 123456789:ABCDefgh-12345... -> 123456789:[MASKED_TELEGRAM_TOKEN]
        (
            re.compile(r"\b(\d{8,10}:)[a-zA-Z0-9_\-]{30,}\b"),
            r"\1[MASKED_TELEGRAM_TOKEN]",
        ),
        # JSON / Query params with secret keys: "api_key": "...", "token": "..."
        (
            re.compile(
                r'((?:api[_-]?key|token|password|secret|access[_-]?token)["\']?\s*[:=]\s*["\'])([^"\']{4,})(["\'])',
                re.IGNORECASE,
            ),
            r"\1****\3",
        ),
        # URL Query parameters containing sensitive data
        (
            re.compile(
                r"([?&](?:key|api_key|token|secret|password)=)[^&]+",
                re.IGNORECASE,
            ),
            r"\1[MASKED]",
        ),
    ]

    @classmethod
    def sanitize(cls, text: str) -> str:
        """Sanitizes text by replacing all known secret patterns with masked values."""
        if not text:
            return text
        result = text
        for pattern, replacement in cls.PATTERNS:
            result = pattern.sub(replacement, result)
        return result

    def filter(self, record: logging.LogRecord) -> bool:
        """Applies sanitization to the log record message and arguments."""
        if isinstance(record.msg, str):
            record.msg = self.sanitize(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: self.sanitize(str(v)) for k, v in record.args.items()}
            elif isinstance(record.args, tuple):
                record.args = tuple(
                    self.sanitize(str(arg)) if isinstance(arg, str) else arg
                    for arg in record.args
                )
        return True


def setup_logger(
    log_level: str = "INFO",
    logs_dir: Optional[Path] = None,
    log_to_file: bool = True,
) -> logging.Logger:
    """
    Configures and initializes the root logger and Uvicorn loggers with
    the zero-leakage MaskingFilter and structured formatters.
    """
    level = getattr(logging, log_level.upper(), logging.INFO)
    masking_filter = MaskingFilter()

    # Formatter: [Timestamp] [LEVEL] [logger_name]: message
    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] [%(name)s]: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Root Logger Configuration
    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # Clear existing handlers to prevent duplicate output
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    # Console Handler (sys.stdout)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)
    console_handler.addFilter(masking_filter)
    root_logger.addHandler(console_handler)

    # File Handler (Optional rotating file in logs/)
    if log_to_file and logs_dir:
        logs_dir = Path(logs_dir)
        logs_dir.mkdir(parents=True, exist_ok=True)
        log_file = logs_dir / "galgame2voice.log"
        file_handler = logging.handlers.RotatingFileHandler(
            filename=str(log_file),
            maxBytes=10 * 1024 * 1024,  # 10 MB per file
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setLevel(level)
        file_handler.setFormatter(formatter)
        file_handler.addFilter(masking_filter)
        root_logger.addHandler(file_handler)

    # Apply MaskingFilter to Uvicorn Loggers
    for uvicorn_logger_name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        uv_logger = logging.getLogger(uvicorn_logger_name)
        uv_logger.addFilter(masking_filter)
        for h in uv_logger.handlers:
            h.addFilter(masking_filter)

    logger = logging.getLogger("galgame2voice")
    logger.info("Logging initialized at level %s with zero-leakage security filter", log_level.upper())
    return logger
