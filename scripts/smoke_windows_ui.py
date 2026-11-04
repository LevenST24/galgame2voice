"""Headless browser UI acceptance helper for packaged Galgame2Voice portable application.

Zero-dependency implementation using Chrome DevTools Protocol (CDP) over a pure
standard-library RFC 6455 WebSocket client, with optional websockets fallback.
Operates against the live restarted portable HTTP server without adding new dependencies.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import time
from typing import Any
import urllib.parse
import urllib.request


def find_browser() -> Path | None:
    """Finds installed Google Chrome, Chromium, or Microsoft Edge executable."""
    for name in ("google-chrome", "chromium", "chromium-browser", "msedge", "chrome"):
        exe = shutil.which(name)
        if exe:
            return Path(exe)

    candidates = [
        Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("LocalAppData", "C:/Users/Default/AppData/Local")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("LocalAppData", "C:/Users/Default/AppData/Local")) / "Microsoft/Edge/Application/msedge.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


class SimpleCdpClient:
    """Pure standard-library WebSocket client implementing RFC 6455 for CDP."""

    def __init__(self, ws_url: str, timeout: float = 15.0) -> None:
        u = urllib.parse.urlparse(ws_url)
        host = u.hostname or "127.0.0.1"
        port = u.port or 80
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        req = (
            f"GET {u.path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n"
            f"Origin: http://127.0.0.1\r\n\r\n"
        )
        self.sock.sendall(req.encode("ascii"))
        res = b""
        while b"\r\n\r\n" not in res:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("WebSocket handshake failed (unexpected EOF)")
            res += chunk
        status_line = res.split(b"\r\n")[0]
        if b" 101 " not in status_line:
            raise ConnectionError(f"WebSocket handshake failed: {status_line.decode('latin1', 'replace')}")
        self._seq = 0

    def _read_exact(self, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("Unexpected EOF while reading WebSocket frame")
            buf.extend(chunk)
        return bytes(buf)

    def _recv_frame(self) -> bytes:
        head = self._read_exact(2)
        opcode = head[0] & 0x0F
        masked = bool(head[1] & 0x80)
        pay_len = head[1] & 0x7F
        if pay_len == 126:
            pay_len = int.from_bytes(self._read_exact(2), "big")
        elif pay_len == 127:
            pay_len = int.from_bytes(self._read_exact(8), "big")
        mask = self._read_exact(4) if masked else b""
        raw = self._read_exact(pay_len)
        if masked:
            raw = bytes(b ^ mask[i % 4] for i, b in enumerate(raw))
        if opcode == 0x8:
            raise ConnectionError("WebSocket closed by server")
        return raw

    def send_command(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._seq += 1
        msg = json.dumps({"id": self._seq, "method": method, "params": params or {}}).encode("utf-8")
        frame = bytearray([0x81])
        length = len(msg)
        if length < 126:
            frame.append(0x80 | length)
        elif length < 65536:
            frame.append(0x80 | 126)
            frame.extend(length.to_bytes(2, "big"))
        else:
            frame.append(0x80 | 127)
            frame.extend(length.to_bytes(8, "big"))
        mask = os.urandom(4)
        frame.extend(mask)
        frame.extend(b ^ mask[i % 4] for i, b in enumerate(msg))
        self.sock.sendall(frame)

        while True:
            payload = self._recv_frame()
            data = json.loads(payload.decode("utf-8"))
            if data.get("id") == self._seq:
                if "error" in data:
                    raise RuntimeError(f"CDP command {method} failed: {data['error']}")
                return data.get("result", {})

    def close(self) -> None:
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()


def make_cdp_client(ws_url: str) -> Any:
    """Connects to CDP endpoint via websockets if present, or SimpleCdpClient."""
    try:
        from websockets.sync.client import connect as ws_connect

        class _WsAdapter:
            def __init__(self, ws: Any) -> None:
                self.ws = ws
                self._seq = 0

            def send_command(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
                self._seq += 1
                self.ws.send(json.dumps({"id": self._seq, "method": method, "params": params or {}}))
                while True:
                    data = json.loads(self.ws.recv(timeout=15))
                    if data.get("id") == self._seq:
                        if "error" in data:
                            raise RuntimeError(f"CDP command {method} failed: {data['error']}")
                        return data.get("result", {})

            def close(self) -> None:
                try:
                    self.ws.close()
                except Exception:
                    pass

        return _WsAdapter(ws_connect(ws_url, origin="http://127.0.0.1", open_timeout=10, close_timeout=3))
    except (ImportError, Exception):
        return SimpleCdpClient(ws_url)


def stop_browser_process(proc: subprocess.Popen, timeout: float = 5.0) -> None:
    """Terminates a browser process strictly by its owned PID."""
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=min(3.0, timeout))
    except (OSError, subprocess.TimeoutExpired):
        try:
            proc.kill()
            proc.wait(timeout=2.0)
        except (OSError, subprocess.TimeoutExpired):
            pass


def build_acceptance_script(target_session_id: str, prompt_text: str, repair_audio_url: str) -> str:
    """Builds self-contained JavaScript for headless DOM acceptance inspection."""
    return f"""(async () => {{
  const result = {{ ok: false, steps: {{}} }};
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const settle = () => document.getAnimations().forEach(a => {{ try {{ a.finish(); }} catch(e) {{}} }});
  const waitFor = async (fn, timeoutMs = 12000, desc = '') => {{
    const start = Date.now();
    while (Date.now() - start < timeoutMs) {{
      settle();
      try {{
        const val = await fn();
        if (val) return val;
      }} catch (e) {{}}
      await sleep(100);
    }}
    throw new Error('Timeout waiting for: ' + desc);
  }};

  // 1. Initial Home Screen check
  const stage = await waitFor(() => document.querySelector('.adv-stage'), 12000, 'adv-stage');
  result.steps.stageFound = true;
  result.steps.initiallyHome = stage.classList.contains('is-home');

  // 2. Settings Center inspection: single dialog, tabs, close/return home
  const gearBtn = await waitFor(() => document.querySelector('.home-gear'), 5000, 'home-gear button');
  gearBtn.click();
  await sleep(250);
  settle();

  const globalModal = document.getElementById('globalModal');
  if (!globalModal || globalModal.classList.contains('hidden')) {{
    throw new Error('globalModal did not open on clicking home-gear');
  }}

  const visibleDialogs = [...document.querySelectorAll('[role="dialog"]')].filter(el => {{
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && !el.closest('[hidden], .hidden');
  }});
  if (visibleDialogs.length !== 1) {{
    throw new Error('Expected exactly 1 visible dialog, found: ' + visibleDialogs.length);
  }}
  result.steps.singleVisibleDialog = true;

  const modelsTab = document.querySelector('[data-gtab="models"]');
  if (!modelsTab) throw new Error('Missing [data-gtab="models"] tab button');
  modelsTab.click();
  await sleep(200);
  settle();
  const modelsPanel = document.getElementById('gp-models');
  if (!modelsPanel || modelsPanel.classList.contains('hidden')) {{
    throw new Error('Models settings panel not visible after switching tab');
  }}
  result.steps.tabModelsSwitched = true;

  const updateTab = document.querySelector('[data-gtab="update"]');
  if (!updateTab) throw new Error('Missing [data-gtab="update"] tab button');
  updateTab.click();
  await sleep(200);
  settle();
  const updatePanel = document.getElementById('gp-update');
  if (!updatePanel || updatePanel.classList.contains('hidden')) {{
    throw new Error('Update settings panel not visible after switching tab');
  }}
  result.steps.tabUpdateSwitched = true;

  const homeBtn = document.querySelector('#globalModal [data-home]');
  if (homeBtn) {{
    homeBtn.click();
  }} else {{
    const closeBtn = document.querySelector('#globalModal .close-btn, #globalModal [data-close]');
    if (closeBtn) closeBtn.click();
  }}
  await sleep(250);
  settle();

  if (!globalModal.classList.contains('hidden')) {{
    throw new Error('Settings modal did not close after clicking home/close');
  }}
  if (!stage.classList.contains('is-home') || stage.closest('[inert]')) {{
    throw new Error('Stage did not return to home or remained inert');
  }}
  result.steps.settingsClosedCleanly = true;

  // 3. Enter dialogue scene and verify persisted LOG entries & audio link
  const continueBtn = await waitFor(
    () => document.querySelector('.home-continue') || document.querySelector('[data-session="{target_session_id}"]'),
    6000, 'home-continue or target session slot'
  );
  continueBtn.click();
  await sleep(350);
  settle();

  if (stage.classList.contains('is-home')) {{
    throw new Error('Stage remained on home screen after enter action');
  }}
  result.steps.enteredDialogue = true;

  const logBtn = await waitFor(() => document.querySelector('.adv-qm-btn-log'), 5000, 'adv-qm-btn-log');
  logBtn.click();
  await sleep(300);
  settle();

  const logOverlay = document.querySelector('.adv-log-overlay');
  if (!logOverlay || logOverlay.hidden || logOverlay.classList.contains('hidden')) {{
    throw new Error('LOG overlay did not open on clicking adv-qm-btn-log');
  }}

  const logEntries = [...document.querySelectorAll('.adv-log-entry')];
  if (logEntries.length !== 2) {{
    throw new Error('Expected exactly 2 persisted LOG entries, found: ' + logEntries.length);
  }}

  const userEntry = logEntries.find(el => el.classList.contains('is-user'));
  const assistantEntry = logEntries.find(el => el.classList.contains('is-assistant'));
  if (!userEntry || !assistantEntry) {{
    throw new Error('LOG entries missing user or assistant element');
  }}

  const userText = userEntry.querySelector('.adv-log-primary')?.textContent || '';
  if (!userText.includes({json.dumps(prompt_text)})) {{
    throw new Error('User log entry text does not match prompt: ' + userText);
  }}

  const replayBtn = assistantEntry.querySelector('.adv-log-replay-btn');
  if (!replayBtn) {{
    throw new Error('Assistant log entry missing replay button');
  }}
  if (replayBtn.disabled) {{
    throw new Error('Assistant replay button is unexpectedly disabled');
  }}

  const audioFetchRes = await fetch({json.dumps(repair_audio_url)});
  if (audioFetchRes.status !== 200) {{
    throw new Error('Browser audio URL fetch returned HTTP ' + audioFetchRes.status);
  }}
  result.steps.logVerification = {{
    entriesCount: logEntries.length,
    userPromptMatched: true,
    hasPlayableVoice: !replayBtn.disabled,
    audioHttpOk: true,
  }};

  const logCloseBtn = logOverlay.querySelector('.adv-log-close-btn');
  if (logCloseBtn) logCloseBtn.click();
  await sleep(200);
  settle();
  result.steps.logClosed = true;

  // 4. Test Backup Export through real UI button click
  const qmConfigBtn = await waitFor(() => document.querySelector('.adv-qm-btn-config'), 5000, 'adv-qm-btn-config');
  qmConfigBtn.click();
  await sleep(250);
  settle();

  const updateTab2 = document.querySelector('[data-gtab="update"]');
  if (updateTab2) updateTab2.click();
  await sleep(200);
  settle();

  const exportBtn = document.getElementById('gExportChats');
  if (!exportBtn) throw new Error('Missing gExportChats button');

  let exportedRaw = null;
  const originalCreateObjectURL = URL.createObjectURL;
  try {{
    const blobPromise = new Promise((resolve, reject) => {{
      const timer = setTimeout(() => reject(new Error('Export blob creation timed out')), 8000);
      URL.createObjectURL = (blob) => {{
        if (blob instanceof Blob) {{
          blob.text().then(text => {{
            clearTimeout(timer);
            resolve(text);
          }}).catch(reject);
        }}
        return originalCreateObjectURL(blob);
      }};
    }});

    exportBtn.click();
    exportedRaw = await blobPromise;
  }} finally {{
    URL.createObjectURL = originalCreateObjectURL;
  }}

  const backupData = JSON.parse(exportedRaw);
  if (!backupData || !Array.isArray(backupData.sessions)) {{
    throw new Error('Exported backup payload missing sessions array');
  }}
  const exportedSession = backupData.sessions.find(s => s.id === {json.dumps(target_session_id)});
  if (!exportedSession) {{
    throw new Error('Exported backup missing target session: {target_session_id}');
  }}
  if (!Array.isArray(exportedSession.messages) || exportedSession.messages.length !== 2) {{
    throw new Error('Target session messages count in backup is not 2, got: ' + (exportedSession.messages?.length));
  }}
  result.steps.backupExportVerification = {{
    sessionsCount: backupData.sessions.length,
    targetSessionId: exportedSession.id,
    targetMessagesCount: exportedSession.messages.length,
  }};

  const homeBtn2 = document.querySelector('#globalModal [data-home]');
  if (homeBtn2) homeBtn2.click();
  await sleep(200);
  settle();

  result.ok = true;
  return result;
}})()"""


def run_browser_ui_acceptance(
    base_url: str,
    scratch: Path,
    target_session_id: str,
    prompt_text: str,
    repair_audio_url: str,
    timeout: float = 40.0,
) -> dict[str, Any]:
    """Launches headless Chrome/Edge and runs browser UI inspection against restarted app."""
    browser_exe = find_browser()
    if not browser_exe:
        raise RuntimeError("No supported Chromium or Microsoft Edge browser found on host for --with-browser-ui.")

    scratch.mkdir(parents=True, exist_ok=True)
    profile_dir = scratch / "ui_profile"
    profile_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        str(browser_exe),
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
        f"--user-data-dir={profile_dir}",
        base_url,
    ]

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    client = None
    try:
        deadline = time.monotonic() + min(15.0, timeout)
        port_file = profile_dir / "DevToolsActivePort"
        cdp_port = None
        while time.monotonic() < deadline and proc.poll() is None:
            if port_file.is_file():
                try:
                    cdp_port = int(port_file.read_text(encoding="utf-8").splitlines()[0])
                    break
                except (OSError, ValueError, IndexError):
                    pass
            time.sleep(0.05)

        if cdp_port is None:
            raise RuntimeError(f"Browser did not expose DevToolsActivePort within deadline (PID {proc.pid})")

        with urllib.request.urlopen(f"http://127.0.0.1:{cdp_port}/json/list", timeout=5) as res:
            targets = json.load(res)
        target = next((t for t in targets if t.get("type") == "page"), None)
        if not target or "webSocketDebuggerUrl" not in target:
            raise RuntimeError(f"Could not locate active page target in CDP targets: {targets}")

        client = make_cdp_client(target["webSocketDebuggerUrl"])
        client.send_command("Runtime.enable")

        js_code = build_acceptance_script(target_session_id, prompt_text, repair_audio_url)
        eval_resp = client.send_command(
            "Runtime.evaluate",
            {"expression": js_code, "awaitPromise": True, "returnByValue": True},
        )

        if "exceptionDetails" in eval_resp:
            exc = eval_resp["exceptionDetails"]
            desc = exc.get("exception", {}).get("description") or exc.get("text", "Unknown exception")
            raise RuntimeError(f"Browser UI evaluation error: {desc}")

        val = eval_resp.get("result", {}).get("value")
        if not isinstance(val, dict) or not val.get("ok"):
            raise RuntimeError(f"Browser UI acceptance failed: {val}")

        return val
    except Exception as exc:
        if client is not None:
            try:
                shot = client.send_command("Page.captureScreenshot")
                if "data" in shot:
                    (scratch / "browser_ui_failure.png").write_bytes(base64.b64decode(shot["data"]))
            except Exception:
                pass
        (scratch / "browser_ui_error.log").write_text(f"{type(exc).__name__}: {exc}\n", encoding="utf-8")
        raise
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
        stop_browser_process(proc, timeout=5.0)
        # Attempt non-blocking profile cleanup without recursive deletion of scratch root
        shutil.rmtree(profile_dir, ignore_errors=True)
