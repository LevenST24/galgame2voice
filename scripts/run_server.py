"""
Galgame2Voice Server Entry Point & Intelligent Auto-Launcher.
Handles automated GPT-SoVITS discovery & startup, browser auto-opening, and robust server lifecycle.
"""

import os
import sys
import time
import socket
import atexit
import threading
import webbrowser
import subprocess
from pathlib import Path

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
    except Exception:
        pass

# ANSI Colors
CYAN = "\033[96m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
MAGENTA = "\033[95m"
BOLD = "\033[1m"
RESET = "\033[0m"

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


def cleanup_subprocesses():
    """Explicitly stops spawned subprocesses and cleans runtime tracking files."""
    global _SPAWNED_SOVITS_PROC
    if _SPAWNED_SOVITS_PROC is not None:
        try:
            if _SPAWNED_SOVITS_PROC.poll() is None:
                _SPAWNED_SOVITS_PROC.terminate()
                try:
                    _SPAWNED_SOVITS_PROC.wait(timeout=1.5)
                except subprocess.TimeoutExpired:
                    _SPAWNED_SOVITS_PROC.kill()
        except Exception:
            pass
        _SPAWNED_SOVITS_PROC = None

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


def find_gpt_sovits_directory() -> Path | None:
    """Probes candidate paths for local GPT-SoVITS installation (known path first)."""
    import glob

    candidates: list[Path] = []

    # 1. Explicit env override
    env_dir = os.environ.get("GPT_SOVITS_DIR")
    if env_dir:
        candidates.append(Path(env_dir))

    # 2. User's known installation (nested layout) — probed first for speed.
    candidates.extend([
        Path(r"E:\GPT-SoVITS-v2pro-20250604\GPT-SoVITS-v2pro-20250604"),
        Path(r"E:\GPT-SoVITS-v2pro-20250604"),
    ])

    # 3. Generic drive patterns for other versions / drives.
    for drive in ("D", "E", "C", "F"):
        candidates.extend(Path(p) for p in glob.glob(rf"{drive}:\GPT-SoVITS*\GPT-SoVITS*"))
        candidates.extend(Path(p) for p in glob.glob(rf"{drive}:\GPT-SoVITS*"))
    candidates.append(PROJECT_ROOT.parent / "GPT-SoVITS")

    for p in candidates:
        if (p / "api_v2.py").exists():
            return p
    return None


def check_system_memory():
    """Checks free physical memory and prints advisory if system RAM is constrained."""
    if sys.platform != "win32":
        return
    try:
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
        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
            free_gb = stat.ullAvailPhys / (1024 ** 3)
            if free_gb < 1.8:
                print(f"      {YELLOW}[内存提示]{RESET} 当前系统空闲物理内存约 {free_gb:.1f} GB。建议关闭高内存占用的后台应用以确保语音合成流畅。")
    except Exception:
        pass


def ensure_gpt_sovits_running():
    """Checks port 9880; if not running, discovers and launches GPT-SoVITS API daemon."""
    print(f"{CYAN}[1/2] 正在检测 GPT-SoVITS 语音推理引擎 (端口 9880)...{RESET}")
    if is_port_in_use(9880):
        print(f"      {GREEN}[OK]{RESET} GPT-SoVITS 语音引擎已在运行")
        return

    check_system_memory()
    sovits_dir = find_gpt_sovits_directory()
    if not sovits_dir:
        print(f"      {YELLOW}[提示]{RESET} 未自动定位到 GPT-SoVITS 目录，若已在其他终端运行请忽略。")
        return

    print(f"      {YELLOW}[..]{RESET} 定位到 GPT-SoVITS: {sovits_dir}")
    print(f"      {YELLOW}[..]{RESET} 正在后台拉起 GPT-SoVITS API 引擎...")

    python_exe = sovits_dir / "runtime" / "python.exe"
    if not python_exe.exists():
        python_exe = Path(sys.executable)

    cmd = [
        str(python_exe),
        "api_v2.py",
        "-a", "127.0.0.1",
        "-p", "9880",
        "-c", "GPT_SoVITS/configs/tts_infer.yaml",
    ]

    try:
        log_file = PROJECT_ROOT / "logs" / "gpt_sovits.log"
        log_fp = open(log_file, "a", encoding="utf-8")
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        env = os.environ.copy()
        env["PATH"] = str(sovits_dir / "runtime") + ";" + str(sovits_dir / "runtime" / "Scripts") + ";" + env.get("PATH", "")
        env["PYTHONIOENCODING"] = "utf-8"
        proc = subprocess.Popen(
            cmd,
            cwd=str(sovits_dir),
            env=env,
            creationflags=flags,
            stdout=log_fp,
            stderr=log_fp,
            stdin=subprocess.DEVNULL,
        )
        _SPAWNED_SOVITS_PROC = proc
        assign_process_to_job(proc)
        (PROJECT_ROOT / "gptsovits.pid").write_text(str(proc.pid), encoding="utf-8")
        print(f"      {GREEN}[OK]{RESET} 已在后台启动 GPT-SoVITS (PID: {proc.pid})，进程与主窗口已安全绑定联动")

        # Readiness check with progress dots (bounded: 120s, model load 10~60s typical).
        print(f"      {CYAN}[..]{RESET} 正在等待 GPT-SoVITS 模型加载入显存 (最长 120 秒)...")
        for i in range(240):
            time.sleep(0.5)
            if is_port_in_use(9880):
                print()
                print(f"      {GREEN}[OK]{RESET} GPT-SoVITS 语音引擎已就绪 (http://127.0.0.1:9880/)")
                break
            if i % 4 == 0:
                print(".", end="", flush=True)
        else:
            print()
            print(f"      {YELLOW}[提示]{RESET} GPT-SoVITS 模型仍在后台加载中 (详见 logs/gpt_sovits.log)，本服务将先行启动。")
    except Exception as e:
        print(f"      {RED}[WARN]{RESET} 启动 GPT-SoVITS 失败: {e}")


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

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind((host, 0))
            return s.getsockname()[1]
    except Exception:
        return preferred_port


def auto_open_browser(port: int = 8080):
    """Background thread that waits for the server to be ready, then launches the browser."""
    def _runner():
        url = f"http://127.0.0.1:{port}/"
        for _ in range(30):
            time.sleep(0.3)
            if is_port_in_use(port):
                time.sleep(0.5)
                webbrowser.open(url)
                break

    t = threading.Thread(target=_runner, daemon=True)
    t.start()


def main():
    os.system("")  # Enable ANSI in terminal
    print(f"\n{MAGENTA}{BOLD}{'='*68}{RESET}")
    print(f"{MAGENTA}{BOLD}        🌸 Galgame2Voice - 二次元智能语音伴侣 一键启动器{RESET}")
    print(f"{MAGENTA}{BOLD}{'='*68}{RESET}\n")

    # Step 0: Setup OS-level process tree binding
    setup_windows_job_object()
    atexit.register(cleanup_subprocesses)

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
        print(f"{YELLOW}[提示] 默认端口 {preferred_port} 无法绑定 (可能被系统代理或其他程序占用)，已自动切换至可用端口: {active_port}{RESET}")

    # Save active port & PID for clean shutdown
    try:
        (PROJECT_ROOT / "data" / "active_port.txt").write_text(str(active_port), encoding="utf-8")
        (PROJECT_ROOT / "galgame2voice.pid").write_text(str(os.getpid()), encoding="utf-8")
    except Exception:
        pass

    # Step 3: Auto Open Browser
    print(f"{CYAN}[2/2] 正在启动 Galgame2Voice 伴侣服务 (端口 {active_port})...{RESET}")
    auto_open_browser(active_port)
    print(f"      {GREEN}[OK]{RESET} 正在为您打开默认浏览器: http://127.0.0.1:{active_port}/\n")

    print(f"{GREEN}{BOLD}{'='*68}{RESET}")
    print(f"{GREEN}{BOLD}  🎉 服务运行中！您可以在浏览器中畅享与二次元伴侣的互动。{RESET}")
    print(f"{GREEN}{BOLD}  💡 直接关闭此控制台窗口即可自动安全退出并释放全部 GPU 显存。{RESET}")
    print(f"{GREEN}{BOLD}{'='*68}{RESET}\n")
    try:
        import uvicorn
        uvicorn.run("galgame2voice.main:app", host="127.0.0.1", port=active_port, log_level="info")
    except (KeyboardInterrupt, SystemExit):
        print(f"\n{YELLOW}[提示] 服务已正常停止，正在释放资源...{RESET}")
    except Exception as e:
        print(f"\n{RED}[错误] 服务运行异常: {e}{RESET}")
        sys.exit(1)
    finally:
        cleanup_subprocesses()
    sys.exit(0)


if __name__ == "__main__":
    main()
