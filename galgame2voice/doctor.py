"""
``galgame2voice doctor`` — one-shot environment diagnosis.

Why this exists
---------------
The README happy path is short, but the real runtime contract spans the Python
interpreter, four writable directories, a SQLite file, ffmpeg, an optional
GPT-SoVITS server, an optional LLM/STT provider and an optional Telegram bot.
When one of those is missing or misconfigured the first symptom is usually a
stack trace deep inside a chat request — the most expensive place to learn about
it. ``doctor`` reports the same facts up front and puts the fix next to each
finding.

Statuses
--------
``OK``    works
``WARN``  works, but something is missing or worth knowing about
``FAIL``  blocks a feature or prevents startup
``SKIP``  not applicable / not enabled

Exit code is 0 when nothing FAILed and 1 otherwise, so it can gate a script.

Note: like application startup, ``doctor`` creates any missing data/audio/logs
/characters directories and writes a short-lived probe file to confirm they are
writable. It never modifies the database.
"""

import argparse
import dataclasses
import ipaddress
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from galgame2voice.config import Settings, get_settings
from galgame2voice.database.session import get_database_path
from galgame2voice.security.auth import is_auth_disabled
from galgame2voice.utils.audio_converter import find_ffmpeg
from galgame2voice.utils.http_client import PROXY_ENV_HINT, create_async_client, proxy_environment_is_usable

OK = "OK"
WARN = "WARN"
FAIL = "FAIL"
SKIP = "SKIP"

HEALTHY = "HEALTHY"
DEGRADED = "DEGRADED"
FAILED = "FAILED"

REQUIRED_PYTHON = (3, 10)
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "0:0:0:0:0:0:0:1"}
_SOVITS_PROBE_TIMEOUT_SECONDS = 3.0


@dataclasses.dataclass(frozen=True)
class Check:
    """A single diagnostic finding."""

    name: str
    status: str
    detail: str
    hint: str = ""


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def summarize(checks: Sequence[Check]) -> str:
    """Collapses findings into HEALTHY / DEGRADED / FAILED."""
    statuses = {check.status for check in checks}
    if FAIL in statuses:
        return FAILED
    if WARN in statuses:
        return DEGRADED
    return HEALTHY


def exit_code(checks: Sequence[Check]) -> int:
    """1 when anything FAILed, else 0."""
    return 1 if any(check.status == FAIL for check in checks) else 0


def render(checks: Sequence[Check]) -> str:
    """Human-readable report, one line per finding."""
    lines = ["galgame2voice doctor", ""]
    width = max((len(check.status) for check in checks), default=len(OK))
    for check in checks:
        lines.append(f"[{check.status.ljust(width)}] {check.name}: {check.detail}")
        if check.hint:
            lines.append(f"{' ' * (width + 3)}-> {check.hint}")
    lines.append("")
    lines.append(f"Overall: {summarize(checks)}")
    return "\n".join(lines)


def as_dicts(checks: Sequence[Check]) -> List[Dict[str, Any]]:
    """Machine-readable findings (for support tickets and scripts)."""
    return [dataclasses.asdict(check) for check in checks]


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def check_runtime() -> List[Check]:
    version = ".".join(str(part) for part in sys.version_info[:3])
    if sys.version_info[:2] >= REQUIRED_PYTHON:
        return [Check("Python", OK, f"{version} (requires >= {'.'.join(map(str, REQUIRED_PYTHON))})")]
    return [
        Check(
            "Python",
            FAIL,
            f"{version} is too old (requires >= {'.'.join(map(str, REQUIRED_PYTHON))})",
            "Install a newer Python and recreate the virtual environment.",
        )
    ]


def _directory_check(label: str, path: Path) -> Check:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return Check(
            f"{label} directory",
            FAIL,
            f"{path} cannot be created ({exc.__class__.__name__}: {exc})",
            "Fix the path permissions or point GALGAME2VOICE_DATA_DIR at a writable location.",
        )

    probe = path / f".doctor_probe_{os.getpid()}"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return Check(
            f"{label} directory",
            FAIL,
            f"{path} is not writable ({exc.__class__.__name__}: {exc})",
            "Grant write access to this directory.",
        )
    return Check(f"{label} directory", OK, f"writable: {path}")


def check_directories(settings: Settings) -> List[Check]:
    return [
        _directory_check("Data", settings.data_dir),
        _directory_check("Audio", settings.audio_dir),
        _directory_check("Logs", settings.logs_dir),
        _directory_check("Characters", settings.characters_dir),
    ]


def check_database(settings: Settings) -> List[Check]:
    # Report the database the app actually opens: session.get_database_path()
    # honours GALGAME2VOICE_DB_PATH, which settings.db_path does not.
    db_path = Path(get_database_path())
    if not db_path.exists():
        return [
            Check(
                "Database",
                WARN,
                f"not created yet: {db_path}",
                "This is normal before the first start; the schema is created automatically.",
            )
        ]

    try:
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        return [
            Check(
                "Database",
                FAIL,
                f"{db_path} cannot be opened read-only ({exc})",
                "Check that the file is a valid SQLite database and is readable.",
            )
        ]

    try:
        journal_mode = connection.execute("PRAGMA journal_mode;").fetchone()
        mode = str(journal_mode[0]).upper() if journal_mode else "UNKNOWN"
        tables = connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table';"
        ).fetchone()
        table_count = int(tables[0]) if tables else 0
    except sqlite3.Error as exc:
        return [
            Check(
                "Database",
                FAIL,
                f"{db_path} is not a usable database ({exc})",
                "Move the file aside and let the app recreate it, or restore a backup from data/backups.",
            )
        ]
    finally:
        connection.close()

    if mode != "WAL":
        return [
            Check(
                "Database",
                WARN,
                f"{db_path} is readable ({table_count} tables) but journal_mode={mode}",
                "WAL is enabled automatically on the next start.",
            )
        ]
    return [Check("Database", OK, f"readable, WAL enabled ({table_count} tables): {db_path}")]


def check_ffmpeg() -> List[Check]:
    executable = find_ffmpeg()
    if not executable:
        return [
            Check(
                "ffmpeg",
                WARN,
                "not found on PATH",
                "Install ffmpeg (apt install ffmpeg / winget install ffmpeg) — OGG voice-note "
                "conversion is unavailable without it. Set FFMPEG_PATH to override discovery.",
            )
        ]
    return [Check("ffmpeg", OK, executable)]


def check_bind_and_auth(settings: Settings) -> List[Check]:
    host = str(settings.host).strip().lower()
    loopback = host in _LOOPBACK_HOSTS
    if not loopback:
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = False

    auth_off = is_auth_disabled()
    bind = f"{settings.host}:{settings.port}"

    if loopback and auth_off:
        return [
            Check(
                "Console auth",
                OK,
                f"disabled, listening on loopback {bind} (local zero-config mode)",
            )
        ]
    if auth_off:
        return [
            Check(
                "Console auth",
                FAIL,
                f"disabled while listening on non-loopback {bind}",
                "The service refuses to start in this state. Set GALGAME2VOICE_AUTH_DISABLED=0, "
                "or bind to 127.0.0.1 again.",
            )
        ]
    return [Check("Console auth", OK, f"enabled, listening on {bind}")]


def check_proxy_environment() -> List[Check]:
    if proxy_environment_is_usable():
        return [Check("Proxy environment", OK, "HTTP_PROXY/NO_PROXY are usable")]
    return [
        Check(
            "Proxy environment",
            WARN,
            "cannot be parsed; HTTP clients fall back to ignoring it",
            PROXY_ENV_HINT,
        )
    ]


def check_characters(settings: Settings) -> List[Check]:
    root = settings.characters_dir
    if not root.is_dir():
        return [
            Check(
                "Character packages",
                WARN,
                f"directory missing: {root}",
                "Add character folders containing manifest.json, or import a package in the console.",
            )
        ]
    packages = sorted(child.name for child in root.iterdir() if (child / "manifest.json").is_file())
    if not packages:
        return [
            Check(
                "Character packages",
                WARN,
                f"none found under {root}",
                "Each character needs its own folder with manifest.json plus the GPT/SoVITS weights.",
            )
        ]
    return [Check("Character packages", OK, f"{len(packages)} discovered: {', '.join(packages)}")]


def check_providers(providers: Optional[List[Any]]) -> List[Check]:
    if providers is None:
        return [Check("LLM providers", SKIP, "database not readable yet")]
    configured = [getattr(provider, "id", "?") for provider in providers]
    if not configured:
        return [
            Check(
                "LLM providers",
                WARN,
                "none configured",
                "Add an LLM provider in the console (Settings -> Providers); chat needs one.",
            )
        ]
    return [Check("LLM providers", OK, f"{len(configured)} configured: {', '.join(str(c) for c in configured)}")]


def _parse_admin_ids(raw: str) -> List[str]:
    return [item.strip() for item in (raw or "").split(",") if item.strip()]


def check_telegram(settings: Settings, db_enabled: Optional[bool], admin_ids: str = "") -> List[Check]:
    enabled = bool(db_enabled) if db_enabled is not None else bool(settings.telegram_enabled)
    if not enabled:
        return [Check("Telegram", SKIP, "disabled")]

    findings: List[Check] = []
    token = (settings.telegram_token or "").strip()
    if not token:
        findings.append(
            Check(
                "Telegram",
                FAIL,
                "enabled but no bot token configured",
                "Set GALGAME2VOICE_TELEGRAM_TOKEN or store the token in the console.",
            )
        )
    else:
        findings.append(Check("Telegram", OK, "bot token configured"))

    # Whitelist lives in the console/DB, with TELEGRAM_ADMIN_IDS as an override.
    effective_admins = _parse_admin_ids(admin_ids) or _parse_admin_ids(
        os.getenv("TELEGRAM_ADMIN_IDS", "") or os.getenv("GALGAME2VOICE_TELEGRAM_ADMIN_IDS", "")
    )
    if not effective_admins:
        findings.append(
            Check(
                "Telegram admins",
                WARN,
                "whitelist is empty; every user is rejected (fail-closed)",
                "Set the admin user id in the console or via TELEGRAM_ADMIN_IDS.",
            )
        )
    else:
        findings.append(Check("Telegram admins", OK, f"{len(effective_admins)} allowed user id(s)"))
    return findings


async def check_gpt_sovits(base_url: str, *, offline: bool = False) -> List[Check]:
    target = (base_url or "").strip().rstrip("/")
    if offline:
        return [Check("GPT-SoVITS", SKIP, f"probe skipped (--offline); configured at {target or 'unset'}")]
    if not target:
        return [
            Check(
                "GPT-SoVITS",
                FAIL,
                "no base URL configured",
                "Set GALGAME2VOICE_GPT_SOVITS_BASE_URL (default http://127.0.0.1:9880).",
            )
        ]
    if not target.startswith(("http://", "https://")):
        return [
            Check(
                "GPT-SoVITS",
                FAIL,
                f"invalid base URL: {target}",
                "The URL must start with http:// or https://.",
            )
        ]

    import httpx

    try:
        async with create_async_client(trust_env=False, timeout=_SOVITS_PROBE_TIMEOUT_SECONDS) as client:
            try:
                response = await client.get(f"{target}/")
            except httpx.HTTPError:
                response = None
    except Exception:  # pragma: no cover - defensive
        return [Check("GPT-SoVITS", WARN, "probe could not be constructed")]

    if response is not None and response.status_code in (200, 400):
        return [Check("GPT-SoVITS", OK, f"reachable at {target} (HTTP {response.status_code})")]
    return [
        Check(
            "GPT-SoVITS",
            WARN,
            f"unreachable at {target}",
            "Start the GPT-SoVITS API server, or update GALGAME2VOICE_GPT_SOVITS_BASE_URL / the "
            "URL stored in the console.",
        )
    ]


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


async def _load_database_state(settings: Settings) -> Tuple[Optional[bool], Optional[List[Any]], str]:
    """Reads telegram_enabled, providers and the admin whitelist from the DB.

    Returns None for the first two when the database is unavailable.
    """
    if not Path(get_database_path()).exists():
        return None, None, ""
    try:
        from galgame2voice.database import crud
        from galgame2voice.database.session import get_db

        async with get_db(get_database_path()) as connection:
            row = await crud.get_settings_raw(connection)
            providers = await crud.list_providers(connection)
        return (
            bool(getattr(row, "telegram_enabled", False)),
            list(providers),
            str(getattr(row, "telegram_admin_ids", "") or ""),
        )
    except Exception:
        return None, None, ""


async def collect_checks(settings: Optional[Settings] = None, *, offline: bool = False) -> List[Check]:
    """Runs every check and returns the findings in report order."""
    settings = settings or get_settings()
    telegram_enabled, providers, admin_ids = await _load_database_state(settings)

    checks: List[Check] = []
    checks.extend(check_runtime())
    checks.extend(check_directories(settings))
    checks.extend(check_database(settings))
    checks.extend(check_ffmpeg())
    checks.extend(check_bind_and_auth(settings))
    checks.extend(check_proxy_environment())
    checks.extend(check_characters(settings))
    checks.extend(check_providers(providers))
    checks.extend(check_telegram(settings, telegram_enabled, admin_ids))
    checks.extend(await check_gpt_sovits(str(getattr(settings, "gpt_sovits_base_url", "") or ""), offline=offline))
    return checks


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="galgame2voice doctor",
        description="Diagnose the local galgame2voice environment.",
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument("--offline", action="store_true", help="skip network probes")
    return parser


def _configure_stdout() -> None:
    """Keeps the report printable on legacy consoles.

    Character folder names are user data and may contain characters the console
    codepage cannot represent (a cp936 console given Japanese kana, for example).
    Replacing those is far better than crashing the diagnostic before it reports
    anything. The console's own encoding is kept.
    """
    stream = sys.stdout
    if hasattr(stream, "reconfigure"):
        try:
            stream.reconfigure(errors="replace")
        except (ValueError, OSError):
            pass


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point. Returns the process exit code."""
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    _configure_stdout()

    import asyncio

    checks = asyncio.run(collect_checks(offline=args.offline))

    if args.json:
        # ensure_ascii keeps --json valid on any console encoding; JSON consumers
        # decode the escapes transparently.
        print(json.dumps({"overall": summarize(checks), "checks": as_dicts(checks)}, indent=2))
    else:
        print(render(checks))
    return exit_code(checks)


__all__ = [
    "Check",
    "OK",
    "WARN",
    "FAIL",
    "SKIP",
    "HEALTHY",
    "DEGRADED",
    "FAILED",
    "summarize",
    "exit_code",
    "render",
    "as_dicts",
    "collect_checks",
    "main",
]
