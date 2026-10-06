"""
Tests for `galgame2voice doctor`.

The command exists to answer "why doesn't it work on my machine" without reading
a stack trace, so these tests pin the two things that matter: every finding is
classified correctly (OK/WARN/FAIL/SKIP) and aggregated into a stable overall
verdict, and the checks themselves do not need a live GPT-SoVITS server, a
Telegram token or a populated database to produce a useful answer.
"""

import json

import pytest

from galgame2voice import doctor
from galgame2voice.config import get_settings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _settings_from_env(monkeypatch, **env):
    """Builds Settings from an explicit environment (aliases are env-driven)."""
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    return get_settings()


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def project_root(tmp_path, monkeypatch):
    """Redirects every resolved path under a temp project root."""
    monkeypatch.setenv("GALGAME2VOICE_PROJECT_ROOT", str(tmp_path))
    get_settings.cache_clear()
    return tmp_path


class _FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code


class _FakeClient:
    """Stands in for httpx.AsyncClient so no socket is ever opened."""

    def __init__(self, status_code=None, error=None):
        self._status_code = status_code
        self._error = error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def get(self, url):
        if self._error is not None:
            raise self._error
        return _FakeResponse(self._status_code)


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def test_summarize_is_healthy_when_nothing_is_wrong():
    checks = [doctor.Check("a", doctor.OK, "fine"), doctor.Check("b", doctor.SKIP, "n/a")]

    assert doctor.summarize(checks) == doctor.HEALTHY


def test_summarize_is_degraded_on_warning():
    checks = [doctor.Check("a", doctor.OK, "fine"), doctor.Check("b", doctor.WARN, "meh")]

    assert doctor.summarize(checks) == doctor.DEGRADED


def test_summarize_is_failed_on_failure_even_with_warnings():
    checks = [doctor.Check("a", doctor.WARN, "meh"), doctor.Check("b", doctor.FAIL, "broken")]

    assert doctor.summarize(checks) == doctor.FAILED


@pytest.mark.parametrize(
    "status,expected",
    [(doctor.OK, 0), (doctor.SKIP, 0), (doctor.WARN, 0), (doctor.FAIL, 1)],
)
def test_exit_code_only_fails_on_fail(status, expected):
    assert doctor.exit_code([doctor.Check("a", status, "detail")]) == expected


def test_render_includes_status_detail_hint_and_overall():
    text = doctor.render([doctor.Check("Thing", doctor.WARN, "not great", hint="do this")])

    assert "[WARN]" in text
    assert "Thing: not great" in text
    assert "-> do this" in text
    assert text.rstrip().endswith("Overall: DEGRADED")


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def test_check_runtime_passes_on_this_interpreter():
    (check,) = doctor.check_runtime()

    assert check.status == doctor.OK
    assert "requires >= 3.10" in check.detail


def test_check_directories_reports_every_directory_writable(project_root):
    checks = doctor.check_directories(get_settings())

    assert [check.status for check in checks] == [doctor.OK] * 4
    # The writability probe must not be left behind.
    assert not list(project_root.rglob(".doctor_probe_*"))


def test_check_database_warns_before_first_start(tmp_path, monkeypatch):
    monkeypatch.setenv("GALGAME2VOICE_DB_PATH", str(tmp_path / "missing.db"))
    monkeypatch.setenv("GALGAME_DB_PATH", str(tmp_path / "missing.db"))

    (check,) = doctor.check_database(get_settings())

    assert check.status == doctor.WARN
    assert "not created yet" in check.detail


def test_check_database_recognises_a_wal_database(tmp_path, monkeypatch):
    import sqlite3

    db_path = tmp_path / "wal.db"
    connection = sqlite3.connect(db_path)
    connection.execute("PRAGMA journal_mode = WAL;")
    connection.execute("CREATE TABLE settings (id INTEGER PRIMARY KEY);")
    connection.commit()
    connection.close()

    monkeypatch.setenv("GALGAME2VOICE_DB_PATH", str(db_path))
    monkeypatch.setenv("GALGAME_DB_PATH", str(db_path))

    (check,) = doctor.check_database(get_settings())

    assert check.status == doctor.OK
    assert "WAL enabled" in check.detail


def test_check_database_reports_the_env_overridden_path(tmp_path, monkeypatch):
    """doctor must inspect the database the app actually opens."""
    import sqlite3

    db_path = tmp_path / "custom.db"
    sqlite3.connect(db_path).close()
    monkeypatch.setenv("GALGAME2VOICE_DB_PATH", str(db_path))
    monkeypatch.setenv("GALGAME_DB_PATH", str(db_path))

    (check,) = doctor.check_database(get_settings())

    assert str(db_path) in check.detail


def test_check_database_flags_a_corrupt_file(tmp_path, monkeypatch):
    db_path = tmp_path / "corrupt.db"
    db_path.write_bytes(b"this is definitely not a sqlite database" * 8)
    monkeypatch.setenv("GALGAME2VOICE_DB_PATH", str(db_path))
    monkeypatch.setenv("GALGAME_DB_PATH", str(db_path))

    (check,) = doctor.check_database(get_settings())

    assert check.status == doctor.FAIL


def test_check_ffmpeg_warns_when_missing(monkeypatch):
    monkeypatch.setattr(doctor, "find_ffmpeg", lambda *a, **k: None)

    (check,) = doctor.check_ffmpeg()

    assert check.status == doctor.WARN
    assert check.hint


def test_check_ffmpeg_reports_the_resolved_path(monkeypatch):
    monkeypatch.setattr(doctor, "find_ffmpeg", lambda *a, **k: r"C:\tools\ffmpeg.exe")

    (check,) = doctor.check_ffmpeg()

    assert check.status == doctor.OK
    assert r"C:\tools\ffmpeg.exe" in check.detail


def test_bind_check_allows_loopback_without_auth(monkeypatch):
    settings = _settings_from_env(
        monkeypatch, GALGAME2VOICE_HOST="127.0.0.1", GALGAME2VOICE_AUTH_DISABLED="1"
    )

    (check,) = doctor.check_bind_and_auth(settings)

    assert check.status == doctor.OK
    assert "loopback" in check.detail


def test_bind_check_fails_on_exposed_listener_without_auth(monkeypatch):
    settings = _settings_from_env(
        monkeypatch, GALGAME2VOICE_HOST="0.0.0.0", GALGAME2VOICE_AUTH_DISABLED="1"
    )

    (check,) = doctor.check_bind_and_auth(settings)

    assert check.status == doctor.FAIL
    assert "refuses to start" in check.hint


def test_bind_check_allows_exposed_listener_with_auth(monkeypatch):
    settings = _settings_from_env(
        monkeypatch, GALGAME2VOICE_HOST="0.0.0.0", GALGAME2VOICE_AUTH_DISABLED="0"
    )

    (check,) = doctor.check_bind_and_auth(settings)

    assert check.status == doctor.OK
    assert "enabled" in check.detail


def test_proxy_check_reports_ok_when_environment_is_clean(monkeypatch):
    monkeypatch.setattr(doctor, "proxy_environment_is_usable", lambda: True)

    (check,) = doctor.check_proxy_environment()

    assert check.status == doctor.OK


def test_proxy_check_warns_and_explains_when_environment_is_broken(monkeypatch):
    monkeypatch.setattr(doctor, "proxy_environment_is_usable", lambda: False)

    (check,) = doctor.check_proxy_environment()

    assert check.status == doctor.WARN
    assert "[::1]" in check.hint


def test_check_characters_counts_packages_by_manifest(project_root):
    root = project_root / "characters"
    for name in ("Aria", "Kuroneko"):
        (root / name).mkdir(parents=True)
        (root / name / "manifest.json").write_text("{}", encoding="utf-8")
    (root / "not_a_character").mkdir()

    (check,) = doctor.check_characters(get_settings())

    assert check.status == doctor.OK
    assert "2 discovered" in check.detail
    assert "not_a_character" not in check.detail


def test_check_characters_warns_when_none_installed(project_root):
    (project_root / "characters").mkdir()

    (check,) = doctor.check_characters(get_settings())

    assert check.status == doctor.WARN


def test_check_providers_skips_without_database():
    (check,) = doctor.check_providers(None)

    assert check.status == doctor.SKIP


def test_check_providers_warns_when_none_configured():
    (check,) = doctor.check_providers([])

    assert check.status == doctor.WARN
    assert "none configured" in check.detail


def test_check_telegram_is_skipped_when_disabled():
    (check,) = doctor.check_telegram(get_settings(), db_enabled=False)

    assert check.status == doctor.SKIP


def test_check_telegram_fails_when_enabled_without_token(monkeypatch):
    settings = _settings_from_env(
        monkeypatch, GALGAME2VOICE_TELEGRAM_ENABLED="1", GALGAME2VOICE_TELEGRAM_TOKEN=""
    )

    findings = doctor.check_telegram(settings, db_enabled=True, admin_ids="111")

    assert any(check.status == doctor.FAIL for check in findings)


def test_check_telegram_warns_when_the_admin_whitelist_is_empty(monkeypatch):
    monkeypatch.delenv("TELEGRAM_ADMIN_IDS", raising=False)
    monkeypatch.delenv("GALGAME2VOICE_TELEGRAM_ADMIN_IDS", raising=False)
    settings = _settings_from_env(
        monkeypatch, GALGAME2VOICE_TELEGRAM_ENABLED="1", GALGAME2VOICE_TELEGRAM_TOKEN="123456:token"
    )

    findings = doctor.check_telegram(settings, db_enabled=True, admin_ids="")

    admin_check = next(check for check in findings if check.name == "Telegram admins")
    assert admin_check.status == doctor.WARN
    assert "fail-closed" in admin_check.detail


def test_check_telegram_ok_with_token_and_admins(monkeypatch):
    settings = _settings_from_env(
        monkeypatch, GALGAME2VOICE_TELEGRAM_ENABLED="1", GALGAME2VOICE_TELEGRAM_TOKEN="123456:token"
    )

    findings = doctor.check_telegram(settings, db_enabled=True, admin_ids="111, 222")

    assert all(check.status == doctor.OK for check in findings)


def test_check_telegram_falls_back_to_the_env_whitelist(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ADMIN_IDS", "999")
    settings = _settings_from_env(
        monkeypatch, GALGAME2VOICE_TELEGRAM_ENABLED="1", GALGAME2VOICE_TELEGRAM_TOKEN="123456:token"
    )

    findings = doctor.check_telegram(settings, db_enabled=True, admin_ids="")

    admin_check = next(check for check in findings if check.name == "Telegram admins")
    assert admin_check.status == doctor.OK


async def test_sovits_probe_is_skipped_in_offline_mode():
    (check,) = await doctor.check_gpt_sovits("http://127.0.0.1:9880", offline=True)

    assert check.status == doctor.SKIP


async def test_sovits_probe_rejects_a_bad_scheme():
    (check,) = await doctor.check_gpt_sovits("127.0.0.1:9880")

    assert check.status == doctor.FAIL


async def test_sovits_probe_accepts_a_reachable_server(monkeypatch):
    monkeypatch.setattr(doctor, "create_async_client", lambda **kwargs: _FakeClient(status_code=200))

    (check,) = await doctor.check_gpt_sovits("http://127.0.0.1:9880")

    assert check.status == doctor.OK
    assert "reachable" in check.detail


async def test_sovits_probe_warns_when_unreachable(monkeypatch):
    import httpx

    monkeypatch.setattr(
        doctor, "create_async_client", lambda **kwargs: _FakeClient(error=httpx.ConnectError("refused"))
    )

    (check,) = await doctor.check_gpt_sovits("http://127.0.0.1:9880")

    assert check.status == doctor.WARN
    assert "unreachable" in check.detail


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


def test_main_emits_json_and_exits_zero(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GALGAME2VOICE_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("GALGAME2VOICE_DB_PATH", str(tmp_path / "data" / "galgame2voice.db"))
    monkeypatch.setenv("GALGAME_DB_PATH", str(tmp_path / "data" / "galgame2voice.db"))
    monkeypatch.setenv("GALGAME2VOICE_HOST", "127.0.0.1")
    monkeypatch.setenv("GALGAME2VOICE_AUTH_DISABLED", "1")
    get_settings.cache_clear()

    code = doctor.main(["--offline", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert payload["overall"] in (doctor.HEALTHY, doctor.DEGRADED)
    assert payload["checks"]
    assert code == 0


def test_main_returns_failure_exit_code_when_a_check_fails(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GALGAME2VOICE_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("GALGAME2VOICE_DB_PATH", str(tmp_path / "data" / "galgame2voice.db"))
    monkeypatch.setenv("GALGAME_DB_PATH", str(tmp_path / "data" / "galgame2voice.db"))
    monkeypatch.setenv("GALGAME2VOICE_HOST", "0.0.0.0")
    monkeypatch.setenv("GALGAME2VOICE_AUTH_DISABLED", "1")
    get_settings.cache_clear()

    code = doctor.main(["--offline"])

    output = capsys.readouterr().out
    assert code == 1
    assert "Overall: FAILED" in output


def test_cli_entrypoint_dispatches_doctor_without_starting_the_server(monkeypatch):
    import galgame2voice.main as main_module

    seen = {}

    def _fake_doctor_main(argv):
        seen["argv"] = argv
        return 0

    monkeypatch.setattr(main_module.sys, "argv", ["galgame2voice", "doctor", "--offline"])
    monkeypatch.setattr("galgame2voice.doctor.main", _fake_doctor_main)

    with pytest.raises(SystemExit) as excinfo:
        main_module.run()

    assert excinfo.value.code == 0
    assert seen["argv"] == ["--offline"]
