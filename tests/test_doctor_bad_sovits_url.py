"""doctor reports a GPT-SoVITS address without http:// instead of crashing."""

import pytest

from galgame2voice import doctor
from galgame2voice.config import get_settings


@pytest.fixture
def scheme_less_env_url(tmp_path, monkeypatch):
    # Many tutorials show the engine address without a scheme.
    monkeypatch.setenv("GALGAME2VOICE_GPT_SOVITS_BASE_URL", "127.0.0.1:9880")
    monkeypatch.setenv("GALGAME2VOICE_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("GALGAME2VOICE_DB_PATH", str(tmp_path / "none.db"))
    monkeypatch.setenv("GALGAME_DB_PATH", str(tmp_path / "none.db"))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_malformed_sovits_url_is_a_finding(scheme_less_env_url):
    checks = await doctor.collect_checks(offline=True)
    (sovits,) = [c for c in checks if c.name == "GPT-SoVITS"]
    assert sovits.status == doctor.FAIL
    assert "source: env" in sovits.detail, "says where the bad address came from"
    assert "http://" in sovits.hint


def test_doctor_command_prints_a_report(scheme_less_env_url, capsys):
    assert doctor.main(["--offline"]) == 1
    assert "GPT-SoVITS" in capsys.readouterr().out
