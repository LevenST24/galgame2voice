"""Verify browser auto-open against slow or unrelated loopback services."""

import http.client
from types import SimpleNamespace
from unittest.mock import MagicMock

from scripts import run_server


def run_waiter(monkeypatch, responses, *, app_name="galgame2voice", browser_success=True):
    seconds = [0.0]
    clock = SimpleNamespace(monotonic=lambda: seconds[0], sleep=lambda delay: seconds.__setitem__(0, seconds[0] + delay))
    monkeypatch.setattr(run_server, "time", clock)
    monkeypatch.setattr(run_server, "get_settings", lambda: SimpleNamespace(app_name=app_name))
    browser = MagicMock(return_value=browser_success)
    monkeypatch.setattr(run_server.webbrowser, "open", browser)
    connections = []

    def connect(*args, **kwargs):
        connection = MagicMock()
        status, body = responses[len(connections)]
        connection.getresponse.return_value.status = status
        connection.getresponse.return_value.read.return_value = body
        connections.append(connection)
        return connection

    monkeypatch.setattr(http.client, "HTTPConnection", connect)

    class SynchronousThread:
        def __init__(self, *, target, **kwargs):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(run_server.threading, "Thread", SynchronousThread)
    run_server.auto_open_browser(18080, host="127.0.0.1")
    return browser, connections, seconds[0]


def test_waiter_ignores_unrelated_or_malformed_healthy_responses(monkeypatch):
    browser, connections, _ = run_waiter(monkeypatch, [
        (200, b'{"app":"another-app","status":"ok"}'),
        (200, b'not json'),
        (200, b'{"app":"galgame2voice","status":"ok"}'),
    ])
    assert len(connections) == 3
    assert all(connection.close.called for connection in connections)
    browser.assert_called_once_with("http://127.0.0.1:18080/")


def test_waiter_opens_browser_after_a_slow_start(monkeypatch):
    browser, connections, elapsed = run_waiter(monkeypatch,
        [(503, b"")] * 40 + [(200, b'{"app":"galgame2voice","status":"ok"}')])
    assert len(connections) == 41 and elapsed >= 12
    browser.assert_called_once_with("http://127.0.0.1:18080/")


def test_waiter_reports_a_manual_url_after_the_bounded_wait(monkeypatch, capsys):
    browser, connections, elapsed = run_waiter(monkeypatch, [(503, b"")] * 205)
    browser.assert_not_called()
    assert 60 <= elapsed < 61 and len(connections) <= 201
    assert all(connection.close.called for connection in connections)
    assert "手动访问：http://127.0.0.1:18080/" in capsys.readouterr().out


def test_waiter_uses_configured_application_name_and_reports_browser_failure(monkeypatch, capsys):
    browser, connections, _ = run_waiter(monkeypatch, [(200, b'{"app":"custom-name","status":"ok"}')],
                                         app_name="custom-name", browser_success=False)
    assert len(connections) == 1
    browser.assert_called_once_with("http://127.0.0.1:18080/")
    assert "手动访问：http://127.0.0.1:18080/" in capsys.readouterr().out
