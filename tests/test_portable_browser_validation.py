"""Portable browser smoke must observe a mounted application, not static copy."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import smoke_windows_portable as smoke


def browser_result(monkeypatch, markup):
    monkeypatch.setattr(smoke.subprocess, "CREATE_NO_WINDOW", 0, raising=False)
    monkeypatch.setattr(smoke.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(
        returncode=0, stdout=markup.encode(), stderr=b""))


@pytest.mark.parametrize("markup", [
    '<p>支持语音输入与朗读回复</p>',
    '<p>支持语音输入与朗读回复</p><div class="adv-stage"></div>',
    '<p>支持语音输入与朗读回复</p><div class="adv-stage"><div class="adv-title-screen" hidden></div></div>',
    '<p>支持语音输入与朗读回复</p><script>const fixture="adv-stage adv-title-screen";</script>',
])
def test_static_or_incomplete_pages_do_not_pass_browser_smoke(tmp_path, monkeypatch, markup):
    browser_result(monkeypatch, markup)
    with pytest.raises(RuntimeError, match="initialize"):
        smoke.check_browser(Path("synthetic.exe"), "http://127.0.0.1:1234", {}, tmp_path, tmp_path)


def test_mounted_home_passes_without_depending_on_legacy_sidebar_copy(tmp_path, monkeypatch):
    browser_result(monkeypatch, '<div id="advRoot"><div class="adv-stage is-home"><section class="adv-title-screen"></section></div></div>')
    smoke.check_browser(Path("synthetic.exe"), "http://127.0.0.1:1234", {}, tmp_path, tmp_path)
