"""Regression tests for hardware probe subprocess timeout with pipe-holding descendants."""

from pathlib import Path
import subprocess
import sys
import time

import psutil
import pytest

from galgame2voice.utils import hardware

pytestmark = [
    pytest.mark.requires_piped_subprocess,
    pytest.mark.requires_process_termination,
    pytest.mark.real_hardware_probe,
]

_SPAWN_HOLDING_GRANDCHILD_CODE = (
    "import pathlib, subprocess, sys, time\n"
    "pid_path = pathlib.Path(sys.argv[1])\n"
    "parent_sleep = float(sys.argv[2])\n"
    "child = subprocess.Popen(\n"
    '    [sys.executable, "-S", "-c", "import time; time.sleep(8)"],\n'
    "    close_fds=False,\n"
    ")\n"
    "pid_path.write_text(str(child.pid), encoding='utf-8')\n"
    "if parent_sleep > 0:\n"
    "    time.sleep(parent_sleep)\n"
    "else:\n"
    "    print('NVIDIA GeForce RTX 4090', flush=True)\n"
)


def _build_pipe_holding_cmd(pid_file: Path, parent_sleep: float = 8.0) -> list[str]:
    return [sys.executable, "-S", "-c", _SPAWN_HOLDING_GRANDCHILD_CODE, str(pid_file), str(parent_sleep)]


def _is_pid_alive(pid: int) -> bool:
    try:
        proc = psutil.Process(pid)
        return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def _wait_pid_dead(pid: int, timeout: float = 1.5) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _is_pid_alive(pid):
            return True
        time.sleep(0.02)
    return not _is_pid_alive(pid)


def _cleanup_grandchild(pid_file: Path) -> None:
    if not pid_file.is_file():
        return
    raw = pid_file.read_text(encoding="utf-8").strip()
    if not raw:
        return
    pid = int(raw)
    if not _is_pid_alive(pid):
        return
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=3.0,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    try:
        proc = psutil.Process(pid)
        proc.kill()
        proc.wait(timeout=2.0)
    except (psutil.NoSuchProcess, psutil.TimeoutExpired, psutil.AccessDenied):
        pass


class _RedirectNvidiaSmiSubprocess:
    """Forwards all subprocess attributes while replacing nvidia-smi with a pipe-holding command."""

    def __init__(self, real_subprocess, replacement_cmd: list[str]) -> None:
        self._real = real_subprocess
        self._replacement_cmd = replacement_cmd

    def __getattr__(self, name: str):
        return getattr(self._real, name)

    def Popen(self, cmd, *args, **kwargs):
        if isinstance(cmd, list) and cmd and cmd[0] == "nvidia-smi":
            cmd = self._replacement_cmd
        return self._real.Popen(cmd, *args, **kwargs)

    def check_output(self, cmd, *args, **kwargs):
        if isinstance(cmd, list) and cmd and cmd[0] == "nvidia-smi":
            cmd = self._replacement_cmd
        return self._real.check_output(cmd, *args, **kwargs)


def test_exec_command_output_returns_promptly_when_grandchild_holds_stdout(tmp_path: Path) -> None:
    pid_file = tmp_path / "grandchild.pid"
    cmd = _build_pipe_holding_cmd(pid_file)
    try:
        t0 = time.monotonic()
        res = hardware._exec_command_output(cmd, timeout=0.5)
        elapsed = time.monotonic() - t0

        assert res is None
        assert elapsed < 4.0, f"expected bounded timeout return (<4.0s), took {elapsed:.2f}s"
        assert pid_file.is_file(), "grandchild pid file was not written before timeout"
        grandchild_pid = int(pid_file.read_text(encoding="utf-8").strip())
        assert _wait_pid_dead(grandchild_pid), f"grandchild pid {grandchild_pid} still alive after timeout cleanup"
    finally:
        _cleanup_grandchild(pid_file)


def test_exec_command_output_returns_promptly_when_parent_exits_leaving_pipe_holder(tmp_path: Path) -> None:
    pid_file = tmp_path / "orphan_pipe_holder.pid"
    cmd = _build_pipe_holding_cmd(pid_file, parent_sleep=0.0)
    try:
        t0 = time.monotonic()
        res = hardware._exec_command_output(cmd, timeout=0.5)
        elapsed = time.monotonic() - t0

        assert res is None
        assert elapsed < 4.0, f"expected bounded pipe drain (<4.0s), took {elapsed:.2f}s"
    finally:
        _cleanup_grandchild(pid_file)


def test_get_gpu_vram_status_timeout_with_pipe_holder_returns_none_tuple(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    pid_file = tmp_path / "vram_grandchild.pid"
    py_cmd = _build_pipe_holding_cmd(pid_file)

    monkeypatch.delitem(sys.modules, "torch", raising=False)
    monkeypatch.setattr(hardware, "DEFAULT_SUBPROCESS_TIMEOUT", 0.5)
    monkeypatch.setattr(
        hardware,
        "subprocess",
        _RedirectNvidiaSmiSubprocess(hardware.subprocess, py_cmd),
    )

    try:
        t0 = time.monotonic()
        vram = hardware.get_gpu_vram_status()
        elapsed = time.monotonic() - t0

        assert vram == (None, None)
        assert elapsed < 4.0, f"expected bounded VRAM probe timeout (<4.0s), took {elapsed:.2f}s"
        assert pid_file.is_file(), "grandchild pid file was not written before timeout"
        grandchild_pid = int(pid_file.read_text(encoding="utf-8").strip())
        assert _wait_pid_dead(grandchild_pid), f"grandchild pid {grandchild_pid} still alive after timeout cleanup"
    finally:
        _cleanup_grandchild(pid_file)


def test_exec_command_output_normal_nonzero_empty_and_invalid_utf8() -> None:
    ok = hardware._exec_command_output([sys.executable, "-S", "-c", "print('NVIDIA GeForce RTX 4090')"])
    assert ok is not None and ok.strip() == "NVIDIA GeForce RTX 4090"

    nonzero = hardware._exec_command_output(
        [sys.executable, "-S", "-c", "import sys; sys.stdout.write('oops'); sys.exit(2)"]
    )
    assert nonzero is None

    empty = hardware._exec_command_output([sys.executable, "-S", "-c", "pass"])
    assert empty is None

    bad_utf8 = hardware._exec_command_output(
        [sys.executable, "-S", "-c", r"import sys; sys.stdout.buffer.write(b'\xff\xfe')"]
    )
    assert bad_utf8 is None

