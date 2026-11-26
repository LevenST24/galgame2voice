"""The program's .env is read from the program folder in the encoding it was saved in.

It used to be read from whatever folder the console was in, so running
scripts/run_server.py from elsewhere silently ignored it. A .env written by
PowerShell 5.1's `>` (UTF-16) or saved as ANSI on Chinese Windows (GBK) also
crashed startup with a UnicodeDecodeError.
"""

from __future__ import annotations

import pytest

from galgame2voice import config


@pytest.fixture
def install(tmp_path, monkeypatch):
    for key in (
        "GALGAME2VOICE_GPT_SOVITS_BASE_URL",
        "GPT_SOVITS_BASE_URL",
        "GALGAME2VOICE_PROJECT_ROOT",
        "GALGAME2VOICE_PORT",
        "PORT",
        "GALGAME_PORT",
    ):
        monkeypatch.delenv(key, raising=False)
    root = tmp_path / "Galgame2Voice 程序"
    root.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setattr(config, "get_install_root", lambda: root)
    return root


def test_env_in_program_folder_applies_from_any_working_directory(install):
    (install / ".env").write_text("GALGAME2VOICE_GPT_SOVITS_BASE_URL=http://127.0.0.1:9999\n", encoding="utf-8")
    assert config.Settings().gpt_sovits_base_url == "http://127.0.0.1:9999"


@pytest.mark.parametrize("in_install_dir", [True, False])
@pytest.mark.parametrize("encoding", ["utf-16", "gbk", "utf-8-sig", "utf-8"])
def test_env_saved_by_windows_editors_is_read(install, monkeypatch, in_install_dir, encoding):
    if in_install_dir:
        monkeypatch.chdir(install)
    text = "# 语音服务地址\r\nGALGAME2VOICE_GPT_SOVITS_BASE_URL=http://127.0.0.1:9998\r\n"
    (install / ".env").write_bytes(text.encode(encoding))
    assert config.Settings().gpt_sovits_base_url == "http://127.0.0.1:9998"


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16", "gbk"])
def test_cwd_env_is_ignored_when_program_folder_has_no_env(install, tmp_path, encoding):
    (tmp_path / "elsewhere" / ".env").write_bytes("# 其他项目\r\nPORT=19999\r\n".encode(encoding))
    assert config.Settings().port == 8080


def test_env_file_can_still_be_turned_off(install):
    (install / ".env").write_text("GALGAME2VOICE_GPT_SOVITS_BASE_URL=http://127.0.0.1:9997\n", encoding="utf-8")
    assert config.Settings(_env_file=None).gpt_sovits_base_url != "http://127.0.0.1:9997"
