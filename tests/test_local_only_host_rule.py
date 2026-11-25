"""doctor and startup agree on which hosts may run with auth off."""

import pytest

from galgame2voice import doctor, main as main_module
from galgame2voice.config import get_settings


@pytest.mark.parametrize(
    "host",
    ["127.0.0.1", "localhost", "::1", "127.0.0.2", "0:0:0:0:0:0:0:1", "0.0.0.0", "192.168.1.5"],
)
def test_doctor_and_startup_agree_on_auth_off_hosts(monkeypatch, host):
    monkeypatch.setenv("GALGAME2VOICE_HOST", host)
    monkeypatch.setenv("GALGAME2VOICE_AUTH_DISABLED", "1")
    monkeypatch.setenv("GALGAME2VOICE_LOG_TO_FILE", "0")
    get_settings.cache_clear()
    try:
        settings = get_settings()
        (check,) = doctor.check_bind_and_auth(settings)
        try:
            main_module._init_logging_and_safety(settings)
            starts = True
        except RuntimeError:
            starts = False
    finally:
        get_settings.cache_clear()
    assert (check.status == doctor.OK) == starts, f"{host}: doctor {check.status}, starts={starts}"
