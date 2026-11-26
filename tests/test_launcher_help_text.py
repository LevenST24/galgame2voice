"""The launcher's --help and argument errors are readable for Chinese users."""

import pytest

from scripts import run_server


def test_help_is_chinese_and_documents_the_no_browser_variable(capsys):
    with pytest.raises(SystemExit) as exc:
        run_server.parse_args(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert out.startswith("用法：")
    assert "显示这份帮助并退出" in out
    assert "GALGAME_NO_BROWSER=1" in out
    assert "GALGAME2VOICE_AUTH_DISABLED=0" in out
    for english in ("show this help", "Host interface", "Suppress automatic", "(default:"):
        assert english not in out, english


def test_bad_argument_gets_a_chinese_error_and_exit_code_2(capsys):
    with pytest.raises(SystemExit) as exc:
        run_server.parse_args(["--port", "abc"])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "[错误] 启动参数有误" in err
    assert "--help" in err


def test_parsed_defaults_are_unchanged(monkeypatch):
    for name in ("GALGAME2VOICE_PORT", "PORT", "GALGAME_PORT", "GALGAME2VOICE_HOST", "HOST", "GALGAME_HOST",
                 "GALGAME_NO_BROWSER"):
        monkeypatch.delenv(name, raising=False)
    parsed = run_server.parse_args([])
    assert (parsed.host, parsed.port, parsed.no_browser, parsed.precision) == ("127.0.0.1", 8080, False, None)
