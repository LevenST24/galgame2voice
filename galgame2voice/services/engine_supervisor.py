"""Injectable supervision seam for reading and managing local/remote GPT-SoVITS engine state."""

import inspect
import logging
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
from typing import Any, Protocol

from fastapi import Request

from galgame2voice.services.engine_recovery import ENGINE_LOG_START
from galgame2voice.services.engine_runtime import EngineLaunch, prepare_engine_entrypoint
from galgame2voice.services.sovits_endpoint import SovitsEndpoint
from galgame2voice.services.sovits_installation import find_engine_python
from galgame2voice.utils.precision import write_sovits_yaml_config
from galgame2voice.utils.windows_runtime import kernel32

logger = logging.getLogger("galgame2voice.services.engine_supervisor")

_RUNTIME_HOST: Any = None
_SPAWNED_SOVITS_PROC: Any = None
_SPAWNED_SOVITS_STARTED_AT: float | None = None
_SPAWNED_SOVITS_LAUNCH: EngineLaunch | None = None
_WINDOWS_JOB_HANDLE: Any = None
_IPV4_LOOPBACK_HOSTS = {"127.0.0.1", "localhost"}


def register_runtime_host(host_module: Any) -> None:
    """Register the launcher runtime module so supervisor and launcher share process state."""
    global _RUNTIME_HOST
    _RUNTIME_HOST = host_module


def _host() -> Any:
    return _RUNTIME_HOST if _RUNTIME_HOST is not None else sys.modules[__name__]


def _resolve_project_root(project_root: Path | None = None) -> Path:
    from galgame2voice.config import get_settings
    from galgame2voice.runtime_paths import get_install_root

    install_root = get_install_root()
    root = Path(project_root if project_root is not None else get_settings().project_root)
    if (
        root != install_root
        or "GALGAME2VOICE_PROJECT_ROOT" in os.environ
        or "PROJECT_ROOT" in os.environ
    ):
        return root
    if _RUNTIME_HOST is not None:
        host_root = getattr(_RUNTIME_HOST, "PROJECT_ROOT", None)
        if host_root is not None and Path(host_root) != install_root:
            return Path(host_root)
    return root


def _supports_project_root(fn: Any) -> bool:
    try:
        return "project_root" in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


def _normalize_host(host: Any) -> str:
    h = str(host or "").strip().lower()
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1].strip()
    return h


def _is_matching_managed_host(endpoint_host: Any, launch_host: Any) -> bool:
    ep_h = _normalize_host(endpoint_host)
    l_h = _normalize_host(launch_host)
    if not ep_h or not l_h:
        return False
    if ep_h == l_h:
        return True
    if {ep_h, l_h} <= _IPV4_LOOPBACK_HOSTS:
        return True
    if l_h == "0.0.0.0" and ep_h in _IPV4_LOOPBACK_HOSTS:
        return True
    return False


def get_managed_engine_launch(endpoint: Any) -> EngineLaunch | None:
    """Match a launch to its owned process and endpoint, including an exited process."""
    runtime_host = _host()
    launch = runtime_host._SPAWNED_SOVITS_LAUNCH
    if (
        endpoint is None
        or not endpoint.is_local
        or launch is None
        or launch.process is not runtime_host._SPAWNED_SOVITS_PROC
        or endpoint.port != launch.port
        or not _is_matching_managed_host(endpoint.host, launch.host)
    ):
        return None
    return launch


def get_managed_engine_precision(endpoint: Any) -> str | None:
    """Return launch precision only for the live owned process at this endpoint."""
    launch = _host().get_managed_engine_launch(endpoint)
    if launch is None or launch.process.poll() is not None:
        return None
    return "CPU" if launch.device == "cpu" else "FP16" if launch.is_half else "FP32"


def setup_windows_job_object():
    """Creates a Windows Job Object configured with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE."""
    runtime_host = _host()
    if runtime_host.sys.platform != "win32":
        return None
    existing = getattr(runtime_host, "_WINDOWS_JOB_HANDLE", None)
    if existing:
        return existing
    try:
        import ctypes
        from ctypes import wintypes

        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
        JobObjectExtendedLimitInformation = 9

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
                ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryLimit", ctypes.c_size_t),
                ("PeakJobMemoryLimit", ctypes.c_size_t),
            ]

        api = kernel32()
        h_job = api.CreateJobObjectW(None, None)
        if not h_job:
            return None

        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE

        res = api.SetInformationJobObject(
            h_job,
            JobObjectExtendedLimitInformation,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not res:
            api.CloseHandle(h_job)
            return None

        runtime_host._WINDOWS_JOB_HANDLE = h_job
        return h_job
    except Exception:
        return None


def assign_process_to_job(proc) -> bool:
    """Assigns a spawned child process to the Windows Job Object."""
    runtime_host = _host()
    job_handle = getattr(runtime_host, "_WINDOWS_JOB_HANDLE", None)
    if runtime_host.sys.platform != "win32" or not job_handle:
        return False
    try:
        if hasattr(proc, "_handle") and proc._handle:
            return bool(kernel32().AssignProcessToJobObject(job_handle, int(proc._handle)))
    except Exception:
        pass
    return False


def terminate_process_tree(pid: int, timeout: float = 2.0) -> None:
    """Robustly terminates a process and all of its recursive child processes."""
    if not pid or pid <= 1 or pid == os.getpid():
        return

    runtime_host = _host()
    host_sys = runtime_host.sys
    host_subprocess = runtime_host.subprocess

    psutil_success = False
    still_alive = False
    try:
        import psutil

        try:
            parent = psutil.Process(pid)
            children = parent.children(recursive=True)
            for child in children:
                try:
                    child.terminate()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            parent.terminate()

            _, alive = psutil.wait_procs(children + [parent], timeout=timeout)
            for p in alive:
                try:
                    p.kill()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            if alive:
                _, still_alive_procs = psutil.wait_procs(alive, timeout=0.5)
                if still_alive_procs:
                    still_alive = True
            psutil_success = not still_alive
        except psutil.NoSuchProcess:
            psutil_success = True
        except (psutil.AccessDenied, Exception):
            psutil_success = False
    except Exception:
        psutil_success = False

    if not psutil_success or still_alive:
        if host_sys.platform == "win32":
            try:
                host_subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    stdout=host_subprocess.DEVNULL,
                    stderr=host_subprocess.DEVNULL,
                    timeout=3.0,
                    check=False,
                )
            except Exception:
                pass
        else:
            try:
                host_subprocess.run(
                    ["pkill", "-TERM", "-P", str(pid)],
                    stdout=host_subprocess.DEVNULL,
                    stderr=host_subprocess.DEVNULL,
                    timeout=1.0,
                    check=False,
                )
            except Exception:
                pass

            try:
                pgid = os.getpgid(pid)
                if pgid != os.getpgrp() and pgid > 1:
                    os.killpg(pgid, signal.SIGTERM)
                    time.sleep(0.1)
                    os.killpg(pgid, signal.SIGKILL)
            except Exception:
                pass

            try:
                os.kill(pid, signal.SIGTERM)
                time.sleep(0.1)
                os.kill(pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass


def is_port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    """Checks if a TCP port is open and listening across IPv4 and IPv6."""
    host_socket = _host().socket
    try:
        infos = host_socket.getaddrinfo(
            host,
            port,
            family=host_socket.AF_UNSPEC,
            type=host_socket.SOCK_STREAM,
        )
    except (socket.gaierror, OSError):
        return False

    for family, socktype, proto, _, sockaddr in infos:
        try:
            with host_socket.socket(family, socktype, proto) as s:
                s.settimeout(0.5)
                if s.connect_ex(sockaddr) == 0:
                    return True
        except OSError:
            continue
    return False


def build_gpt_sovits_env(
    sovits_dir: Path,
    is_half: bool = False,
    *,
    project_root: Path | None = None,
) -> dict[str, str]:
    """Constructs an isolated process environment for GPT-SoVITS."""
    root = _resolve_project_root(project_root)
    env = os.environ.copy()
    env["is_half"] = "True" if is_half else "False"

    runtime_scripts = (
        (sovits_dir / "runtime" / "Scripts")
        if _host().sys.platform == "win32"
        else (sovits_dir / "runtime" / "bin")
    )
    env["PATH"] = os.pathsep.join(
        [str(sovits_dir / "runtime"), str(runtime_scripts), env.get("PATH", "")]
    )
    env["PYTHONIOENCODING"] = "utf-8"
    env["no_proxy"] = "localhost, 127.0.0.1, ::1"
    env["NO_PROXY"] = "localhost, 127.0.0.1, ::1"
    env["all_proxy"] = ""
    env["ALL_PROXY"] = ""

    cache_dir = root / "data" / "engine_cache" / "numba"
    cache_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryFile(dir=cache_dir):
        pass
    env["NUMBA_CACHE_DIR"] = str(cache_dir)

    sovits_temp = cache_dir.parent / "temp"
    sovits_temp.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryFile(dir=sovits_temp):
        pass
    for name in ("TEMP", "TMP", "TMPDIR"):
        env[name] = str(sovits_temp)

    return env


def _launch_sovits_process(
    sovits_dir: Path,
    host: str,
    port: int,
    is_half: bool,
    device: str = "cuda",
    *,
    project_root: Path | None = None,
    warn_missing_runtime: bool = False,
) -> tuple[subprocess.Popen, bool]:
    """Launches the GPT-SoVITS API daemon and returns (process, assigned_to_job)."""
    runtime_host = _host()
    host_sys = getattr(runtime_host, "sys", sys)
    root = _resolve_project_root(project_root)
    python_exe = getattr(runtime_host, "find_engine_python", find_engine_python)(sovits_dir)
    if python_exe is None:
        if getattr(host_sys, "frozen", False):
            raise RuntimeError(
                "GPT-SoVITS 目录缺少 runtime/python.exe。请选择包含运行环境的完整引擎包，或连接已启动的引擎。"
            )
        if warn_missing_runtime:
            print("      [提示] 该 GPT-SoVITS 集成包缺少内置 runtime/python 解释器；")
            print("             将改用当前 Python 启动引擎，若报缺少 GPT-SoVITS 依赖，")
            print("             请下载官方完整集成包 (含 runtime 目录) 或手动安装其 requirements。")
        python_exe = Path(host_sys.executable)

    runtime_dir = root / "data" / "engine_cache"
    entrypoint = getattr(
        runtime_host, "prepare_engine_entrypoint", prepare_engine_entrypoint
    )(sovits_dir, runtime_dir / "bootstrap")
    synced_yaml = getattr(
        runtime_host, "write_sovits_yaml_config", write_sovits_yaml_config
    )(sovits_dir, is_half, device=device, output_path=runtime_dir / "tts_infer.yaml")
    config_arg = "GPT_SoVITS/configs/tts_infer.yaml"
    if synced_yaml:
        try:
            config_arg = str(synced_yaml.relative_to(sovits_dir))
        except ValueError:
            config_arg = str(synced_yaml)

    cmd = [
        str(python_exe),
        "-I",
        "-u",
        str(entrypoint),
        "-a",
        host,
        "-p",
        str(port),
        "-c",
        config_arg,
    ]

    log_file = root / "logs" / "gpt_sovits.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        if log_file.exists() and log_file.stat().st_size > 10 * 1024 * 1024:
            old_file = log_file.with_suffix(".old.log")
            if old_file.exists():
                old_file.unlink()
            log_file.rename(old_file)
    except OSError:
        pass
    flags = subprocess.CREATE_NO_WINDOW if host_sys.platform == "win32" else 0
    build_env_fn = runtime_host.build_gpt_sovits_env
    if _supports_project_root(build_env_fn):
        env = build_env_fn(sovits_dir, is_half=is_half, project_root=root)
    else:
        env = build_env_fn(sovits_dir, is_half=is_half)
    log_fp = open(log_file, "a", encoding="utf-8")
    log_fp.write(f"\n{ENGINE_LOG_START}\n")
    log_fp.flush()
    extra_popen_kwargs: dict[str, Any] = {}
    if host_sys.platform != "win32":
        extra_popen_kwargs["start_new_session"] = True
    try:
        proc = runtime_host.subprocess.Popen(
            cmd,
            cwd=str(sovits_dir),
            env=env,
            creationflags=flags,
            stdout=log_fp,
            stderr=log_fp,
            stdin=subprocess.DEVNULL,
            **extra_popen_kwargs,
        )
    finally:
        log_fp.close()

    global _SPAWNED_SOVITS_PROC, _SPAWNED_SOVITS_STARTED_AT, _SPAWNED_SOVITS_LAUNCH
    started_at = runtime_host.time.monotonic()
    launch = EngineLaunch(proc, host, port, device, is_half, started_at)
    _SPAWNED_SOVITS_PROC = proc
    _SPAWNED_SOVITS_STARTED_AT = started_at
    _SPAWNED_SOVITS_LAUNCH = launch
    runtime_host._SPAWNED_SOVITS_PROC = proc
    runtime_host._SPAWNED_SOVITS_STARTED_AT = started_at
    runtime_host._SPAWNED_SOVITS_LAUNCH = launch
    if host_sys.platform == "win32" and not getattr(runtime_host, "_WINDOWS_JOB_HANDLE", None):
        runtime_host.setup_windows_job_object()
    assigned = bool(runtime_host.assign_process_to_job(proc))
    (root / "gptsovits.pid").write_text(str(proc.pid), encoding="utf-8")
    return proc, assigned


def _spawn_sovits_process(
    sovits_dir: Path,
    host: str,
    port: int,
    is_half: bool,
    device: str = "cuda",
    *,
    project_root: Path | None = None,
) -> subprocess.Popen:
    """Launches the GPT-SoVITS API daemon with the given precision and device."""
    proc, _ = _launch_sovits_process(
        sovits_dir, host, port, is_half, device=device, project_root=project_root
    )
    return proc


class EngineSupervisor(Protocol):
    """Supervision seam consumed by health/config routes and GET /api/system/engine-status."""

    async def resolve_endpoint(self) -> SovitsEndpoint: ...
    def is_restarting(self, endpoint: SovitsEndpoint) -> bool: ...
    def get_managed_launch(self, endpoint: SovitsEndpoint) -> Any | None: ...
    def get_managed_precision(self, endpoint: SovitsEndpoint | None) -> str | None: ...
    async def probe_endpoint(self, base_url: str) -> bool: ...
    def monotonic(self) -> float: ...
    def engine_log_path(self) -> Path: ...
    def is_port_in_use(self, port: int, host: str = "127.0.0.1") -> bool: ...
    def terminate_process_tree(self, pid: int) -> None: ...
    def get_owned_process(self) -> Any | None: ...
    def clear_owned_process(self, *, project_root: Path | None = None) -> None: ...
    def restart_guard(self, target_url: str): ...
    def spawn_process(
        self,
        sovits_dir: Path,
        host: str,
        port: int,
        is_half: bool,
        *,
        device: str = "cuda",
        project_root: Path | None = None,
    ) -> Any: ...
    def stop_owned_local_engine_for_remote(
        self,
        target_url: str,
        *,
        project_root: Path | None = None,
    ) -> None: ...


class DefaultEngineSupervisor:
    """Production adapter delegating dynamically to the registered runtime host."""

    async def resolve_endpoint(self) -> SovitsEndpoint:
        from galgame2voice.routers import health

        return await health.resolve_effective_sovits_endpoint()

    def is_restarting(self, endpoint: SovitsEndpoint) -> bool:
        from galgame2voice.routers import health

        return health._restarting_endpoint(endpoint)

    def get_managed_launch(self, endpoint: SovitsEndpoint) -> Any | None:
        return _host().get_managed_engine_launch(endpoint)

    def get_managed_precision(self, endpoint: SovitsEndpoint | None) -> str | None:
        return _host().get_managed_engine_precision(endpoint)

    async def probe_endpoint(self, base_url: str) -> bool:
        from galgame2voice.routers import health

        return (await health._probe_gpt_sovits(base_url)).status == "reachable"

    def monotonic(self) -> float:
        return _host().time.monotonic()

    def engine_log_path(self) -> Path:
        from galgame2voice.routers import health

        return health.get_settings().project_root / "logs" / "gpt_sovits.log"

    def is_port_in_use(self, port: int, host: str = "127.0.0.1") -> bool:
        return _host().is_port_in_use(port, host)

    def terminate_process_tree(self, pid: int) -> None:
        _host().terminate_process_tree(pid)

    def get_owned_process(self) -> Any | None:
        return _host()._SPAWNED_SOVITS_PROC

    def clear_owned_process(self, *, project_root: Path | None = None) -> None:
        global _SPAWNED_SOVITS_PROC, _SPAWNED_SOVITS_LAUNCH, _SPAWNED_SOVITS_STARTED_AT
        runtime_host = _host()
        _SPAWNED_SOVITS_PROC = None
        _SPAWNED_SOVITS_LAUNCH = None
        _SPAWNED_SOVITS_STARTED_AT = None
        runtime_host._SPAWNED_SOVITS_PROC = None
        runtime_host._SPAWNED_SOVITS_LAUNCH = None
        runtime_host._SPAWNED_SOVITS_STARTED_AT = None
        root = _resolve_project_root(project_root)
        (root / "gptsovits.pid").unlink(missing_ok=True)

    def restart_guard(self, target_url: str):
        """Coordinate the restart lock and the in-progress target URL."""
        from galgame2voice.routers import health

        return health.engine_restart_guard(target_url)

    def spawn_process(
        self,
        sovits_dir: Path,
        host: str,
        port: int,
        is_half: bool,
        *,
        device: str = "cuda",
        project_root: Path | None = None,
    ) -> Any:
        spawn_fn = _host()._spawn_sovits_process
        if _supports_project_root(spawn_fn):
            return spawn_fn(
                sovits_dir,
                host,
                port,
                is_half,
                device=device,
                project_root=_resolve_project_root(project_root),
            )
        return spawn_fn(sovits_dir, host, port, is_half, device=device)

    def stop_owned_local_engine_for_remote(
        self,
        target_url: str,
        *,
        project_root: Path | None = None,
    ) -> None:
        from galgame2voice.routers import health
        from galgame2voice.services.gpt_sovits_client import get_gpt_sovits_client

        runtime_host = _host()
        health._engine_restart_lock.acquire()
        try:
            if get_gpt_sovits_client().base_url != str(target_url).strip().rstrip("/"):
                return
            proc = runtime_host._SPAWNED_SOVITS_PROC
            if proc is None:
                return
            if proc.poll() is None:
                pid = getattr(proc, "pid", None)
                if pid:
                    logger.info(
                        "Effective GPT-SoVITS endpoint is remote (%s); stopping owned local engine (PID %s) to free resources",
                        target_url,
                        pid,
                    )
                    try:
                        runtime_host.terminate_process_tree(pid)
                        if hasattr(proc, "wait"):
                            proc.wait(timeout=3)
                    except Exception as stop_exc:
                        logger.warning(
                            "Failed while terminating owned local GPT-SoVITS process (PID %s): %s",
                            pid,
                            stop_exc,
                        )
            if runtime_host._SPAWNED_SOVITS_PROC is proc and proc.poll() is not None:
                try:
                    self.clear_owned_process(project_root=project_root)
                except OSError:
                    pass
            elif proc.poll() is None:
                logger.warning(
                    "Owned local GPT-SoVITS process (PID %s) is still running after termination attempt; retaining management references",
                    getattr(proc, "pid", None),
                )
        finally:
            health._engine_restart_lock.release()


def get_engine_supervisor(request: Request | None = None) -> EngineSupervisor:
    """Returns the app-injected EngineSupervisor, falling back to DefaultEngineSupervisor."""
    if request is not None:
        supervisor = getattr(request.app.state, "engine_supervisor", None)
        if supervisor is not None:
            return supervisor
    return DefaultEngineSupervisor()


__all__ = [
    "DefaultEngineSupervisor",
    "EngineSupervisor",
    "assign_process_to_job",
    "build_gpt_sovits_env",
    "get_engine_supervisor",
    "get_managed_engine_launch",
    "get_managed_engine_precision",
    "is_port_in_use",
    "register_runtime_host",
    "setup_windows_job_object",
    "terminate_process_tree",
]
