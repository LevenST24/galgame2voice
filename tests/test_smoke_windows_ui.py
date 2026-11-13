"""Unit tests for smoke_windows_ui CDP client and process lifecycle helpers."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import threading
from typing import Any
import unittest
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.smoke_windows_turn_reply import (  # noqa: E402
    verify_sqlite_turn_cardinality,
)
import scripts.smoke_windows_ui as smoke_windows_ui  # noqa: E402
from scripts.smoke_windows_ui import (  # noqa: E402
    SimpleCdpClient,
    build_acceptance_script,
    find_browser,
    run_browser_ui_acceptance,
    stop_browser_process,
    wait_for_initial_navigation,
)


class TestSmokeWindowsUi(unittest.TestCase):
    """Unit tests for WebSocket framing, script building, and lifecycle cleanup."""

    def test_build_acceptance_script_serializes_safe_arguments(self) -> None:
        script = build_acceptance_script(
            target_session_id="session'\"special",
            prompt_text="Hello '\" world",
            repair_audio_url="/api/audio/files/test.wav",
        )
        self.assertIn('"session\'\\"special"', script)
        self.assertIn('"Hello \'\\" world"', script)
        self.assertIn('"/api/audio/files/test.wav"', script)
        self.assertIn(".adv-qm-btn-log", script)
        self.assertIn("gExportChats", script)

    def test_find_browser_resolves_or_returns_none(self) -> None:
        res = find_browser()
        if res is not None:
            self.assertTrue(isinstance(res, Path))
            self.assertTrue(res.is_file())

    def test_stop_browser_process_terminates_owned_pid(self) -> None:
        proc = MagicMock()
        proc.poll.side_effect = [None, 0]
        stop_browser_process(proc, timeout=1.0)
        proc.terminate.assert_called_once()
        proc.wait.assert_called_once()

    def test_stop_browser_process_kills_on_timeout(self) -> None:
        import subprocess

        proc = MagicMock()
        proc.poll.side_effect = [None, None, 0]
        proc.wait.side_effect = [subprocess.TimeoutExpired(cmd="test", timeout=1.0), 0]
        stop_browser_process(proc, timeout=1.0)
        proc.terminate.assert_called_once()
        proc.kill.assert_called_once()

    def test_cdp_client_handshake_and_json_rpc_exchange(self) -> None:
        """Runs a loopback WebSocket test server to verify RFC 6455 framing and RPC handling."""
        server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_sock.bind(("127.0.0.1", 0))
        server_sock.listen(1)
        port = server_sock.getsockname()[1]

        def server_worker() -> None:
            conn, _ = server_sock.accept()
            try:
                data = b""
                while b"\r\n\r\n" not in data:
                    chunk = conn.recv(1024)
                    if not chunk:
                        return
                    data += chunk
                headers = data.decode("latin1", "replace")
                key = ""
                for line in headers.split("\r\n"):
                    if line.lower().startswith("sec-websocket-key:"):
                        key = line.split(":", 1)[1].strip()
                import hashlib

                accept = base64.b64encode(
                    hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()
                ).decode("ascii")
                resp = (
                    "HTTP/1.1 101 Switching Protocols\r\n"
                    "Upgrade: websocket\r\n"
                    "Connection: Upgrade\r\n"
                    f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
                )
                conn.sendall(resp.encode("ascii"))

                # Read client masked frame
                head = conn.recv(2)
                pay_len = head[1] & 0x7F
                mask = conn.recv(4)
                raw = conn.recv(pay_len)
                client_msg = bytes(b ^ mask[i % 4] for i, b in enumerate(raw))
                req_obj = json.loads(client_msg.decode("utf-8"))

                # Send unmasked server frame with matching id
                resp_obj = {"id": req_obj["id"], "result": {"value": 42}}
                payload = json.dumps(resp_obj).encode("utf-8")
                frame = bytearray([0x81, len(payload)]) + payload
                conn.sendall(frame)
            finally:
                conn.close()

        t = threading.Thread(target=server_worker, daemon=True)
        t.start()

        client = SimpleCdpClient(f"ws://127.0.0.1:{port}/devtools/page/test", timeout=5.0)
        try:
            res = client.send_command("Runtime.evaluate", {"expression": "1+41"})
            self.assertEqual(res, {"value": 42})
        finally:
            client.close()
            server_sock.close()
            t.join(timeout=2.0)

    def test_cdp_client_raises_on_rpc_error(self) -> None:
        server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_sock.bind(("127.0.0.1", 0))
        server_sock.listen(1)
        port = server_sock.getsockname()[1]

        def server_worker() -> None:
            conn, _ = server_sock.accept()
            try:
                data = b""
                while b"\r\n\r\n" not in data:
                    chunk = conn.recv(1024)
                    if not chunk:
                        return
                    data += chunk
                headers = data.decode("latin1", "replace")
                key = next(
                    line.split(":", 1)[1].strip()
                    for line in headers.split("\r\n")
                    if line.lower().startswith("sec-websocket-key:")
                )
                import hashlib

                accept = base64.b64encode(
                    hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()
                ).decode("ascii")
                resp = (
                    "HTTP/1.1 101 Switching Protocols\r\n"
                    "Upgrade: websocket\r\n"
                    "Connection: Upgrade\r\n"
                    f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
                )
                conn.sendall(resp.encode("ascii"))

                head = conn.recv(2)
                pay_len = head[1] & 0x7F
                mask = conn.recv(4)
                raw = conn.recv(pay_len)
                client_msg = bytes(b ^ mask[i % 4] for i, b in enumerate(raw))
                req_obj = json.loads(client_msg.decode("utf-8"))

                resp_obj = {"id": req_obj["id"], "error": {"message": "Invalid syntax"}}
                payload = json.dumps(resp_obj).encode("utf-8")
                frame = bytearray([0x81, len(payload)]) + payload
                conn.sendall(frame)
            finally:
                conn.close()

        t = threading.Thread(target=server_worker, daemon=True)
        t.start()

        client = SimpleCdpClient(f"ws://127.0.0.1:{port}/devtools/page/test", timeout=5.0)
        try:
            with self.assertRaises(RuntimeError) as ctx:
                client.send_command("Runtime.evaluate", {"expression": "++"})
            self.assertIn("Invalid syntax", str(ctx.exception))
        finally:
            client.close()
            server_sock.close()
            t.join(timeout=2.0)

    def test_run_browser_ui_acceptance_fails_when_no_browser_found(self) -> None:
        with patch("scripts.smoke_windows_ui.find_browser", return_value=None):
            with self.assertRaises(RuntimeError) as ctx:
                run_browser_ui_acceptance(
                    base_url="http://127.0.0.1:1234",
                    scratch=Path("tmp_fake"),
                    target_session_id="test",
                    prompt_text="test",
                    repair_audio_url="test",
                )
            self.assertIn("No supported Chromium or Microsoft Edge", str(ctx.exception))

    def test_build_acceptance_script_matches_exact_synth_prompt(self) -> None:
        synth_prompt = "合成验收提问"
        script = build_acceptance_script(
            target_session_id="portable_turn_reply",
            prompt_text=synth_prompt,
            repair_audio_url="/api/audio/files/voice_1_test.wav",
        )
        self.assertIn(json.dumps(synth_prompt), script)
        self.assertIn('"portable_turn_reply"', script)
        self.assertIn('"/api/audio/files/voice_1_test.wav"', script)

    def test_verify_sqlite_turn_cardinality_enforces_strict_bounds(self) -> None:
        import sqlite3
        import tempfile

        with tempfile.TemporaryDirectory() as tmp_dir:
            db_file = Path(tmp_dir) / "test.db"
            conn = sqlite3.connect(db_file)
            try:
                conn.execute("PRAGMA user_version = 7;")
                conn.execute(
                    "CREATE TABLE messages ("
                    "id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, "
                    "lines_json TEXT, reply_to_message_id INTEGER);"
                )
                audio_data = json.dumps([{"index": 0, "audio": {"chunks": [{"audio_url": "/api/audio/repair.wav"}]}}])
                conn.execute(
                    "INSERT INTO messages (id, session_id, role, lines_json, reply_to_message_id) "
                    "VALUES (1, 's1', 'user', '[]', NULL);"
                )
                conn.execute(
                    "INSERT INTO messages (id, session_id, role, lines_json, reply_to_message_id) "
                    "VALUES (2, 's1', 'assistant', ?, 1);",
                    (audio_data,),
                )
                conn.commit()
            finally:
                conn.close()

            res = verify_sqlite_turn_cardinality(
                db_path=db_file,
                session_id="s1",
                u_id=1,
                assistant_id=2,
                l_idx=0,
                repair_audio_url="/api/audio/repair.wav",
            )
            self.assertEqual(res["total_messages"], 2)
            self.assertEqual(res["assistant_reply_to"], 1)

            # Extra row fails cardinality
            conn = sqlite3.connect(db_file)
            try:
                conn.execute(
                    "INSERT INTO messages (id, session_id, role, lines_json, reply_to_message_id) "
                    "VALUES (3, 's1', 'user', '[]', NULL);"
                )
                conn.commit()
            finally:
                conn.close()

            with self.assertRaises(AssertionError) as ctx:
                verify_sqlite_turn_cardinality(
                    db_path=db_file,
                    session_id="s1",
                    u_id=1,
                    assistant_id=2,
                    l_idx=0,
                    repair_audio_url="/api/audio/repair.wav",
                )
            self.assertIn("Expected exactly 2 total messages", str(ctx.exception))

    def test_verify_sqlite_turn_cardinality_handles_special_characters_in_path(self) -> None:
        """Paths containing '#' or '%' must not truncate URI or create ghost files in verify_sqlite_turn_cardinality."""
        import sqlite3
        import tempfile

        with tempfile.TemporaryDirectory() as tmp_dir:
            special_dir = Path(tmp_dir) / "sub#1%20dir"
            special_dir.mkdir(parents=True, exist_ok=True)
            db_file = special_dir / "test.db"
            conn = sqlite3.connect(str(db_file))
            try:
                conn.execute("PRAGMA user_version = 7;")
                conn.execute(
                    "CREATE TABLE messages ("
                    "id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, "
                    "lines_json TEXT, reply_to_message_id INTEGER);"
                )
                audio_data = json.dumps([{"index": 0, "audio": {"chunks": [{"audio_url": "/api/audio/repair.wav"}]}}])
                conn.execute(
                    "INSERT INTO messages (id, session_id, role, lines_json, reply_to_message_id) "
                    "VALUES (1, 's1', 'user', '[]', NULL);"
                )
                conn.execute(
                    "INSERT INTO messages (id, session_id, role, lines_json, reply_to_message_id) "
                    "VALUES (2, 's1', 'assistant', ?, 1);",
                    (audio_data,),
                )
                conn.commit()
            finally:
                conn.close()

            res = verify_sqlite_turn_cardinality(
                db_path=db_file,
                session_id="s1",
                u_id=1,
                assistant_id=2,
                l_idx=0,
                repair_audio_url="/api/audio/repair.wav",
            )
            self.assertEqual(res["total_messages"], 2)
            stray_file = Path(tmp_dir) / "sub"
            self.assertFalse(stray_file.exists(), f"Stray file was created at {stray_file}")

    def test_direct_script_execution_resolves_browser_ui_without_pythonpath(self) -> None:
        """Regression test: running smoke script directly sets sys.path[0] to scripts/ without parent PYTHONPATH."""
        script_path = REPO_ROOT / "scripts" / "smoke_windows_turn_reply.py"
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)

        # 1. Direct invocation with --help
        res_help = subprocess.run(
            [sys.executable, str(script_path), "--help"],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=5.0,
        )
        self.assertEqual(res_help.returncode, 0, f"Direct script --help failed: {res_help.stderr}")
        self.assertIn("--with-browser-ui", res_help.stdout)

        # 2. Direct import of the browser UI helper from smoke_windows_turn_reply context
        code = (
            "import sys; "
            "import smoke_windows_turn_reply as s; "
            "from scripts.smoke_windows_ui import run_browser_ui_acceptance; "
            "print('IMPORT_RESOLVED')"
        )
        res_import = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(REPO_ROOT / "scripts"),
            env=env,
            capture_output=True,
            text=True,
            timeout=5.0,
        )
        self.assertEqual(res_import.returncode, 0, f"Import resolution failed: {res_import.stderr}")
        self.assertIn("IMPORT_RESOLVED", res_import.stdout)

    def _run_with_mocked_driver(self, mock_client: MagicMock, timeout: float = 5.0) -> dict[str, Any]:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp_dir:
            scratch = Path(tmp_dir)

            def fake_popen(*args: Any, **kwargs: Any) -> MagicMock:
                proc = MagicMock()
                proc.poll.return_value = None
                proc.pid = 9999
                profile = scratch / "ui_profile"
                profile.mkdir(parents=True, exist_ok=True)
                (profile / "DevToolsActivePort").write_text("9222\n/devtools/browser/fake", encoding="utf-8")
                return proc

            def fake_urlopen(url: str, *args: Any, **kwargs: Any) -> MagicMock:
                mock_resp = MagicMock()
                mock_resp.read.return_value = json.dumps([
                    {"type": "page", "webSocketDebuggerUrl": "ws://127.0.0.1:9222/page/fake"}
                ]).encode("utf-8")
                mock_resp.__enter__.return_value = mock_resp
                return mock_resp

            with patch("scripts.smoke_windows_ui.find_browser", return_value=Path("C:/dummy/chrome.exe")), \
                 patch("scripts.smoke_windows_ui.subprocess.Popen", side_effect=fake_popen), \
                 patch("scripts.smoke_windows_ui.make_cdp_client", return_value=mock_client), \
                 patch("urllib.request.urlopen", side_effect=fake_urlopen):
                return run_browser_ui_acceptance(
                    base_url="http://127.0.0.1:8000",
                    scratch=scratch,
                    target_session_id="test_session",
                    prompt_text="test_prompt",
                    repair_audio_url="/api/audio/test.wav",
                    timeout=timeout,
                )

    def test_initial_navigation_tolerates_context_destroyed_and_runs_acceptance_once(self) -> None:
        mock_client = MagicMock()
        eval_calls: list[str] = []

        def mock_send(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
            if method == "Runtime.evaluate":
                expr = (params or {}).get("expression", "")
                eval_calls.append(expr)
                if len(eval_calls) == 1:
                    # Initial context destroyed during navigation polling
                    raise RuntimeError(
                        "CDP command Runtime.evaluate failed: {'code': -32000, 'message': 'Execution context was destroyed.'}"
                    )
                elif len(eval_calls) == 2:
                    # Next poll: ready
                    return {"result": {"value": {"url": "http://127.0.0.1:8000/", "readyState": "complete"}}}
                else:
                    # Business acceptance script
                    return {"result": {"value": {"ok": True, "steps": {"mock": True}}}}
            return {}

        mock_client.send_command.side_effect = mock_send
        res = self._run_with_mocked_driver(mock_client)
        self.assertEqual(res, {"ok": True, "steps": {"mock": True}})
        # Exactly 3 evaluate calls: 2 for readiness polling, 1 for acceptance script
        self.assertEqual(len(eval_calls), 3)
        self.assertIn("document.URL", eval_calls[0])
        self.assertIn("document.URL", eval_calls[1])
        self.assertIn("adv-stage", eval_calls[2])

    def test_initial_navigation_non_transient_error_propagates_immediately(self) -> None:
        mock_client = MagicMock()
        eval_calls: list[str] = []

        def mock_send(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
            if method == "Runtime.evaluate":
                expr = (params or {}).get("expression", "")
                eval_calls.append(expr)
                # Generic -32000 with non-transient message must not be retried
                raise RuntimeError(
                    "CDP command Runtime.evaluate failed: {'code': -32000, 'message': 'No target with given id found'}"
                )
            return {}

        mock_client.send_command.side_effect = mock_send
        with self.assertRaises(RuntimeError) as ctx:
            self._run_with_mocked_driver(mock_client, timeout=5.0)
        self.assertIn("No target with given id found", str(ctx.exception))
        # Fails immediately on 1st evaluate without retry loop and never runs acceptance
        self.assertEqual(len(eval_calls), 1)
        self.assertIn("document.URL", eval_calls[0])

    def test_initial_navigation_wrong_url_or_never_ready_times_out_without_acceptance(self) -> None:
        def make_sender(bad_val: dict[str, Any], calls: list[str]) -> Any:
            def mock_send(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
                if method == "Runtime.evaluate":
                    expr = (params or {}).get("expression", "")
                    calls.append(expr)
                    return {"result": {"value": bad_val}}
                return {}
            return mock_send

        for bad_val in (
            {"url": "about:blank", "readyState": "complete"},
            {"url": "http://127.0.0.1:8000/unexpected", "readyState": "complete"},
            {"url": "http://127.0.0.1:8000/", "readyState": "loading"},
        ):
            with self.subTest(bad_val=bad_val):
                mock_client = MagicMock()
                eval_calls: list[str] = []
                mock_client.send_command.side_effect = make_sender(bad_val, eval_calls)
                with self.assertRaises(TimeoutError) as ctx:
                    self._run_with_mocked_driver(mock_client, timeout=0.15)
                self.assertIn("Timed out waiting for initial navigation", str(ctx.exception))
                # Acceptance script was NEVER evaluated
                self.assertTrue(all("adv-stage" not in expr for expr in eval_calls))

    def test_business_context_destroyed_propagates_and_executes_once(self) -> None:
        mock_client = MagicMock()
        eval_calls: list[str] = []

        def mock_send(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
            if method == "Runtime.evaluate":
                expr = (params or {}).get("expression", "")
                eval_calls.append(expr)
                if "document.URL" in expr:
                    # Navigation polling succeeds
                    return {"result": {"value": {"url": "http://127.0.0.1:8000/", "readyState": "complete"}}}
                else:
                    # Business phase context destroyed
                    raise RuntimeError(
                        "CDP command Runtime.evaluate failed: {'code': -32000, 'message': 'Execution context was destroyed.'}"
                    )
            return {}

        mock_client.send_command.side_effect = mock_send
        with self.assertRaises(RuntimeError) as ctx:
            self._run_with_mocked_driver(mock_client, timeout=5.0)
        self.assertIn("Execution context was destroyed", str(ctx.exception))
        # Business acceptance script was executed exactly once (no replay/retry!)
        business_calls = [expr for expr in eval_calls if "adv-stage" in expr]
        self.assertEqual(len(business_calls), 1)

    def _make_browser_harness(self, root: Path, outcomes: list[tuple[int | None, int | None]]) -> Any:
        profiles, procs, cmds = [], [], []
        n = 0

        def make_profile_dir() -> Path:
            nonlocal n
            n += 1
            p = root / f"profile_{n}"
            p.mkdir(parents=True, exist_ok=True)
            return p

        def fake_popen(cmd: list[str], *args: Any, **kwargs: Any) -> MagicMock:
            poll_val, port = outcomes[len(procs)]
            profile = Path(next(a.split("=", 1)[1] for a in cmd if a.startswith("--user-data-dir=")))
            self.assertEqual(list(profile.iterdir()), [])
            profiles.append(profile)
            cmds.append(list(cmd))
            proc = MagicMock(pid=1000 + len(procs))
            proc.poll.return_value = poll_val
            procs.append(proc)
            if port is not None:
                (profile / "DevToolsActivePort").write_text(f"{port}\n/devtools/browser/fake", encoding="utf-8")
            return proc

        return make_profile_dir, fake_popen, procs, profiles, cmds

    def test_run_browser_ui_acceptance_retries_with_fresh_profile_when_first_launch_stalls(self) -> None:
        mock_client = MagicMock()
        mock_client.send_command.side_effect = lambda m, p=None: (
            {"result": {"value": {"url": "http://127.0.0.1:8000/", "readyState": "complete"}}}
            if m == "Runtime.evaluate" and "document.URL" in (p or {}).get("expression", "")
            else ({"result": {"value": {"ok": True}}} if m == "Runtime.evaluate" else {})
        )
        resp = MagicMock()
        resp.read.return_value = json.dumps([{"type": "page", "webSocketDebuggerUrl": "ws://127.0.0.1:9333/page/fake"}]).encode()
        resp.__enter__.return_value = resp
        with tempfile.TemporaryDirectory() as tmp_dir:
            _, fake_popen, procs, profiles, _ = self._make_browser_harness(Path(tmp_dir), [(None, None), (None, 9333)])
            with patch("scripts.smoke_windows_ui.find_browser", return_value=Path("C:/dummy/chrome.exe")), \
                 patch("scripts.smoke_windows_ui.subprocess.Popen", side_effect=fake_popen), \
                 patch("scripts.smoke_windows_ui.make_cdp_client", return_value=mock_client), \
                 patch("urllib.request.urlopen", return_value=resp) as urlopen_mock:
                res = run_browser_ui_acceptance("http://127.0.0.1:8000", Path(tmp_dir), "s1", "hi", "/a.wav", timeout=0.3)
        self.assertEqual(res, {"ok": True})
        self.assertEqual(len(profiles), 2)
        self.assertNotEqual(profiles[0], profiles[1])
        self.assertIn(":9333/", urlopen_mock.call_args.args[0])
        procs[0].terminate.assert_called_once()
        procs[1].terminate.assert_called_once()

    def test_launch_browser_with_devtools_retries_once_on_stall_and_stops_first_proc(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            make_profile_dir, fake_popen, procs, profiles, cmds = self._make_browser_harness(
                Path(tmp_dir), [(None, None), (None, 9444)]
            )
            with patch("scripts.smoke_windows_ui.subprocess.Popen", side_effect=fake_popen):
                proc, port, profile_dir = smoke_windows_ui.launch_browser_with_devtools(
                    Path("C:/dummy/chrome.exe"), make_profile_dir, ["--headless", "--remote-debugging-port=0"],
                    attempt_timeout=0.3, attempts=2,
                )
        self.assertIs(proc, procs[1])
        self.assertEqual(port, 9444)
        self.assertEqual(profile_dir, profiles[1])
        self.assertEqual(len(profiles), 2)
        self.assertNotEqual(profiles[0], profiles[1])
        procs[0].terminate.assert_called_once()
        procs[1].terminate.assert_not_called()
        self.assertIn(f"--user-data-dir={profiles[1]}", cmds[1])

    def test_launch_browser_with_devtools_does_not_retry_when_process_exits(self) -> None:
        for exit_code in (42, 0):
            with self.subTest(exit_code=exit_code), tempfile.TemporaryDirectory() as tmp_dir:
                make_profile_dir, fake_popen, procs, profiles, _ = self._make_browser_harness(
                    Path(tmp_dir), [(exit_code, None)]
                )
                with patch("scripts.smoke_windows_ui.subprocess.Popen", side_effect=fake_popen) as popen_mock:
                    with self.assertRaises(RuntimeError) as ctx:
                        smoke_windows_ui.launch_browser_with_devtools(
                            Path("C:/dummy/chrome.exe"), make_profile_dir, ["--headless"],
                            attempt_timeout=0.3, attempts=2,
                        )
                self.assertEqual(popen_mock.call_count, 1)
                self.assertEqual(len(profiles), 1)
                procs[0].terminate.assert_not_called()
                self.assertIn(f"exit code: {exit_code}", str(ctx.exception))

    def test_launch_browser_with_devtools_fails_with_attempt_durations_and_exit_codes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            make_profile_dir, fake_popen, procs, profiles, _ = self._make_browser_harness(
                Path(tmp_dir), [(None, None), (None, None)]
            )
            with patch("scripts.smoke_windows_ui.subprocess.Popen", side_effect=fake_popen) as popen_mock:
                with self.assertRaises(RuntimeError) as ctx:
                    smoke_windows_ui.launch_browser_with_devtools(
                        Path("C:/dummy/chrome.exe"), make_profile_dir, ["--headless"],
                        attempt_timeout=0.2, attempts=2,
                    )
        self.assertEqual(popen_mock.call_count, 2)
        self.assertEqual(len(profiles), 2)
        procs[0].terminate.assert_called_once()
        procs[1].terminate.assert_called_once()
        msg = str(ctx.exception)
        self.assertGreaterEqual(len(re.findall(r"\d+\.\d+s", msg)), 2, msg)
        self.assertIn("attempt 1", msg)
        self.assertIn("attempt 2", msg)
        self.assertEqual(msg.count("exit code: None"), 2)

    def test_real_browser_synthetic_navigation_proof(self) -> None:
        browser_exe = find_browser()
        if not browser_exe:
            self.skipTest("No supported browser found on host")

        from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
        import tempfile

        # Threaded: Chromium may open a speculative connection and send nothing on it; a
        # single-threaded server blocks on that socket and never answers the navigation.
        class _SynthServer(ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(self, request: Any, client_address: Any) -> None:
                pass

        class _SynthHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(b"<!doctype html><html><head><title>Synth</title></head><body><h1>Synthetic Ready</h1></body></html>")

            def log_message(self, format: str, *args: Any) -> None:
                pass

        server = _SynthServer(("127.0.0.1", 0), _SynthHandler)
        port = server.server_port
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_root = Path(tmp_dir)
            attempt_count = 0

            def make_profile_dir() -> Path:
                nonlocal attempt_count
                attempt_count += 1
                profile = tmp_root / f"profile_{attempt_count}"
                profile.mkdir(parents=True, exist_ok=True)
                return profile

            extra_args = [
                "--headless",
                "--no-sandbox",
                "--disable-gpu",
                "--no-first-run",
                "--disable-sync",
                "--no-proxy-server",
                "--disable-background-networking",
                "--disable-extensions",
                "--remote-debugging-port=0",
                "--remote-allow-origins=*",
            ]
            proc = None
            client = None
            try:
                proc, cdp_port, _profile_dir = smoke_windows_ui.launch_browser_with_devtools(
                    browser_exe,
                    make_profile_dir,
                    extra_args,
                    attempt_timeout=15.0,
                    attempts=2,
                )

                import urllib.request
                with urllib.request.urlopen(f"http://127.0.0.1:{cdp_port}/json/list", timeout=5) as res:
                    targets = json.load(res)
                target = next((t for t in targets if t.get("type") == "page"), None)
                self.assertIsNotNone(target)

                client = SimpleCdpClient(target["webSocketDebuggerUrl"])
                base_url = f"http://127.0.0.1:{port}"
                ready_state = wait_for_initial_navigation(client, base_url, timeout=10.0)
                self.assertEqual(ready_state["url"].rstrip("/"), base_url)
                self.assertIn(ready_state["readyState"], ("interactive", "complete"))
            finally:
                if client:
                    client.close()
                if proc is not None:
                    stop_browser_process(proc)
                server.shutdown()
                server.server_close()



if __name__ == "__main__":
    unittest.main()
