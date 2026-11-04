"""Unit regression tests for smoke_windows_turn_reply HTTP transport and socket handling."""

from __future__ import annotations

import faulthandler
import http.client
import io
from pathlib import Path
import socket
import sys
import threading
import time
import unittest
import urllib.request

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.smoke_windows_turn_reply import (  # noqa: E402
    CaseInsensitiveDict,
    read_sse_bounded,
    request_http,
    verify_port_released,
)


def make_test_opener(client_sock: socket.socket) -> urllib.request.OpenerDirector:
    class SocketPairHTTPHandler(urllib.request.HTTPHandler):
        def http_open(self, req: urllib.request.Request):
            class CustomConn(http.client.HTTPConnection):
                def connect(self):
                    self.sock = client_sock
                    if self.timeout is not None:
                        self.sock.settimeout(self.timeout)

            return self.do_open(CustomConn, req)

    return urllib.request.build_opener(SocketPairHTTPHandler)


class TestSmokeWindowsHttpTransport(unittest.TestCase):
    """Transport unit tests verifying EOF, case-insensitivity, SSE timing, and port rebind."""

    @classmethod
    def setUpClass(cls) -> None:
        faulthandler.enable()
        try:
            faulthandler.dump_traceback_later(timeout=35.0, exit=True)
        except (AttributeError, io.UnsupportedOperation):
            pass

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            faulthandler.cancel_dump_traceback_later()
        except AttributeError:
            pass

    def _cleanup_pair_and_thread(
        self, s1: socket.socket, s2: socket.socket, t: threading.Thread | None = None
    ) -> None:
        for s in (s1, s2):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                s.close()
            except OSError:
                pass
        if t is not None:
            t.join(timeout=3.0)
            self.assertFalse(t.is_alive(), "Server thread did not terminate cleanly after shutdown and close")

    def test_content_length_short_json(self) -> None:
        s1, s2 = socket.socketpair()
        s1.settimeout(5.0)
        s2.settimeout(5.0)
        t = None
        try:
            def server():
                try:
                    s2.recv(1024)
                    s2.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 15\r\n\r\n{\"status\":\"ok\"}")
                except Exception:
                    pass

            t = threading.Thread(target=server)
            t.start()
            opener = make_test_opener(s1)
            st, headers, body = request_http(opener, "GET", "http://127.0.0.1/health", timeout=3.0)
            self.assertEqual(st, 200)
            self.assertEqual(body, b"{\"status\":\"ok\"}")
        finally:
            self._cleanup_pair_and_thread(s1, s2, t)

    def test_empty_body(self) -> None:
        s1, s2 = socket.socketpair()
        s1.settimeout(5.0)
        s2.settimeout(5.0)
        t = None
        try:
            def server():
                try:
                    s2.recv(1024)
                    s2.sendall(b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n")
                except Exception:
                    pass

            t = threading.Thread(target=server)
            t.start()
            opener = make_test_opener(s1)
            st, headers, body = request_http(opener, "GET", "http://127.0.0.1/empty", timeout=3.0)
            self.assertEqual(st, 204)
            self.assertEqual(body, b"")
        finally:
            self._cleanup_pair_and_thread(s1, s2, t)

    def test_chunked_response(self) -> None:
        s1, s2 = socket.socketpair()
        s1.settimeout(5.0)
        s2.settimeout(5.0)
        t = None
        try:
            def server():
                try:
                    s2.recv(1024)
                    resp = (
                        b"HTTP/1.1 200 OK\r\n"
                        b"Transfer-Encoding: chunked\r\n\r\n"
                        b"4\r\nWiki\r\n"
                        b"6\r\npedia \r\n"
                        b"0\r\n\r\n"
                    )
                    s2.sendall(resp)
                except Exception:
                    pass

            t = threading.Thread(target=server)
            t.start()
            opener = make_test_opener(s1)
            st, headers, body = request_http(opener, "GET", "http://127.0.0.1/chunked", timeout=3.0)
            self.assertEqual(st, 200)
            self.assertEqual(body, b"Wikipedia ")
        finally:
            self._cleanup_pair_and_thread(s1, s2, t)

    def test_header_case_insensitivity_lowercase(self) -> None:
        s1, s2 = socket.socketpair()
        s1.settimeout(5.0)
        s2.settimeout(5.0)
        t = None
        try:
            def server():
                try:
                    s2.recv(1024)
                    resp = (
                        b"HTTP/1.1 200 OK\r\n"
                        b"content-type: application/javascript; charset=utf-8\r\n"
                        b"Content-Length: 14\r\n\r\n"
                        b"console.log();"
                    )
                    s2.sendall(resp)
                except Exception:
                    pass

            t = threading.Thread(target=server)
            t.start()
            opener = make_test_opener(s1)
            st, headers, body = request_http(opener, "GET", "http://127.0.0.1/asset.js", timeout=3.0)
            self.assertEqual(st, 200)
            self.assertIsInstance(headers, CaseInsensitiveDict)
            self.assertIn("content-type", headers)
            self.assertIn("Content-Type", headers)
            self.assertIn("CONTENT-TYPE", headers)
            self.assertEqual(headers.get("Content-Type"), "application/javascript; charset=utf-8")
            self.assertEqual(headers.get("content-type"), "application/javascript; charset=utf-8")
            self.assertEqual(headers["CONTENT-TYPE"], "application/javascript; charset=utf-8")
        finally:
            self._cleanup_pair_and_thread(s1, s2, t)

    def test_header_case_insensitivity_uppercase(self) -> None:
        s1, s2 = socket.socketpair()
        s1.settimeout(5.0)
        s2.settimeout(5.0)
        t = None
        try:
            def server():
                try:
                    s2.recv(1024)
                    resp = (
                        b"HTTP/1.1 200 OK\r\n"
                        b"CONTENT-TYPE: text/css\r\n"
                        b"Content-Length: 9\r\n\r\n"
                        b"body { }\n"
                    )
                    s2.sendall(resp)
                except Exception:
                    pass

            t = threading.Thread(target=server)
            t.start()
            opener = make_test_opener(s1)
            st, headers, body = request_http(opener, "GET", "http://127.0.0.1/style.css", timeout=3.0)
            self.assertEqual(st, 200)
            self.assertEqual(headers.get("Content-Type"), "text/css")
            self.assertEqual(headers.get("content-type"), "text/css")
            self.assertEqual(headers["CONTENT-TYPE"], "text/css")
        finally:
            self._cleanup_pair_and_thread(s1, s2, t)

    def test_header_case_insensitivity_mixedcase(self) -> None:
        s1, s2 = socket.socketpair()
        s1.settimeout(5.0)
        s2.settimeout(5.0)
        t = None
        try:
            def server():
                try:
                    s2.recv(1024)
                    resp = (
                        b"HTTP/1.1 200 OK\r\n"
                        b"CoNtEnT-tYpE: text/html; charset=utf-8\r\n"
                        b"Content-Length: 6\r\n\r\n"
                        b"<html>"
                    )
                    s2.sendall(resp)
                except Exception:
                    pass

            t = threading.Thread(target=server)
            t.start()
            opener = make_test_opener(s1)
            st, headers, body = request_http(opener, "GET", "http://127.0.0.1/", timeout=3.0)
            self.assertEqual(st, 200)
            self.assertEqual(headers.get("Content-Type"), "text/html; charset=utf-8")
            self.assertEqual(headers.get("content-type"), "text/html; charset=utf-8")
        finally:
            self._cleanup_pair_and_thread(s1, s2, t)

    def test_sse_delayed_first_sentence(self) -> None:
        """Verify SSE reader handles model delay > 1s without socket poison or premature timeout."""
        s1, s2 = socket.socketpair()
        s1.settimeout(10.0)
        s2.settimeout(10.0)
        t = None
        try:
            def sse_server():
                try:
                    req_buf = b""
                    while b"\r\n\r\n" not in req_buf:
                        chunk = s2.recv(1024)
                        if not chunk:
                            return
                        req_buf += chunk

                    s2.sendall(
                        b"HTTP/1.1 200 OK\r\n"
                        b"Content-Type: text/event-stream\r\n"
                        b"Cache-Control: no-cache\r\n"
                        b"Connection: close\r\n\r\n"
                    )
                    time.sleep(1.2)
                    payload = (
                        'event: turn_started\r\n'
                        'data: {"user_message_id": 42}\r\n\r\n'
                        'event: line\r\n'
                        'data: {"line_index": 0, "ja": "遅れてすみません", "emotion": "gentle"}\r\n\r\n'
                    ).encode("utf-8")
                    s2.sendall(payload)
                    while True:
                        data = s2.recv(1024)
                        if not data:
                            break
                except Exception:
                    pass

            t = threading.Thread(target=sse_server)
            t.start()

            orig_connect = http.client.HTTPConnection.connect
            try:
                def custom_connect(conn_self):
                    conn_self.sock = s1
                    if conn_self.timeout is not None:
                        conn_self.sock.settimeout(conn_self.timeout)

                http.client.HTTPConnection.connect = custom_connect
                u_id, l_idx, ja_text, emotion, done_seen, events, first_line_t, close_t = read_sse_bounded(
                    "127.0.0.1", 12345, "/api/chat/stream",
                    payload={"prompt": "hi"},
                    app_base="http://127.0.0.1:12345",
                    sse_timeout=10.0,
                )
                self.assertEqual(u_id, 42)
                self.assertEqual(l_idx, 0)
                self.assertEqual(ja_text, "遅れてすみません")
                self.assertEqual(emotion, "gentle")
                self.assertFalse(done_seen)
                self.assertIsNotNone(first_line_t)
                self.assertIsNotNone(close_t)
            finally:
                http.client.HTTPConnection.connect = orig_connect
        finally:
            self._cleanup_pair_and_thread(s1, s2, t)

    def test_transient_port_bind_retry(self) -> None:
        """Verify verify_port_released retries boundedly during transient bind conflict and succeeds."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]

        hold_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            hold_sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        hold_sock.bind(("127.0.0.1", port))

        def release_after_delay():
            time.sleep(0.3)
            try:
                hold_sock.close()
            except OSError:
                pass

        t = threading.Thread(target=release_after_delay)
        t.start()
        try:
            res = verify_port_released(port, deadline=time.monotonic() + 3.0)
            self.assertTrue(res.get("success"))
            self.assertGreaterEqual(res.get("attempts", 0), 2)
            self.assertTrue(len(res.get("history", [])) >= 1)
        finally:
            try:
                hold_sock.close()
            except OSError:
                pass
            t.join(timeout=3.0)
            self.assertFalse(t.is_alive(), "Release thread did not terminate cleanly")


if __name__ == "__main__":
    unittest.main()
