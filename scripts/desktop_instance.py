"""One portable server per installation in the current Windows session."""

import ctypes
import hashlib
import json
import os
from pathlib import Path
import tempfile
from time import monotonic, sleep
import urllib.request
import webbrowser

from galgame2voice.utils.windows_runtime import kernel32


class DesktopInstance:
    def __init__(self, root: Path):
        identity = os.path.normcase(str(root.resolve())).encode("utf-8")
        self.name = "Local\\Galgame2Voice-" + hashlib.sha256(identity).hexdigest()
        self.handle = None
        self.acquired = False

    def acquire(self) -> bool:
        api = kernel32()
        ctypes.set_last_error(0)
        self.handle = api.CreateMutexW(None, True, self.name)
        error = ctypes.get_last_error()
        if not self.handle:
            raise ctypes.WinError(error)
        self.acquired = error != 183  # ERROR_ALREADY_EXISTS
        return self.acquired

    def close(self) -> None:
        if self.handle:
            if self.acquired:
                kernel32().ReleaseMutex(self.handle)
            kernel32().CloseHandle(self.handle)
            self.handle = None
            self.acquired = False


def write_instance_address(root: Path, host: str, port: int) -> None:
    """Publish the actual bind address atomically, after choosing the port."""
    folder = root / "data"
    folder.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=folder, delete=False) as handle:
            temporary = Path(handle.name)
            json.dump({"pid": os.getpid(), "host": host, "port": port}, handle)
        temporary.replace(folder / "desktop_instance.json")
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def open_existing_instance(root: Path, *, no_browser: bool, timeout: float = 8.0) -> None:
    """Wait briefly for a first launch, then reuse its verified web address."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        try:
            data = json.loads((root / "data" / "desktop_instance.json").read_text(encoding="utf-8"))
            port = data["port"]
            if type(port) is not int or not 1 <= port <= 65535:
                raise ValueError("Invalid instance port")
            host = data["host"]
            # Never open a URL supplied by an arbitrary metadata file.
            if host in ("0.0.0.0", "::", "127.0.0.1", "localhost"):
                host = "127.0.0.1"
            elif host == "::1":
                host = "[::1]"
            else:
                raise ValueError("Existing instance is not bound to loopback")
            url = f"http://{host}:{port}/"
            with opener.open(url + "api/health", timeout=0.5) as response:
                health = json.loads(response.read(4096))
            if health.get("app") != "galgame2voice" or health.get("status") != "ok":
                raise ValueError("Unexpected service")
            print(f"[提示] Galgame2Voice 已在运行，访问地址：{url}")
            if not no_browser:
                webbrowser.open(url)
            return
        except (OSError, ValueError, KeyError, TypeError):
            sleep(0.2)
    print("[提示] Galgame2Voice 已启动，正在准备网页。请保留原启动窗口，稍候访问。")
