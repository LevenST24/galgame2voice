"""Unit tests for smoke_windows_ui CDP client and process lifecycle helpers."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import unittest
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.smoke_windows_turn_reply import (  # noqa: E402
    verify_sqlite_turn_cardinality,
)
from scripts.smoke_windows_ui import (  # noqa: E402
    SimpleCdpClient,
    build_acceptance_script,
    find_browser,
    run_browser_ui_acceptance,
    stop_browser_process,
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


if __name__ == "__main__":
    unittest.main()
