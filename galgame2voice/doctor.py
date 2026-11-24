"""
``galgame2voice doctor`` — one-shot environment diagnosis.

Why this exists
---------------
The README happy path is short, but the real runtime contract spans the Python
interpreter, four writable directories, a SQLite file, ffmpeg, an optional
GPT-SoVITS server, and an optional LLM/STT provider.
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
import asyncio
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
from galgame2voice.services.sovits_endpoint import resolve_sovits_url
from galgame2voice.utils.audio_converter import find_ffmpeg
from galgame2voice.utils.http_client import create_async_client, proxy_environment_is_usable

OK = "OK"
WARN = "WARN"
FAIL = "FAIL"
SKIP = "SKIP"

HEALTHY = "HEALTHY"
DEGRADED = "DEGRADED"
FAILED = "FAILED"

REQUIRED_PYTHON = (3, 10)
# Report labels; --json keeps the codes above so scripts and issue templates stay stable.
STATUS_LABELS = {OK: "正常", WARN: "注意", FAIL: "错误", SKIP: "跳过"}
OVERALL_LABELS = {HEALTHY: "一切正常", DEGRADED: "能运行，但有需要注意的项", FAILED: "有错误，需要先处理"}

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
    lines = ["Galgame2Voice 环境检查", ""]
    for check in checks:
        lines.append(f"[{STATUS_LABELS.get(check.status, check.status)}] {check.name}：{check.detail}")
        if check.hint:
            lines.append(f"       -> {check.hint}")
    lines.append("")
    overall = summarize(checks)
    lines.append(f"总体结果：{OVERALL_LABELS.get(overall, overall)}（{overall}）")
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
        return [Check("Python", OK, f"{version}（需要 >= {'.'.join(map(str, REQUIRED_PYTHON))}）")]
    return [
        Check(
            "Python",
            FAIL,
            f"{version} 版本太旧（需要 >= {'.'.join(map(str, REQUIRED_PYTHON))}）",
            "请安装更新的 Python，再重新运行 uv sync --frozen 创建运行环境。",
        )
    ]


def _directory_check(label: str, path: Path) -> Check:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return Check(
            f"{label}目录",
            FAIL,
            f"无法创建 {path}（{exc.__class__.__name__}: {exc}）",
            "请把程序文件夹移到可以写入的位置，或用 GALGAME2VOICE_DATA_DIR 指定一个可写的数据目录。",
        )

    probe = path / f".doctor_probe_{os.getpid()}"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return Check(
            f"{label}目录",
            FAIL,
            f"{path} 不能写入（{exc.__class__.__name__}: {exc}）",
            "请给这个文件夹写入权限，或把程序文件夹移到可以写入的位置。",
        )
    return Check(f"{label}目录", OK, f"可写：{path}")


def check_directories(settings: Settings) -> List[Check]:
    return [
        _directory_check("数据", settings.data_dir),
        _directory_check("音频", settings.audio_dir),
        _directory_check("日志", settings.logs_dir),
        _directory_check("角色包", settings.characters_dir),
    ]


def check_database(settings: Settings) -> List[Check]:
    # Report the database the app actually opens: session.get_database_path()
    # honours GALGAME2VOICE_DB_PATH, which settings.db_path does not.
    db_path = Path(get_database_path())
    if not db_path.exists():
        return [
            Check(
                "数据库",
                WARN,
                f"还没有创建：{db_path}",
                "第一次启动前是正常的，启动时会自动创建。",
            )
        ]

    try:
        connection = sqlite3.connect(
            f"{Path(db_path).resolve().as_uri()}?mode=ro", uri=True
        )
    except sqlite3.Error as exc:
        return [
            Check(
                "数据库",
                FAIL,
                f"无法以只读方式打开 {db_path}（{exc}）",
                "请确认这个文件是完整的数据库文件，并且可以读取。",
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
                "数据库",
                FAIL,
                f"{db_path} 不是可用的数据库（{exc}）",
                "请先另存这个文件，再从 data/backups 里的备份恢复（便携版可用「数据恢复.bat」）。",
            )
        ]
    finally:
        connection.close()

    if mode != "WAL":
        return [
            Check(
                "数据库",
                WARN,
                f"{db_path} 可以读取（{table_count} 张表），但 journal_mode={mode}",
                "下次启动时会自动切换到 WAL 模式。",
            )
        ]
    return [Check("数据库", OK, f"可以读取，已启用 WAL（{table_count} 张表）：{db_path}")]


def check_ffmpeg() -> List[Check]:
    executable = find_ffmpeg()
    if not executable:
        return [
            Check(
                "ffmpeg",
                WARN,
                "在 PATH 里没有找到",
                "没有 ffmpeg 时无法把语音转成 OGG 语音消息。请安装 ffmpeg（winget install ffmpeg / "
                "apt install ffmpeg），或用 FFMPEG_PATH 指定 ffmpeg 的位置。",
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
                "访问令牌",
                OK,
                f"未开启，只在本机 {bind} 监听（本机免令牌模式）",
            )
        ]
    if auth_off:
        return [
            Check(
                "访问令牌",
                FAIL,
                f"未开启，却在局域网地址 {bind} 监听",
                "这种状态下服务会拒绝启动。请在 .env 里设置 GALGAME2VOICE_AUTH_DISABLED=0，"
                "或改回只在 127.0.0.1 监听。",
            )
        ]
    return [Check("访问令牌", OK, f"已开启，监听 {bind}")]


def check_proxy_environment() -> List[Check]:
    if proxy_environment_is_usable():
        return [Check("代理环境变量", OK, "HTTP_PROXY/NO_PROXY 可以正常解析")]
    return [
        Check(
            "代理环境变量",
            WARN,
            "无法解析，联网请求会忽略这些设置",
            "请检查 NO_PROXY/HTTP_PROXY/HTTPS_PROXY 里写错的条目，例如带方括号的 IPv6 地址 '[::1]'。",
        )
    ]


def check_characters(settings: Settings) -> List[Check]:
    root = settings.characters_dir
    if not root.is_dir():
        return [
            Check(
                "角色包",
                WARN,
                f"文件夹不存在：{root}",
                "请在网页里导入角色包，或放入带 manifest.json 的角色文件夹。",
            )
        ]
    packages = sorted(child.name for child in root.iterdir() if (child / "manifest.json").is_file())
    if not packages:
        return [
            Check(
                "角色包",
                WARN,
                f"{root} 里没有角色包",
                "每个角色需要一个单独的文件夹，里面有 manifest.json 和 GPT/SoVITS 模型文件。",
            )
        ]
    return [Check("角色包", OK, f"找到 {len(packages)} 个：{', '.join(packages)}")]


def check_providers(providers: Optional[List[Any]]) -> List[Check]:
    if providers is None:
        return [Check("大模型服务", SKIP, "数据库暂时读不到")]
    configured = [getattr(provider, "id", "?") for provider in providers]
    if not configured:
        return [
            Check(
                "大模型服务",
                WARN,
                "还没有配置",
                "聊天需要至少一个大模型服务，请在网页「设置中心」里添加。",
            )
        ]
    return [Check("大模型服务", OK, f"已配置 {len(configured)} 个：{', '.join(str(c) for c in configured)}")]


async def check_gpt_sovits(base_url: str, *, offline: bool = False) -> List[Check]:
    target = (base_url or "").strip().rstrip("/")
    if offline:
        return [Check("GPT-SoVITS", SKIP, f"已跳过连通性检测（--offline）；配置的地址：{target or '未设置'}")]
    if not target:
        return [
            Check(
                "GPT-SoVITS",
                FAIL,
                "没有配置语音服务地址",
                "请在网页「设置中心 → 语音服务」填写地址，或设置 GALGAME2VOICE_GPT_SOVITS_BASE_URL（默认 http://127.0.0.1:9880）。",
            )
        ]
    if not target.startswith(("http://", "https://")):
        return [
            Check(
                "GPT-SoVITS",
                FAIL,
                f"语音服务地址无效：{target}",
                "地址必须以 http:// 或 https:// 开头。",
            )
        ]

    import httpx

    try:
        async with create_async_client(trust_env=False, timeout=_SOVITS_PROBE_TIMEOUT_SECONDS) as client:
            try:
                # Same probe as the app's health check: api_v2 has no route at "/" (404)
                # and answers 400 on /control without a command.
                response = await client.get(f"{target}/control")
            except httpx.HTTPError:
                response = None
    except Exception:  # pragma: no cover - defensive
        return [Check("GPT-SoVITS", WARN, "无法发起检测请求")]

    if response is not None and response.status_code in (200, 400):
        return [Check("GPT-SoVITS", OK, f"可以连接：{target}（HTTP {response.status_code}）")]
    return [
        Check(
            "GPT-SoVITS",
            WARN,
            f"连接不上：{target}" if response is None
            else f"{target}/control 返回 HTTP {response.status_code}，那里运行的不是 GPT-SoVITS 语音引擎",
            "请先启动语音引擎，或在网页「设置中心 → 语音服务」改正语音服务地址。",
        )
    ]


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


async def _load_database_state(
    settings: Settings,
) -> Tuple[Optional[List[Any]], Optional[str]]:
    """Reads providers and gpt_sovits_url from the DB.

    Returns None for DB-derived fields when the database is unavailable.
    """
    db_path = get_database_path()
    if not await asyncio.to_thread(Path(db_path).exists):
        return None, None
    try:
        from galgame2voice.database import crud
        from galgame2voice.database.session import get_db

        async with get_db(db_path) as connection:
            row = await crud.get_settings_raw(connection)
            providers = await crud.list_providers(connection)
        db_sovits_url = (row.gpt_sovits_url or "").strip() or None
        return list(providers), db_sovits_url
    except Exception:
        return None, None


async def collect_checks(settings: Optional[Settings] = None, *, offline: bool = False) -> List[Check]:
    """Runs every check and returns the findings in report order."""
    settings = settings or get_settings()
    providers, db_sovits_url = await _load_database_state(settings)

    effective_sovits_url = resolve_sovits_url(
        db_url=db_sovits_url,
        settings_url=str(getattr(settings, "gpt_sovits_base_url", "") or ""),
    ).base_url

    checks: List[Check] = []
    checks.extend(check_runtime())
    checks.extend(check_directories(settings))
    checks.extend(check_database(settings))
    checks.extend(check_ffmpeg())
    checks.extend(check_bind_and_auth(settings))
    checks.extend(check_proxy_environment())
    checks.extend(check_characters(settings))
    checks.extend(check_providers(providers))
    checks.extend(await check_gpt_sovits(effective_sovits_url, offline=offline))
    return checks


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="galgame2voice doctor",
        description="检查本机 Galgame2Voice 的运行环境。",
    )
    parser.add_argument("--json", action="store_true", help="输出机器可读的 JSON")
    parser.add_argument("--offline", action="store_true", help="跳过联网检测")
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
