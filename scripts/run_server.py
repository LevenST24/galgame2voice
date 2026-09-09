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
from pathlib import Path
from typing import Any

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

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


def is_turing_tu116_tu117_gpu(gpu_name_override: str | None = None) -> bool:
    """
    Detects if the system has an NVIDIA Turing TU116 or TU117 architecture GPU.
    Affected models: GeForce MX450, MX550, GTX 1650, GTX 1660, GTX 1630, etc.
    On these GPUs, FP16 half-precision inference causes PyTorch to produce NaN and zero-amplitude (silent) audio.
    """
    target_keywords = ["mx450", "mx550", "1650", "1660", "1630", "tu117", "tu116"]
    if gpu_name_override is not None:
        return any(k in gpu_name_override.lower() for k in target_keywords)

    gpu_names = []

    # 1. PyTorch CUDA inspection if torch is available
    try:
        import torch
        if torch.cuda.is_available():
            for i in range(torch.cuda.device_count()):
                gpu_names.append(torch.cuda.get_device_name(i).lower())
    except Exception:
        pass

    # 2. nvidia-smi tool inspection
    if not gpu_names:
        try:
            out = subprocess.check_output(
                ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                text=True, stderr=subprocess.DEVNULL, timeout=2.0
            )
            gpu_names.extend([line.strip().lower() for line in out.splitlines() if line.strip()])
        except Exception:
            pass

    # 3. Windows WMI / CIM query
    if not gpu_names and sys.platform == "win32":
        try:
            out = subprocess.check_output(
                ["wmic", "path", "win32_VideoController", "get", "name"],
                text=True, stderr=subprocess.DEVNULL, timeout=2.0
            )
            gpu_names.extend([line.strip().lower() for line in out.splitlines() if line.strip() and line.strip().lower() != "name"])
        except Exception:
            pass

        if not gpu_names:
            try:
                out = subprocess.check_output(
                    ["powershell", "-NoProfile", "-Command", "(Get-CimInstance Win32_VideoController).Name"],
                    text=True, stderr=subprocess.DEVNULL, timeout=3.0
                )
                gpu_names.extend([line.strip().lower() for line in out.splitlines() if line.strip()])
            except Exception:
                pass

    all_names_str = " ".join(gpu_names).lower()
    target_keywords = ["mx450", "mx550", "1650", "1660", "1630", "tu117", "tu116"]
    return any(k in all_names_str for k in target_keywords)


def patch_sovits_precision_config(sovits_dir: Path, force_fp32: bool = False) -> None:
    """
    Deprecated / No-op in commercial release.
    External third-party GPT-SoVITS files are NEVER mutated on disk.
    FP32 single precision is enforced cleanly and strictly through process environment
    isolation (env['is_half'] = 'False') passed to subprocess.Popen.
    """
    return


def build_gpt_sovits_env(sovits_dir: Path, is_turing: bool | None = None) -> dict[str, str]:
    """
    Constructs an isolated process environment for GPT-SoVITS.
    Enforces precision via env['is_half'] without mutating third-party files on disk.
    """
    if is_turing is None:
        is_turing = is_turing_tu116_tu117_gpu()

    env = os.environ.copy()
    if is_turing:
        env["is_half"] = "False"
    elif "is_half" not in env:
        env["is_half"] = "True"

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
                print("=" * 60 + "\n")
                return False
        else:
            print("\n" + "=" * 60)
            print(f"[错误] 缺少核心运行库: {', '.join(missing)}")
            print("请手动运行: pip install " + " ".join(missing))
            print("=" * 60 + "\n")
            return False

    return True


if sys.platform == "win32":
    import ctypes

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]


def get_system_ram_gb() -> tuple[float, float]:
    """Returns (total_ram_gb, avail_ram_gb) for the host system."""
    try:
        if sys.platform == "win32":
            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                return stat.ullTotalPhys / (1024 ** 3), stat.ullAvailPhys / (1024 ** 3)
        else:
            import psutil
            vm = psutil.virtual_memory()
            return vm.total / (1024 ** 3), vm.available / (1024 ** 3)
    except Exception:
        pass
    return 0.0, 0.0


def run_hardware_diagnostics() -> dict[str, Any]:
    """
    Comprehensive pre-flight hardware and environment diagnostics.
    Inspects GPU architecture, CUDA availability, and system memory.
    Displays upfront commercial-grade notices and guidance.
    """
    diag: dict[str, Any] = {
        "is_turing": False,
        "cuda_available": False,
        "gpu_names": [],
        "total_ram_gb": 0.0,
        "avail_ram_gb": 0.0,
        "has_nvidia": False,
    }

    # 1. System Memory Check
    diag["total_ram_gb"], diag["avail_ram_gb"] = get_system_ram_gb()

    # 2. GPU Detection
    gpu_names = []
    cuda_available = False
    try:
        import torch
        cuda_available = torch.cuda.is_available()
        if cuda_available:
            for i in range(torch.cuda.device_count()):
                gpu_names.append(torch.cuda.get_device_name(i))
    except Exception:
        pass

    if not gpu_names:
        try:
            out = subprocess.check_output(
                ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                text=True, stderr=subprocess.DEVNULL, timeout=2.0
            )
            gpu_names.extend([line.strip() for line in out.splitlines() if line.strip()])
            if gpu_names:
                cuda_available = True
        except Exception:
            pass

    if not gpu_names and sys.platform == "win32":
        try:
            out = subprocess.check_output(
                ["powershell", "-NoProfile", "-Command", "(Get-CimInstance Win32_VideoController).Name"],
                text=True, stderr=subprocess.DEVNULL, timeout=3.0
            )
            gpu_names.extend([line.strip() for line in out.splitlines() if line.strip()])
        except Exception:
            pass

    diag["gpu_names"] = gpu_names
    diag["cuda_available"] = cuda_available

    all_gpu_str = " ".join(gpu_names).lower()
    diag["has_nvidia"] = any(k in all_gpu_str for k in ["nvidia", "geforce", "rtx", "gtx", "quadro", "tesla"])
    diag["is_turing"] = is_turing_tu116_tu117_gpu()

    # 3. Print upfront commercial-grade notices
    print("\n[环境巡检] 正在诊断系统硬件与运行环境...")

    if diag["is_turing"]:
        print("      [硬件优化] 检测到 NVIDIA MX / 16 系列显卡，已自动开启单精度 (FP32) 兼容模式，保证发声正常。")
    elif diag["has_nvidia"] or diag["cuda_available"]:
        nvidia_names = [g for g in gpu_names if any(k in g.lower() for k in ["nvidia", "geforce", "rtx", "gtx"])]
        detected_name = nvidia_names[0] if nvidia_names else (gpu_names[0] if gpu_names else "NVIDIA GPU")
        print(f"      [硬件就绪] 检测到独立显卡: {detected_name} (已准备 CUDA 加速推理)")
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


def ensure_gpt_sovits_running():
    """Checks port 9880; if not running, discovers and launches GPT-SoVITS API daemon."""
    print("[1/2] 正在检测 GPT-SoVITS 语音推理引擎 (端口 9880)...")
    if is_port_in_use(9880):
        print("      [OK] GPT-SoVITS 语音引擎已在运行")
        print("      [注意] 引擎为外部启动，本启动器无法核实其精度 (FP16/FP32) 配置；")
        print("             若语音全程无声，请关闭旧的 GPT-SoVITS 进程后重新运行本启动器。")
        return

    check_system_memory()
    sovits_dir = find_gpt_sovits_directory()
    if not sovits_dir:
        print("      [提示] 未自动定位到 GPT-SoVITS 目录，若已在其他终端运行请忽略。")
        return

    print(f"      [..] 定位到 GPT-SoVITS: {sovits_dir}")
    print("      [..] 正在后台拉起 GPT-SoVITS API 引擎...")

    python_exe = sovits_dir / "runtime" / "python.exe"
    if not python_exe.exists():
        python_exe = Path(sys.executable)

    cmd = [
        str(python_exe),
        "-I",
        "api_v2.py",
        "-a", "127.0.0.1",
        "-p", "9880",
        "-c", "GPT_SoVITS/configs/tts_infer.yaml",
    ]

    is_turing = is_turing_tu116_tu117_gpu()
    try:
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
        env = build_gpt_sovits_env(sovits_dir, is_turing=is_turing)
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
        _SPAWNED_SOVITS_PROC = proc
        assign_process_to_job(proc)
        (PROJECT_ROOT / "gptsovits.pid").write_text(str(proc.pid), encoding="utf-8")
        print(f"      [OK] 已在后台启动 GPT-SoVITS (PID: {proc.pid})，进程与主窗口已安全绑定联动")

        # Non-blocking parallel readiness monitor (bounded: 120s, engine logs written to gpt_sovits.log)
        print("      [..] GPT-SoVITS 正在后台加载模型入显存 (最长 120 秒，伴侣服务先行启动)...")

        def _wait_for_sovits_readiness_worker():
            for i in range(240):
                time.sleep(0.5)
                if is_port_in_use(9880):
                    print("\n      [OK] GPT-SoVITS 语音引擎已就绪 (http://127.0.0.1:9880/)")
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


def auto_open_browser(port: int = 8080):
    """Background thread that waits for the HTTP service to answer, then launches the browser."""
    import http.client

    def _runner():
        url = f"http://127.0.0.1:{port}/"
        # Fast local probe directly via raw loopback socket bypassing OS proxy delay
        for _ in range(30):
            time.sleep(0.3)
            try:
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=0.2)
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



def main():
    if "--help" in sys.argv or "-h" in sys.argv:
        print("Galgame2Voice Server Launcher")
        print("Usage: python scripts/run_server.py [--port PORT]")
        sys.exit(0)

    # Step 0: Pre-Flight Environment & Hardware Diagnostics
    if not check_python_environment():
        sys.exit(1)
    run_hardware_diagnostics()

    setup_windows_job_object()
    setup_signal_handlers()
    atexit.register(cleanup_subprocesses)

    try:
        # Step 1: GPT-SoVITS
        ensure_gpt_sovits_running()

        # Step 2: Determine & Probe Port
        preferred_port = 8080
        if os.environ.get("GALGAME_PORT"):
            try:
                preferred_port = int(os.environ["GALGAME_PORT"])
            except ValueError:
                pass

        # Parse CLI --port if provided
        for i, arg in enumerate(sys.argv):
            if arg == "--port" and i + 1 < len(sys.argv):
                try:
                    preferred_port = int(sys.argv[i + 1])
                except ValueError:
                    pass

        active_port = find_available_port(preferred_port)
        if active_port != preferred_port:
            print(f"[提示] 默认端口 {preferred_port} 无法绑定 (可能被系统代理或其他程序占用)，已自动切换至可用端口: {active_port}")

        # Save active port & PID for clean shutdown
        try:
            (PROJECT_ROOT / "data" / "active_port.txt").write_text(str(active_port), encoding="utf-8")
            (PROJECT_ROOT / "galgame2voice.pid").write_text(str(os.getpid()), encoding="utf-8")
        except Exception:
            pass

        # Step 3: Auto Open Browser
        print(f"[2/2] 正在启动 Galgame2Voice 伴侣服务 (端口 {active_port})...")
        auto_open_browser(active_port)
        print(f"      [OK] 正在打开浏览器: http://127.0.0.1:{active_port}/")
        print("      关闭此窗口即可退出并释放显存。")
        try:
            import uvicorn
            uvicorn.run("galgame2voice.main:app", host="127.0.0.1", port=active_port, log_level="info")
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
