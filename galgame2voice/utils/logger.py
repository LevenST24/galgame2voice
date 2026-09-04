"""
Security-hardened logging module for galgame2voice.
Provides zero-leakage masking filter, masking formatter, and traceback sanitization.
"""

import logging
import logging.handlers
import re
import sys
from pathlib import Path
from typing import Optional, Union


class MaskingFilter(logging.Filter):
    """
    Logging filter that sanitizes log records, masking API keys,
    bearer tokens, Telegram bot tokens, passwords, and sensitive fields.
    """

    # Compiled patterns for high-performance string sanitization (linear O(N), ReDoS-safe)
    PATTERNS = [
        # 1. OpenAI / generic API keys with visible prefix & suffix
        (
            re.compile(r"\b(sk-[a-zA-Z0-9_\-]{3})[a-zA-Z0-9_\-]{5,}([a-zA-Z0-9_\-]{4})\b"),
            r"\1****\2",
        ),
        # 2. Generic sk- tokens fallback
        (
            re.compile(r"\b(sk-[a-zA-Z0-9_\-]{8,})\b"),
            r"sk-****[MASKED]",
        ),
        # 3. Google Gemini / Cloud API keys (AIzaSy...)
        (
            re.compile(r"\b(AIzaSy[a-zA-Z0-9_\-]{4})[a-zA-Z0-9_\-]{25,}([a-zA-Z0-9_\-]{4})\b"),
            r"\1****\2",
        ),
        # 4. Google API keys fallback
        (
            re.compile(r"\b(AIzaSy[a-zA-Z0-9_\-]{8,})\b"),
            r"AIzaSy****[MASKED]",
        ),
        # 5. HuggingFace tokens (hf_...)
        (
            re.compile(r"\b(hf_[a-zA-Z0-9]{3})[a-zA-Z0-9]{15,}([a-zA-Z0-9]{4})\b"),
            r"\1****\2",
        ),
        # 6. HuggingFace fallback
        (
            re.compile(r"\b(hf_[a-zA-Z0-9]{8,})\b"),
            r"hf_****[MASKED]",
        ),
        # 7. Bearer Authorization headers / tokens
        (
            re.compile(r"(Bearer\s+)[a-zA-Z0-9_\-\.]{8,}", re.IGNORECASE),
            r"\1[MASKED_TOKEN]",
        ),
        # 8. Telegram bot tokens in URLs: https://api.telegram.org/bot<token>...
        (
            re.compile(r"(https?://api\.telegram\.org/bot)(\d{6,12}:)?[a-zA-Z0-9_\-]+", re.IGNORECASE),
            r"\1[MASKED_TELEGRAM_TOKEN]",
        ),
        # 9. Telegram bot tokens standalone: 123456789:ABCDefgh-12345... -> 123456789:[MASKED_TELEGRAM_TOKEN]
        (
            re.compile(r"\b(\d{8,12}:)[a-zA-Z0-9_\-]{20,}\b"),
            r"\1[MASKED_TELEGRAM_TOKEN]",
        ),
        # 10. URL Query parameters containing sensitive data (?api_key=..., &token=...)
        (
            re.compile(
                r"([?&](?:key|api[_-]?key|token|secret|password|access[_-]?token)=)[^&\s#]+",
                re.IGNORECASE,
            ),
            r"\1[MASKED]",
        ),
        # 11. JSON / Quoted key-value pairs: "api_key": "...", "token": "..."
        (
            re.compile(
                r'((?:api[_-]?key|token|password|secret|access[_-]?token|console[_-]?token|auth[_-]?token|client[_-]?secret)["\']?\s*[:=]\s*["\'])([^"\']{4,})(["\'])',
                re.IGNORECASE,
            ),
            r"\1****\3",
        ),
        # 12. Unquoted key-value pairs in logs/text: Console token: 12345678, api_key=12345678
        (
            re.compile(
                r'(\b(?:api[_-]?key|token|password|secret|access[_-]?token|console[_-]?token|auth[_-]?token|client[_-]?secret)\s*[:=]\s*)(?!\*{3,}|\[MASKED|["\'])([^\s,"\'}{\]\[)(;]{4,})',
                re.IGNORECASE,
            ),
            r"\1****",
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


class MaskingFormatter(logging.Formatter):
    """
    Zero-leakage logging formatter that sanitizes the entire formatted message,
    including timestamps, logger names, message bodies, exception tracebacks,
    and stack traces.
    """

    def format(self, record: logging.LogRecord) -> str:
        """
        Formats the record and sanitizes the entire final output string.
        """
        formatted = super().format(record)
        return MaskingFilter.sanitize(formatted)

    def formatException(self, ei) -> str:
        """
        Formats exception traceback and sanitizes all frames and error messages.
        """
        exc_text = super().formatException(ei)
        return MaskingFilter.sanitize(exc_text)

    def formatStack(self, stack_info: str) -> str:
        """
        Formats stack info and sanitizes all frames.
        """
        stack_text = super().formatStack(stack_info)
        return MaskingFilter.sanitize(stack_text)


def sanitize_error_detail(exc_or_msg: Optional[Union[Exception, str]]) -> str:
    """
    Sanitizes an exception or error string before returning to client or logging.
    Guarantees no API keys, tokens, or URL query secrets are disclosed.
    """
    if exc_or_msg is None:
        return ""
    raw_text = str(exc_or_msg)
    return MaskingFilter.sanitize(raw_text)


def setup_logger(
    log_level: str = "INFO",
    logs_dir: Optional[Path] = None,
    log_to_file: bool = True,
) -> logging.Logger:
    """
    Configures and initializes the root logger and Uvicorn loggers with
    the zero-leakage MaskingFilter and MaskingFormatter.
    """
    level = getattr(logging, log_level.upper(), logging.INFO)
    masking_filter = MaskingFilter()

    # Formatter: [Timestamp] [LEVEL] [logger_name]: message (with Zero-Leakage Masking)
    formatter = MaskingFormatter(
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

    # Apply MaskingFilter and MaskingFormatter to Uvicorn Loggers
    for uvicorn_logger_name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        uv_logger = logging.getLogger(uvicorn_logger_name)
        uv_logger.addFilter(masking_filter)
        for h in uv_logger.handlers:
            h.addFilter(masking_filter)
            h.setFormatter(formatter)

    logger = logging.getLogger("galgame2voice")
    logger.info("Logging initialized at level %s with zero-leakage security filter and formatter", log_level.upper())
    return logger
