"""
Galgame2Voice Server Entry Point & Intelligent Auto-Launcher.
Handles automated GPT-SoVITS discovery & startup, browser auto-opening, and robust server lifecycle.
"""

import os
import sys
import time
import socket
import atexit
import signal
import threading
import webbrowser
import subprocess
import argparse
from pathlib import Path
from typing import Any, Optional

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from galgame2voice.utils.precision import (
    read_precision_cache,
    write_precision_cache,
    write_sovits_yaml_is_half,
    write_sovits_yaml_config,
    resolve_initial_is_half,
    resolve_initial_device_and_half,
    read_db_precision,
)
from galgame2voice.utils.hardware import get_gpu_vram_status

# Ensure runtime directories
for d in ["logs", "data", "audio"]:
    (PROJECT_ROOT / d).mkdir(parents=True, exist_ok=True)

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        import ctypes
        ctypes.windll.kernel32.SetConsoleTitleW("Galgame2Voice 启动器")
    except Exception:
        pass

_SPAWNED_SOVITS_PROC = None
_WINDOWS_JOB_HANDLE = None


def setup_windows_job_object():
    """
    Creates a Windows Job Object configured with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE.
    Any child process assigned to this job object will be automatically and atomically
    terminated by the Windows kernel when the parent process exits (even on window close X,
    Ctrl+C, taskkill, or power off).
    """
    global _WINDOWS_JOB_HANDLE
    if sys.platform != "win32":
        return None
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

        kernel32 = ctypes.windll.kernel32
        h_job = kernel32.CreateJobObjectW(None, None)
        if not h_job:
            return None

        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE

        res = kernel32.SetInformationJobObject(
            h_job,
            JobObjectExtendedLimitInformation,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not res:
            kernel32.CloseHandle(h_job)
            return None

        _WINDOWS_JOB_HANDLE = h_job
        return h_job
    except Exception:
        return None


def assign_process_to_job(proc):
    """Assigns a spawned child process to the Windows Job Object."""
    if sys.platform != "win32" or not _WINDOWS_JOB_HANDLE:
        return False
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        if hasattr(proc, "_handle") and proc._handle:
            return bool(kernel32.AssignProcessToJobObject(_WINDOWS_JOB_HANDLE, proc._handle))
    except Exception:
        pass
    return False


def terminate_process_tree(pid: int, timeout: float = 2.0):
    """
    Robustly terminates a process and all of its recursive child processes.
    Uses psutil if available, with Windows taskkill /F /T /PID fallback,
    and POSIX process group / pkill / SIGKILL fallback.
    """
    if not pid or pid <= 1 or pid == os.getpid():
        return

    # 1. Cross-platform tree kill via psutil
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

            gone, alive = psutil.wait_procs(children + [parent], timeout=timeout)
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

    # 2. OS-level fallback if psutil failed or process subtree is still running
    if not psutil_success or still_alive:
        if sys.platform == "win32":
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=3.0,
                    check=False,
                )
            except Exception:
                pass
        else:
            # 3. POSIX fallback: pkill children, kill process group, and send SIGKILL
            try:
                subprocess.run(
                    ["pkill", "-TERM", "-P", str(pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
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


def setup_signal_handlers():
    """
    Registers signal handlers for graceful shutdown on POSIX systems (and Windows where supported).
    Ensures SIGTERM (Docker stop / systemd), SIGHUP (terminal closure), and SIGQUIT clean up
    child processes and runtime PID/port files.
    """
    if threading.current_thread() is not threading.main_thread():
        return

    def _posix_signal_handler(signum, frame):
        cleanup_subprocesses()
        sys.exit(128 + signum)

    signals_to_register = []
    if hasattr(signal, "SIGTERM"):
        signals_to_register.append(signal.SIGTERM)
    if hasattr(signal, "SIGHUP") and sys.platform != "win32":
        signals_to_register.append(signal.SIGHUP)
    if hasattr(signal, "SIGQUIT") and sys.platform != "win32":
        signals_to_register.append(signal.SIGQUIT)

    for sig in signals_to_register:
        try:
            signal.signal(sig, _posix_signal_handler)
        except (ValueError, OSError, AttributeError):
            pass


def cleanup_subprocesses():
    """
    Explicitly stops spawned subprocesses and cleans runtime tracking files.
    Ensures full process tree is terminated via Job Object or fallback tree kill.
    """
    global _SPAWNED_SOVITS_PROC
    pids_to_clean = set()

    if _SPAWNED_SOVITS_PROC is not None:
        try:
            if hasattr(_SPAWNED_SOVITS_PROC, "pid") and _SPAWNED_SOVITS_PROC.pid:
                pids_to_clean.add(_SPAWNED_SOVITS_PROC.pid)
            if _SPAWNED_SOVITS_PROC.poll() is None:
                _SPAWNED_SOVITS_PROC.terminate()
                try:
                    _SPAWNED_SOVITS_PROC.wait(timeout=1.5)
                except subprocess.TimeoutExpired:
                    _SPAWNED_SOVITS_PROC.kill()
        except Exception:
            pass
        _SPAWNED_SOVITS_PROC = None

    # Check pid file in case process was started earlier or object lost
    pid_file = PROJECT_ROOT / "gptsovits.pid"
    if pid_file.exists():
        try:
            raw_pid = pid_file.read_text(encoding="utf-8").strip()
            if raw_pid.isdigit():
                pids_to_clean.add(int(raw_pid))
        except Exception:
            pass

    for pid in pids_to_clean:
        terminate_process_tree(pid)

    try:
        (PROJECT_ROOT / "data" / "active_port.txt").unlink(missing_ok=True)
        (PROJECT_ROOT / "galgame2voice.pid").unlink(missing_ok=True)
        (PROJECT_ROOT / "gptsovits.pid").unlink(missing_ok=True)
    except Exception:
        pass


def is_port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    """Checks if a TCP port is open and listening."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def get_sovits_host_port() -> tuple[str, int]:
    """
    Single source of truth for the engine address: parses GPT_SOVITS_BASE_URL
    (env or config) so launcher, backend client and Docker all agree.
    """
    from urllib.parse import urlparse
    try:
        from galgame2voice.config import get_settings
        parsed = urlparse(get_settings().gpt_sovits_base_url)
        return parsed.hostname or "127.0.0.1", parsed.port or 9880
    except Exception:
        return "127.0.0.1", 9880


def _check_sovits_dir(p: Path) -> Path | None:
    """Checks if p or a nested directory inside p contains api_v2.py."""
    if not p.exists() or not p.is_dir():
        return None
    if (p / "api_v2.py").is_file():
        return p
    # Check nested directory with same name or any subfolder containing api_v2.py
    nested_same = p / p.name
    if (nested_same / "api_v2.py").is_file():
        return nested_same
    try:
        for sub in p.iterdir():
            if sub.is_dir():
                if (sub / "api_v2.py").is_file():
                    return sub
                # Check 2 levels deep (e.g. outer / nested / api_v2.py)
                try:
                    for sub2 in sub.iterdir():
                        if sub2.is_dir() and (sub2 / "api_v2.py").is_file():
                            return sub2
                except (PermissionError, OSError):
                    pass
    except (PermissionError, OSError):
        pass
    return None


def find_gpt_sovits_directory() -> Path | None:
    """Probes candidate paths for local GPT-SoVITS installation (known path first)."""
    import glob

    candidates: list[Path] = []

    # 1. Explicit env overrides
    env_dir = os.environ.get("GPT_SOVITS_DIR")
    if env_dir:
        candidates.append(Path(env_dir))
    # Legacy known-installation hints (kept as low-priority fallbacks only)
    for extra in (os.environ.get("GALGAME2VOICE_DATA_ROOT"),):
        if extra:
            candidates.append(Path(extra))

    # Fast cache lookup to avoid repeating disk searches on every run
    # Strictly use utf-8-sig to strip any invisible Windows PowerShell UTF-8 BOM (\ufeff)
    cache_file = PROJECT_ROOT / "data" / "sovits_dir.txt"
    if cache_file.exists():
        try:
            cached_path_str = cache_file.read_text(encoding="utf-8-sig").strip().strip('"\'')
            if cached_path_str:
                cached_path = Path(cached_path_str)
                if not cached_path.is_absolute():
                    cached_path = (PROJECT_ROOT / cached_path).resolve()
                valid_cached = _check_sovits_dir(cached_path)
                if valid_cached:
                    return valid_cached
        except Exception:
            pass

    for p in candidates:
        valid_dir = _check_sovits_dir(p)
        if valid_dir:
            try:
                cache_file.write_text(str(valid_dir), encoding="utf-8")
            except Exception:
                pass
            return valid_dir

    # 2. Sibling and local project folders
    local_candidates = [
        PROJECT_ROOT.parent / "GPT-SoVITS",
        PROJECT_ROOT / "GPT-SoVITS",
    ]
    try:
        for p in PROJECT_ROOT.parent.glob("GPT-SoVITS*"):
            local_candidates.append(p)
    except Exception:
        pass

    for p in local_candidates:
        valid_dir = _check_sovits_dir(p)
        if valid_dir:
            try:
                cache_file.write_text(str(valid_dir), encoding="utf-8")
            except Exception:
                pass
            return valid_dir

    # 3. Generic drive patterns for other versions / drives (probe existing drives only)
    for drive in ("D", "E", "C", "F"):
        if not os.path.exists(f"{drive}:\\"):
            continue
        for p_str in glob.glob(rf"{drive}:\GPT-SoVITS*\GPT-SoVITS*"):
            p = Path(p_str)
            valid_dir = _check_sovits_dir(p)
            if valid_dir:
                try:
                    cache_file.write_text(str(valid_dir), encoding="utf-8")
                except Exception:
                    pass
                return valid_dir
        for p_str in glob.glob(rf"{drive}:\GPT-SoVITS*"):
            p = Path(p_str)
            valid_dir = _check_sovits_dir(p)
            if valid_dir:
                try:
                    cache_file.write_text(str(valid_dir), encoding="utf-8")
                except Exception:
                    pass
                return valid_dir

    return None


def detect_gpu_capability() -> tuple[bool, str, int | None]:
    """
    Detects GPU compute availability and primary device metadata.
    Re-exported from galgame2voice.utils.hardware for backward compatibility.
    """
    import galgame2voice.utils.hardware as hw
    if "subprocess" in globals() and globals()["subprocess"] is not hw.subprocess:
        orig = hw.subprocess
        try:
            hw.subprocess = globals()["subprocess"]
            return hw.detect_gpu_capability()
        finally:
            hw.subprocess = orig
    return hw.detect_gpu_capability()


def get_system_memory_status() -> tuple[float | None, float | None]:
    """
    Returns (total_ram_gb, available_ram_gb) for the host system.
    Re-exported from galgame2voice.utils.hardware for backward compatibility.
    """
    import galgame2voice.utils.hardware as hw
    return hw.get_system_memory_status()


def build_gpt_sovits_env(sovits_dir: Path, is_half: bool = False) -> dict[str, str]:
    """
    Constructs an isolated process environment for GPT-SoVITS.
    Precision comes exclusively from the calibration store / explicit argument —
    no GPU model name matching. env['is_half'] is enforced without mutating
    third-party files on disk. Defaults to False (FP32) for universal compatibility.
    """
    env = os.environ.copy()
    env["is_half"] = "True" if is_half else "False"

    runtime_scripts = (sovits_dir / "runtime" / "Scripts") if sys.platform == "win32" else (sovits_dir / "runtime" / "bin")
    env["PATH"] = os.pathsep.join([str(sovits_dir / "runtime"), str(runtime_scripts), env.get("PATH", "")])
    env["PYTHONIOENCODING"] = "utf-8"
    env["no_proxy"] = "localhost, 127.0.0.1, ::1"
    env["NO_PROXY"] = "localhost, 127.0.0.1, ::1"
    env["all_proxy"] = ""
    env["ALL_PROXY"] = ""
    return env


def check_python_environment() -> bool:
    """
    Validates Python runtime version and production core dependencies upfront.
    If dependencies are missing, offers automated installation or friendly guidance.
    """
    if sys.version_info < (3, 10):
        print("\n" + "=" * 60)
        print(f"[错误] 当前 Python 版本为 {sys.version.split()[0]}，低于最低要求 (3.10+)")
        print("常见解决办法:")
        print("1. 前往官网下载安装最新 Python: https://www.python.org/downloads/")
        print("2. 安装时请务必勾选底部 'Add python.exe to PATH'")
        print("=" * 60 + "\n")
        return False

    core_deps = [
        ("fastapi", "fastapi"),
        ("uvicorn", "uvicorn"),
        ("aiosqlite", "aiosqlite"),
        ("pydantic", "pydantic"),
        ("httpx", "httpx"),
    ]
    missing = []
    for mod_name, pkg_name in core_deps:
        try:
            __import__(mod_name)
        except ImportError:
            missing.append(pkg_name)

    if missing:
        print(f"\n[提示] 正在检查运行依赖... 发现缺少核心运行库: {', '.join(missing)}")
        req_file = PROJECT_ROOT / "requirements.txt"
        if req_file.exists():
            print(f"[提示] 正在尝试自动安装依赖: {req_file.name} ...")
            try:
                subprocess.run(
                    [sys.executable, "-m", "pip", "install", "-r", str(req_file)],
                    check=True,
                )
                print("[OK] 运行依赖自动安装成功。")
            except Exception as e:
                print("\n" + "=" * 60)
                print(f"[错误] 核心运行依赖安装失败: {e}")
                print("请在命令行手动执行安装:")
                print(f"    {sys.executable} -m pip install -r requirements.txt")
                if sys.platform == "win32":
                    print("    或使用 Windows 自带的 Python 引导器:")
                    print("    py -m pip install -r requirements.txt")
                print("=" * 60 + "\n")
                return False
        else:
            print("\n" + "=" * 60)
            print(f"[错误] 缺少核心运行库: {', '.join(missing)}")
            print("请手动运行: pip install " + " ".join(missing))
            print("=" * 60 + "\n")
            return False

    return True


def get_system_ram_gb() -> tuple[float, float]:
    """
    Returns (total_ram_gb, avail_ram_gb) for the host system.
    Re-exported backward-compatibility shim using galgame2voice.utils.hardware.
    """
    total, avail = get_system_memory_status()
    return total or 0.0, avail or 0.0


def run_hardware_diagnostics() -> dict[str, Any]:
    """
    Comprehensive pre-flight hardware and environment diagnostics.
    Inspects GPU architecture, CUDA availability, and system memory.
    Displays upfront commercial-grade notices and guidance.
    """
    diag: dict[str, Any] = {
        "cuda_available": False,
        "gpu_names": [],
        "total_ram_gb": 0.0,
        "avail_ram_gb": 0.0,
        "has_nvidia": False,
    }

    # 1. System Memory Check
    diag["total_ram_gb"], diag["avail_ram_gb"] = get_system_ram_gb()

    # 2. GPU Detection
    gpu_avail, gpu_name, count = detect_gpu_capability()
    gpu_names = [gpu_name] if gpu_name and gpu_name != "N/A" else []
    diag["gpu_names"] = gpu_names
    diag["cuda_available"] = gpu_avail

    all_gpu_str = " ".join(gpu_names).lower()
    diag["has_nvidia"] = gpu_avail or any(k in all_gpu_str for k in ["nvidia", "geforce", "rtx", "gtx", "quadro", "tesla"])

    # 3. Print upfront commercial-grade notices
    print("\n[环境巡检] 正在诊断系统硬件与运行环境...")

    if diag["has_nvidia"] or diag["cuda_available"]:
        nvidia_names = [g for g in gpu_names if any(k in g.lower() for k in ["nvidia", "geforce", "rtx", "gtx"])]
        detected_name = nvidia_names[0] if nvidia_names else (gpu_names[0] if gpu_names else "NVIDIA GPU")
        print(f"      [硬件就绪] 检测到独立显卡: {detected_name} (已准备 CUDA 加速推理)")
        vram_total, vram_free = get_gpu_vram_status()
        if vram_total is not None:
            diag["vram_total_gb"] = vram_total
            diag["vram_free_gb"] = vram_free
            if vram_total <= 4.1:
                print(f"      [显存提示] 显卡物理显存为 {vram_total:.1f} GB (显存较紧凑)。")
                print("                长时间连续多轮对话或高并发时可能存在显存溢出(OOM)风险。")
                print("                若遇显存不足，可在控制面板或通过 `启动.bat --cpu` 启用「CPU 稳定模式」（依托大内存，彻底杜绝崩溃）。")
            else:
                print(f"      [显存就绪] 显存容量: {vram_total:.1f} GB (当前空闲约 {vram_free:.1f} GB)")

        cached = read_precision_cache(PROJECT_ROOT)
        if cached and cached.get("device") == "cpu":
            print("      [推理模式] 已配置为 CPU 稳定模式推理 (免显存占用，利用大内存防爆显存)。")
        elif cached and cached.get("is_half") is False:
            print("      [精度校准] 已缓存校准结果: 此设备使用 FP32 单精度推理 (保证发声正常)。")
        else:
            print("      [精度校准] 引擎就绪后将自动校准 FP16/FP32 精度，无需手动配置。")
    else:
        print("      [硬件提示] 未检测到兼容的 NVIDIA 独立显卡或 CUDA 推理环境。")
        print("                系统将以 CPU 兼容模式运行。首次模型加载与推理耗时较长属于正常现象，建议在配置 NVIDIA 显卡的电脑上使用以获得最佳体验。")

    if diag["avail_ram_gb"] > 0:
        if diag["avail_ram_gb"] < 1.8:
            print(f"      [内存提示] 当前系统空闲物理内存约 {diag['avail_ram_gb']:.1f} GB (总计 {diag['total_ram_gb']:.1f} GB)。建议关闭高内存占用的后台应用以确保语音合成流畅。")
        elif diag["total_ram_gb"] < 4.0:
            print(f"      [内存提示] 本机物理内存较小 ({diag['total_ram_gb']:.1f} GB)，若并发较高可能受限，建议配置虚拟内存。")
        else:
            print(f"      [内存就绪] 系统物理内存充裕: 空闲 {diag['avail_ram_gb']:.1f} GB / 总计 {diag['total_ram_gb']:.1f} GB")

    print("      [巡检通过] 运行环境诊断完毕。\n")
    return diag


def check_system_memory():
    """Checks free physical memory and prints advisory if system RAM is constrained."""
    if sys.platform != "win32":
        return
    _, free_gb = get_system_ram_gb()
    if 0 < free_gb < 1.8:
        print(f"      [内存提示] 当前系统空闲物理内存约 {free_gb:.1f} GB。建议关闭高内存占用的后台应用以确保语音合成流畅。")


def _spawn_sovits_process(
    sovits_dir: Path,
    host: str,
    port: int,
    is_half: bool,
    device: str = "cuda",
) -> subprocess.Popen:
    """Launches the GPT-SoVITS API daemon with the given precision and device, binding to launcher lifecycle."""
    runtime_candidates = (
        (sovits_dir / "runtime" / "python.exe", sovits_dir / "runtime" / "python",
         sovits_dir / "runtime" / "python" / "bin" / "python3")
        if sys.platform == "win32"
        else (sovits_dir / "runtime" / "python" / "bin" / "python3", sovits_dir / "runtime" / "python")
    )
    python_exe = next((c for c in runtime_candidates if c.is_file()), None)
    if python_exe is None:
        print("      [提示] 该 GPT-SoVITS 集成包缺少内置 runtime/python 解释器；")
        print("             将改用当前 Python 启动引擎，若报缺少 GPT-SoVITS 依赖，")
        print("             请下载官方完整集成包 (含 runtime 目录) 或手动安装其 requirements。")
        python_exe = Path(sys.executable)

    synced_yaml = write_sovits_yaml_config(sovits_dir, is_half, device=device)
    config_arg = "GPT_SoVITS/configs/tts_infer.yaml"
    if synced_yaml:
        try:
            config_arg = str(synced_yaml.relative_to(sovits_dir))
        except ValueError:
            config_arg = str(synced_yaml)

    cmd = [
        str(python_exe),
        "-I",
        "api_v2.py",
        "-a", host,
        "-p", str(port),
        "-c", config_arg,
    ]

    log_file = PROJECT_ROOT / "logs" / "gpt_sovits.log"
    # 简单轮转：超过 10MB 归档为 .old（引擎日志为 append 模式，无内置轮转）
    try:
        if log_file.exists() and log_file.stat().st_size > 10 * 1024 * 1024:
            old_file = log_file.with_suffix(".old.log")
            if old_file.exists():
                old_file.unlink()
            log_file.rename(old_file)
    except OSError:
        pass
    log_fp = open(log_file, "a", encoding="utf-8")
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    env = build_gpt_sovits_env(sovits_dir, is_half=is_half)
    extra_popen_kwargs = {}
    if sys.platform != "win32":
        extra_popen_kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(
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
    global _SPAWNED_SOVITS_PROC
    _SPAWNED_SOVITS_PROC = proc
    assign_process_to_job(proc)
    (PROJECT_ROOT / "gptsovits.pid").write_text(str(proc.pid), encoding="utf-8")
    print(f"      [OK] 已在后台启动 GPT-SoVITS (PID: {proc.pid}, {'FP16' if is_half else 'FP32'})，进程与主窗口已安全绑定联动")
    return proc


def _probe_synth_peak(host: str, port: int, timeout: float = 90.0) -> Optional[float]:
    """
    Synthesizes one short test sentence via the engine's /tts endpoint and returns
    the WAV peak amplitude (0.0~1.0). Returns None when the probe is INCONCLUSIVE
    (timeout / connection / HTTP error) — None never means 'silent'.
    """
    import urllib.parse
    import urllib.request

    try:
        from galgame2voice.services.gpt_sovits_client import (
            _BUNDLED_REF_AUDIO,
            _BUNDLED_REF_TEXT,
            wav_peak_amplitude,
        )
    except Exception:
        return None

    ref_audio = _BUNDLED_REF_AUDIO if _BUNDLED_REF_AUDIO.is_file() else PROJECT_ROOT / "audio" / "nat002_032.ogg"
    if not ref_audio.is_file():
        return None

    params = urllib.parse.urlencode({
        "text": "テスト、聞こえていますか。",
        "text_lang": "ja",
        "ref_audio_path": str(ref_audio),
        "prompt_text": _BUNDLED_REF_TEXT,
        "prompt_lang": "ja",
    })
    url = f"http://{host}:{port}/tts?{params}"
    # 直接连接，绕过系统代理（本机回环地址不应走代理）
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            audio = resp.read()
    except Exception:
        return None
    if not audio:
        return None
    return wav_peak_amplitude(audio)


def calibrate_engine_precision(
    sovits_dir: Path,
    is_half: bool,
    probe_fn,
    restart_fn,
) -> tuple[bool, bool]:
    """
    Evidence-based precision calibration: the engine proves which precision works
    on THIS machine, no GPU model names involved.

    Decision table (probe returns peak amplitude, or None = inconclusive):
      peak > 0     -> current precision verified; cache it.
      peak == 0    -> current precision produces silence:
          FP16     -> restart with FP32 and re-probe: audible -> FP32 verified + cached;
                      still silent/inconclusive -> keep FP32, no cache (deeper issue).
          FP32     -> keep FP32, no cache (deeper issue than precision).
      None         -> inconclusive; keep current setting, no cache.

    Returns (final_is_half, calibrated) where calibrated means a verified value was cached.
    """
    peak = probe_fn()
    if peak is None:
        print("      [精度校准] 探针未完成 (超时/网络)，跳过本次校准，沿用当前精度。")
        return is_half, False
    if peak > 0:
        write_precision_cache(PROJECT_ROOT, str(sovits_dir), is_half)
        return is_half, True

    # Silence on FP16: restart with FP32 and verify.
    if is_half:
        print("      [精度校准] FP16 探针结果为纯静音 —— 此设备半精度推理有缺陷，正在以 FP32 重启引擎...")
        new_proc = restart_fn(False)
        if new_proc is None:
            print("      [WARN] FP32 引擎重启失败，请查看 logs/gpt_sovits.log。")
            return False, False
        peak_fp32 = probe_fn()
        if peak_fp32 is not None and peak_fp32 > 0:
            write_precision_cache(PROJECT_ROOT, str(sovits_dir), False)
            print("      [OK] 精度校准完成: 此设备使用 FP32 单精度，语音输出正常。结果已缓存，下次启动直接生效。")
            return False, True
        print("      [WARN] FP32 探针仍为静音或未完成 —— 问题可能不在精度，请查看 logs/gpt_sovits.log。")
        return False, False

    # Silence on FP32: precision is not the culprit.
    print("      [WARN] FP32 探针为纯静音 —— 问题可能不在精度，请查看 logs/gpt_sovits.log。")
    return is_half, False


def _calibrate_precision_after_ready(
    sovits_dir: Path,
    host: str,
    port: int,
    is_half: bool,
    precision_source: str,
    device: str = "cuda",
) -> None:
    """Runs the calibration probe in the readiness-monitor thread once the engine answers."""
    if precision_source != "default" or device == "cpu":
        return  # env override or CPU mode needs no probe

    def probe() -> Optional[float]:
        return _probe_synth_peak(host, port)

    def restart(new_is_half: bool):
        global _SPAWNED_SOVITS_PROC
        old = _SPAWNED_SOVITS_PROC
        if old is not None:
            try:
                terminate_process_tree(old.pid)
            except Exception:
                pass
        # Wait for the old engine to release the port before relaunching.
        for _ in range(20):
            time.sleep(0.5)
            if not is_port_in_use(port, host):
                break
        try:
            return _spawn_sovits_process(sovits_dir, host, port, new_is_half)
        except Exception as exc:
            print(f"      [WARN] 引擎重启失败: {exc}")
            return None

    # Give the model a moment to finish loading weights after port bind.
    time.sleep(2.0)
    _, calibrated = calibrate_engine_precision(sovits_dir, is_half, probe, restart)
    if calibrated:
        cached_half = read_precision_cache(PROJECT_ROOT)
        if cached_half is not None and cached_half.get("is_half") is False:
            print("      [精度校准] 此设备已记录为 FP32 模式 (FP16 半精度输出纯静音)。")


def ensure_gpt_sovits_running(
    fp16: bool = False,
    fp32: bool = False,
    cpu: bool = False,
    precision: str | None = None,
):
    """
    Spawns the local GPT-SoVITS API daemon if it is not already running.
    Runs non-blocking parallel readiness checking in the background.
    """
    sovits_host, sovits_port = get_sovits_host_port()
    print(f"[1/2] 正在检测 GPT-SoVITS 语音推理引擎 ({sovits_host}:{sovits_port})...")
    if is_port_in_use(sovits_port, sovits_host) or (sovits_port != 9880 and is_port_in_use(9880)):
        print("      [OK] GPT-SoVITS 语音引擎已在运行")
        return
    sovits_host = "127.0.0.1"
    sovits_port = 9880

    if is_port_in_use(sovits_port, sovits_host):
        print(f"      [OK] GPT-SoVITS 服务已在运行中 (http://{sovits_host}:{sovits_port}/)")
        return

    sovits_dir = find_gpt_sovits_directory()
    if not sovits_dir:
        print("      [提示] 未自动定位到 GPT-SoVITS 目录，若已在其他终端运行请忽略。")
        return

    print(f"      [..] 定位到 GPT-SoVITS: {sovits_dir}")
    print("      [..] 正在后台拉起 GPT-SoVITS API 引擎...")

    # Precision and device resolution order:
    # 1. CLI explicit flags: --cpu, --fp16, --fp32, or --precision
    # 2. Environment variable: GPT_SOVITS_PRECISION / GPT_SOVITS_DEVICE
    # 3. Saved setting in SQLite database (SettingsInDB)
    # 4. Verified calibration cache: data/precision.json
    # 5. Existing engine config: tts_infer.yaml
    # 6. Default fallback: CUDA FP16 if GPU present, else CPU
    prec_opt = (precision or "").lower()
    if cpu or prec_opt == "cpu":
        device = "cpu"
        is_half = False
        precision_source = "cli"
        print("      [推理模式] 已指定 --cpu 稳定模式运行 (免显存占用，利用大内存防爆显存)。")
    elif fp16 or prec_opt == "fp16":
        device = "cuda"
        is_half = True
        precision_source = "cli"
        print("      [推理精度] 已指定 --fp16 半精度模式运行。")
    elif fp32 or prec_opt == "fp32":
        device = "cuda"
        is_half = False
        precision_source = "cli"
        print("      [推理精度] 已指定 --fp32 单精度模式运行。")
    else:
        device, is_half, precision_source = resolve_initial_device_and_half(PROJECT_ROOT, sovits_dir)
        if device == "cpu":
            prec_str = "CPU 稳定模式"
        else:
            prec_str = "FP16 半精度" if is_half else "FP32 单精度"
        if precision_source == "env":
            print(f"      [推理精度] 已通过环境变量手动指定 {prec_str}。")
        elif precision_source == "db":
            print(f"      [推理精度] 使用控制台保存的配置: {prec_str} (来源: SQLite数据库设置)")
        elif precision_source == "cache":
            print(f"      [推理精度] 使用已验证的校准结果: {prec_str} (来源: data/precision.json)")
        elif precision_source == "yaml":
            print(f"      [推理精度] 使用现有引擎配置文件: {prec_str} (来源: tts_infer.yaml)")
        else:
            print("      [推理精度] 未指定固定精度，进入自动校准模式 (初始 FP16，就绪后验证发声)。")
    try:
        proc = _spawn_sovits_process(sovits_dir, sovits_host, sovits_port, is_half, device=device)

        # Non-blocking parallel readiness monitor (bounded: 120s, engine logs written to gpt_sovits.log)
        print("      [..] GPT-SoVITS 正在后台加载模型 (最长 120 秒，伴侣服务先行启动)...")

        def _wait_for_sovits_readiness_worker():
            for i in range(240):
                time.sleep(0.5)
                if is_port_in_use(sovits_port, sovits_host):
                    print(f"\n      [OK] GPT-SoVITS 语音引擎已就绪 (http://{sovits_host}:{sovits_port}/)")
                    _calibrate_precision_after_ready(sovits_dir, sovits_host, sovits_port, is_half, precision_source, device=device)
                    return
                if proc and proc.poll() is not None:
                    print(f"\n      [WARN] GPT-SoVITS 异常退出 (退出码: {proc.returncode})，详见 logs/gpt_sovits.log")
                    return
            print("\n      [提示] GPT-SoVITS 模型仍在后台加载中 (详见 logs/gpt_sovits.log)")

        monitor_thread = threading.Thread(
            target=_wait_for_sovits_readiness_worker,
            daemon=True,
            name="sovits-readiness-monitor",
        )
        monitor_thread.start()
    except Exception as e:
        print(f"      [WARN] 启动 GPT-SoVITS 失败: {e}")



def find_available_port(preferred_port: int = 8080, host: str = "127.0.0.1") -> int:
    """Finds a bindable TCP port starting from preferred_port with graceful fallbacks."""
    candidates = [preferred_port, 8081, 8082, 8085, 8088, 8888, 18080, 28080]
    seen = set()
    unique_candidates = [p for p in candidates if not (p in seen or seen.add(p))]

    for p in unique_candidates:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind((host, p))
                return p
        except OSError:
            continue

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


def auto_open_browser(port: int = 8080, host: str = "127.0.0.1"):
    """Background thread that waits for the HTTP service to answer, then launches the browser."""
    import http.client

    def _runner():
        probe_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
        display_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
        url = f"http://{display_host}:{port}/"
        # Fast local probe directly via raw loopback socket bypassing OS proxy delay
        for _ in range(30):
            time.sleep(0.3)
            try:
                conn = http.client.HTTPConnection(probe_host, port, timeout=0.2)
                conn.request("GET", "/api/health")
                resp = conn.getresponse()
                if resp.status == 200:
                    conn.close()
                    webbrowser.open(url)
                    break
                conn.close()
            except Exception:
                continue

    t = threading.Thread(target=_runner, daemon=True, name="browser-launcher")
    t.start()



def parse_args(args: list[str] | None = None) -> argparse.Namespace:
    """Parses CLI flags for the Galgame2Voice server launcher."""
    parser = argparse.ArgumentParser(
        description="Galgame2Voice Server Entry Point & Enterprise Auto-Launcher",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    env_port = 8080
    if os.environ.get("GALGAME_PORT"):
        try:
            env_port = int(os.environ["GALGAME_PORT"])
        except ValueError:
            pass
    env_host = os.environ.get("GALGAME_HOST", "127.0.0.1")
    env_no_browser = os.environ.get("GALGAME_NO_BROWSER", "").lower() in ("1", "true", "yes")

    parser.add_argument(
        "--host",
        type=str,
        default=env_host,
        help="Host interface to bind the server on",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=env_port,
        help="Port to listen on",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        default=env_no_browser,
        help="Suppress automatic browser launch on server startup",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Run pre-flight environment & hardware diagnostics, print report, and exit cleanly (exit code 0 if healthy, 1 if fatal)",
    )
    parser.add_argument(
        "--precision",
        choices=["fp16", "fp32", "cpu", "auto"],
        default=None,
        help="Inference precision/mode: fp16 (half-precision), fp32 (single-precision), cpu (safe host-RAM mode), or auto",
    )
    parser.add_argument(
        "--fp16",
        action="store_true",
        default=False,
        help="Enable half-precision (FP16) inference (alias for --precision fp16)",
    )
    parser.add_argument(
        "--fp32",
        action="store_true",
        default=False,
        help="Force single-precision (FP32) inference (alias for --precision fp32)",
    )
    parser.add_argument(
        "--cpu",
        action="store_true",
        default=False,
        help="Force CPU inference mode (bypasses GPU/VRAM to eliminate OOM risk and driver crashes)",
    )
    return parser.parse_args(args)


def main(args: list[str] | None = None):
    parsed = parse_args(args)

    # Step 0: Pre-Flight Environment & Hardware Diagnostics
    if not check_python_environment():
        if parsed.check_only:
            print("\n[巡检失败] Python 运行环境或核心运行库校验未通过。")
        sys.exit(1)

    try:
        run_hardware_diagnostics()
    except Exception as e:
        print(f"\n[错误] 硬件巡检诊断异常: {e}")
        sys.exit(1)

    if parsed.check_only:
        print("[巡检通过] 所有前置依赖与硬件诊断均已就绪，系统运行状态正常。")
        sys.exit(0)

    setup_windows_job_object()
    setup_signal_handlers()
    atexit.register(cleanup_subprocesses)

    try:
        # Step 1: GPT-SoVITS
        ensure_gpt_sovits_running(
            fp16=getattr(parsed, "fp16", False),
            fp32=getattr(parsed, "fp32", False),
            cpu=getattr(parsed, "cpu", False),
            precision=getattr(parsed, "precision", None),
        )

        # Step 2: Determine & Probe Port
        preferred_port = parsed.port
        bind_host = parsed.host
        active_port = find_available_port(preferred_port, host=bind_host)
        if active_port != preferred_port:
            print(f"[提示] 默认端口 {preferred_port} 无法绑定 (可能被系统代理或其他程序占用)，已自动切换至可用端口: {active_port}")

        # Save active port & PID for clean shutdown and downstream configuration
        os.environ["GALGAME_PORT"] = str(active_port)
        os.environ["PORT"] = str(active_port)
        os.environ["GALGAME_HOST"] = bind_host
        os.environ["HOST"] = bind_host
        try:
            (PROJECT_ROOT / "data" / "active_port.txt").write_text(str(active_port), encoding="utf-8")
            (PROJECT_ROOT / "galgame2voice.pid").write_text(str(os.getpid()), encoding="utf-8")
        except Exception:
            pass

        # Step 3: Auto Open Browser
        display_host = "127.0.0.1" if bind_host in ("0.0.0.0", "::") else bind_host
        print(f"[2/2] 正在启动 Galgame2Voice 伴侣服务 ({bind_host}:{active_port})...")
        if not parsed.no_browser:
            auto_open_browser(active_port, host=bind_host)
            print(f"      [OK] 正在打开浏览器: http://{display_host}:{active_port}/")
        else:
            print(f"      [提示] 已开启 --no-browser，跳过自动打开浏览器。访问地址: http://{display_host}:{active_port}/")
        print("      关闭此窗口即可退出并释放显存。")
        try:
            import uvicorn
            uvicorn.run("galgame2voice.main:app", host=bind_host, port=active_port, log_level="info")
        except (KeyboardInterrupt, SystemExit):
            print("\n[提示] 服务已正常停止，正在释放资源...")
        except Exception as e:
            print(f"\n[错误] 服务运行异常: {e}")
            sys.exit(1)
    finally:
        cleanup_subprocesses()
    sys.exit(0)


if __name__ == "__main__":
    main()

