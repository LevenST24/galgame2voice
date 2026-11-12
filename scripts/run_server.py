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
import importlib
import json
import shlex
from pathlib import Path
from typing import Any

# Ensure project root is in sys.path
PROJECT_ROOT = (
    Path(sys.executable).resolve().parent if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent.parent
)
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if __name__ == "__main__":
    # The API's engine restart must share this launcher's process ownership.
    sys.modules["scripts.run_server"] = sys.modules[__name__]


def _print_python_recovery_command(arguments: list[str]) -> None:
    """Show commands for the current interpreter in each supported shell."""
    command = [sys.executable, *arguments]
    if sys.platform == "win32":
        print(f"    命令提示符: {subprocess.list2cmdline(command)}")
        quoted = ["'" + part.replace("'", "''") + "'" for part in command]
        print("    PowerShell: & " + " ".join(quoted))
    else:
        print(f"    {shlex.join(command)}")


# E402: these imports must follow the sys.path bootstrap above, otherwise the
# script cannot find the package when it is executed directly from a checkout.
try:
    from galgame2voice.utils.precision import (  # noqa: E402, F401
        read_precision_cache,
        write_precision_cache,
        write_sovits_yaml_config,
        resolve_initial_device_and_half,
        sync_runtime_precision,
    )
    from galgame2voice.utils.hardware import get_gpu_vram_status  # noqa: E402
    from galgame2voice.services.sovits_installation import (  # noqa: E402, F401
        find_engine_python,
        read_sovits_directory,
        write_sovits_directory_hint,
    )
    from galgame2voice.services.engine_runtime import EngineLaunch, prepare_engine_entrypoint  # noqa: E402, F401
    from galgame2voice.services.engine_supervisor import (  # noqa: E402, F401
        _is_matching_managed_host,
        _launch_sovits_process,
        assign_process_to_job,
        build_gpt_sovits_env as _build_gpt_sovits_env,
        get_managed_engine_launch,
        get_managed_engine_precision,
        is_port_in_use,
        register_runtime_host,
        setup_windows_job_object as _setup_windows_job_object,
        terminate_process_tree,
    )
    from galgame2voice.config import get_settings  # noqa: E402
    from galgame2voice.utils.windows_runtime import kernel32  # noqa: E402
    from scripts.launcher_address import browser_hosts  # noqa: E402
except ImportError as exc:
    # Config imports Pydantic before main() can run the ordinary dependency
    # check. Give source-checkout users a recovery path, but keep missing local
    # modules and frozen/imported launcher failures visible to their callers.
    if (__name__ != "__main__" or getattr(sys, "frozen", False)
            or exc.name not in {"pydantic", "pydantic_settings"}):
        raise
    print(f"[错误] 当前 Python 环境的运行依赖缺少或版本不兼容：{exc.name}。")
    print("请在项目目录打开终端，运行：uv sync --frozen")
    print("如果没有 uv，可先确认此 Python 有 pip，再运行：")
    _print_python_recovery_command(["-m", "pip", "install", "-r", str(PROJECT_ROOT / "requirements.txt")])
    print("如提示没有 pip，可先运行：")
    _print_python_recovery_command(["-m", "ensurepip", "--upgrade"])
    sys.exit(1)

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
_SPAWNED_SOVITS_STARTED_AT: float | None = None
_SPAWNED_SOVITS_LAUNCH: EngineLaunch | None = None
_WINDOWS_JOB_HANDLE = None
register_runtime_host(sys.modules[__name__])


def setup_windows_job_object():
    """Creates a Windows Job Object configured with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE."""
    return _setup_windows_job_object()


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
    global _SPAWNED_SOVITS_PROC, _WINDOWS_JOB_HANDLE
    proc, _SPAWNED_SOVITS_PROC = _SPAWNED_SOVITS_PROC, None
    # Enumerate descendants before terminating their parent. A PID file alone
    # never establishes ownership, and an exited Popen PID may already be reused.
    if proc is not None:
        try:
            if proc.poll() is None:
                terminate_process_tree(proc.pid)
                proc.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            pass
    if _WINDOWS_JOB_HANDLE:
        kernel32().CloseHandle(_WINDOWS_JOB_HANDLE)
        _WINDOWS_JOB_HANDLE = None
    # Another launcher may own these files. Preserve them while it is alive.
    owner_file = PROJECT_ROOT / "galgame2voice.pid"
    try:
        raw_pid = owner_file.read_text(encoding="utf-8").strip()
        if raw_pid.isdigit() and int(raw_pid) != os.getpid():
            import psutil
            if psutil.pid_exists(int(raw_pid)):
                return
    except FileNotFoundError:
        pass
    except (OSError, ImportError):
        return
    try:
        (PROJECT_ROOT / "data" / "active_port.txt").unlink(missing_ok=True)
        (PROJECT_ROOT / "galgame2voice.pid").unlink(missing_ok=True)
        (PROJECT_ROOT / "gptsovits.pid").unlink(missing_ok=True)
        (PROJECT_ROOT / "data" / "desktop_instance.json").unlink(missing_ok=True)
    except Exception:
        pass


def get_sovits_host_port() -> tuple[str, int]:
    """
    Single source of truth for the engine address: parses GPT_SOVITS_BASE_URL
    (env or config) so launcher, backend client and Docker all agree.

    Thin wrapper over the unified resolver
    (galgame2voice.services.sovits_endpoint); kept for backward compatibility.
    """
    try:
        from galgame2voice.services.sovits_endpoint import (
            resolve_effective_sovits_endpoint_sync,
        )

        endpoint = resolve_effective_sovits_endpoint_sync()
        return endpoint.host, endpoint.port
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
        p = Path(env_dir.strip().strip("\"'"))
        if not p.is_absolute():
            p = PROJECT_ROOT / p
        candidates.append(p)
    # Legacy known-installation hints (kept as low-priority fallbacks only)
    for extra in (os.environ.get("GALGAME2VOICE_DATA_ROOT"),):
        if extra:
            candidates.append(Path(extra))

    # Fast cache lookup to avoid repeating disk searches on every run
    # Strictly use utf-8-sig to strip any invisible Windows PowerShell UTF-8 BOM (\ufeff)
    cache_file = PROJECT_ROOT / "data" / "sovits_dir.txt"
    if cache_file.exists():
        try:
            cached_path = read_sovits_directory(PROJECT_ROOT)
            if cached_path is not None:
                valid_cached = _check_sovits_dir(cached_path)
                if valid_cached:
                    try:
                        raw_hint = cache_file.read_text(encoding="utf-8-sig").strip().strip("\"'")
                        if Path(raw_hint).is_absolute() and valid_cached.resolve().is_relative_to(PROJECT_ROOT.resolve().parent):
                            write_sovits_directory_hint(PROJECT_ROOT, valid_cached)
                    except Exception:
                        pass
                    return valid_cached
        except Exception:
            pass

    for p in candidates:
        valid_dir = _check_sovits_dir(p)
        if valid_dir:
            try:
                write_sovits_directory_hint(PROJECT_ROOT, valid_dir)
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
                write_sovits_directory_hint(PROJECT_ROOT, valid_dir)
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
                    write_sovits_directory_hint(PROJECT_ROOT, valid_dir)
                except Exception:
                    pass
                return valid_dir
        for p_str in glob.glob(rf"{drive}:\GPT-SoVITS*"):
            p = Path(p_str)
            valid_dir = _check_sovits_dir(p)
            if valid_dir:
                try:
                    write_sovits_directory_hint(PROJECT_ROOT, valid_dir)
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


def build_gpt_sovits_env(
    sovits_dir: Path,
    is_half: bool = False,
    *,
    project_root: Path | None = None,
) -> dict[str, str]:
    """Constructs an isolated process environment for GPT-SoVITS."""
    root = Path(project_root) if project_root is not None else PROJECT_ROOT
    return _build_gpt_sovits_env(sovits_dir, is_half=is_half, project_root=root)


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
        ("pydantic_settings", "pydantic-settings"),
        ("psutil", "psutil"),
        ("cryptography", "cryptography"),
    ]
    if getattr(sys, "frozen", False):
        core_deps.append(("tkinter", "tkinter"))
    missing = []
    for mod_name, pkg_name in core_deps:
        try:
            __import__(mod_name)
        except ImportError:
            missing.append(pkg_name)

    if missing:
        print(f"\n[提示] 正在检查运行依赖... 发现缺少核心运行库: {', '.join(missing)}")
        if getattr(sys, "frozen", False):
            print("[错误] 便携包缺少运行组件。请重新解压完整的 Windows 便携包，保留 _internal 目录。")
            return False
        req_file = PROJECT_ROOT / "requirements.txt"
        if req_file.exists():
            print(f"[提示] 正在尝试自动安装依赖: {req_file.name} ...")
            try:
                subprocess.run(
                    [sys.executable, "-m", "pip", "install", "-r", str(req_file)],
                    check=True,
                )
            except Exception as e:
                print("\n" + "=" * 60)
                print(f"[错误] 核心运行依赖安装失败: {e}")
                print("请在命令行手动执行安装:")
                _print_python_recovery_command(["-m", "pip", "install", "-r", str(req_file)])
                print("如提示没有 pip，可先运行：")
                _print_python_recovery_command(["-m", "ensurepip", "--upgrade"])
                print("=" * 60 + "\n")
                return False
            importlib.invalidate_caches()
            still_missing = []
            for mod_name, pkg_name in core_deps:
                if pkg_name not in missing:
                    continue
                try:
                    __import__(mod_name)
                except ImportError:
                    still_missing.append(pkg_name)
            if still_missing:
                print(f"[错误] 安装命令已结束，但当前 Python 仍无法加载: {', '.join(still_missing)}")
                print("请检查 pip 安装位置及 PIP_TARGET、PIP_PREFIX；也可在项目目录运行 uv sync --frozen。")
                return False
            print("[OK] 运行依赖自动安装成功。")
        else:
            print("\n" + "=" * 60)
            print(f"[错误] 缺少核心运行库: {', '.join(missing)}")
            print("请手动运行：")
            _print_python_recovery_command(["-m", "pip", "install", *missing])
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
    Inspects GPU availability via NVIDIA vendor-name matching, plus VRAM and system memory.
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
        print(f"      [硬件就绪] 检测到独立显卡: {detected_name} (按厂商名匹配识别，实际推理设备与精度以引擎校准结果为准)")
        vram_total, vram_free = get_gpu_vram_status()
        if vram_total is not None:
            diag["vram_total_gb"] = vram_total
            diag["vram_free_gb"] = vram_free
            if vram_total <= 4.1:
                print(f"      [显存提示] 显卡物理显存为 {vram_total:.1f} GB (显存较紧凑)。")
                print("                长时间连续多轮对话或高并发时可能存在显存溢出(OOM)风险。")
                print("                若遇显存不足，可在控制面板或通过 `启动.bat --cpu`（Linux/macOS 下为 `bash run.sh --cpu`）启用「CPU 稳定模式」（依托大内存，彻底杜绝崩溃）。")
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
    *,
    project_root: Path | None = None,
) -> subprocess.Popen:
    """Launches the GPT-SoVITS API daemon with the given precision and device, binding to launcher lifecycle."""
    root = Path(project_root) if project_root is not None else PROJECT_ROOT
    proc, assigned = _launch_sovits_process(
        sovits_dir,
        host,
        port,
        is_half,
        device=device,
        project_root=root,
        warn_missing_runtime=True,
    )
    prec_label = "CPU" if str(device).lower() == "cpu" else ("FP16" if is_half else "FP32")
    print(f"      [OK] 已在后台启动 GPT-SoVITS (PID: {proc.pid}, {prec_label})")
    if sys.platform == "win32" and not assigned:
        print("      [提示] Windows 未允许绑定引擎进程；请用 Ctrl+C 正常退出以清理引擎。")
    return proc


def _find_custom_voice_ref_audio(
    default_text: str = "テスト、聞こえていますか。",
    default_lang: str = "ja",
) -> tuple[Path | None, str, str]:
    """
    Fallback when no character package is available: inspect SQLite voice_profiles.
    Prioritizes settings.active_voice_profile_id, then walks rows by is_default DESC, id ASC.
    Resolves reference audio files against resolve_reference_audio_path and PROJECT_ROOT.
    Returns (ref_audio_path, prompt_text, prompt_lang) or (None, default_text, default_lang).
    """
    conn = None
    cur = None
    try:
        import sqlite3
        from galgame2voice.config import get_settings
        from galgame2voice.utils.audio_spec import resolve_reference_audio_path

        settings = get_settings()
        db_path = getattr(settings, "db_path", None)
        if not db_path:
            return None, default_text, default_lang
        db_file = Path(db_path)
        if not db_file.is_file():
            return None, default_text, default_lang

        # 只读查询 SQLite (优先使用 mode=ro URI，失败则普通连接)
        try:
            conn = sqlite3.connect(f"{db_file.resolve().as_uri()}?mode=ro", uri=True, timeout=2.0)
        except Exception:
            conn = sqlite3.connect(str(db_file), timeout=2.0)

        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        active_id = None
        try:
            cur.execute("SELECT active_voice_profile_id FROM settings WHERE id = 1;")
            s_row = cur.fetchone()
            if s_row and s_row["active_voice_profile_id"] is not None:
                active_id = s_row["active_voice_profile_id"]
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() or "busy" in str(e).lower():
                return None, default_text, default_lang
        except Exception:
            pass

        if active_id is not None:
            cur.execute(
                "SELECT * FROM voice_profiles ORDER BY CASE WHEN id = ? THEN 0 ELSE 1 END, is_default DESC, id ASC;",
                (active_id,),
            )
        else:
            cur.execute("SELECT * FROM voice_profiles ORDER BY is_default DESC, id ASC;")

        candidates = cur.fetchall()
        project_root = getattr(settings, "project_root", PROJECT_ROOT)

        for row in candidates:
            keys = row.keys()
            raw_ref = (
                row["ref_audio_path"]
                if "ref_audio_path" in keys and row["ref_audio_path"]
                else (row["refer_audio_path"] if "refer_audio_path" in keys else "")
            )
            if not raw_ref or not str(raw_ref).strip():
                continue

            raw_ref_str = str(raw_ref).strip()
            resolved_file: Path | None = None

            try:
                resolved_str = resolve_reference_audio_path(raw_ref_str)
                if resolved_str:
                    candidate = Path(resolved_str)
                    if candidate.is_file():
                        resolved_file = candidate.resolve()
            except Exception:
                pass

            if resolved_file is None:
                p_ref = Path(raw_ref_str)
                search_paths = [p_ref] if p_ref.is_absolute() else [project_root / p_ref, PROJECT_ROOT / p_ref]
                for candidate in search_paths:
                    try:
                        if candidate.is_file():
                            resolved_file = candidate.resolve()
                            break
                    except OSError:
                        pass

            if resolved_file is not None and resolved_file.is_file():
                raw_text = (
                    row["prompt_text"]
                    if "prompt_text" in keys and row["prompt_text"]
                    else (row["refer_text"] if "refer_text" in keys else None)
                )
                raw_lang = (
                    row["prompt_lang"]
                    if "prompt_lang" in keys and row["prompt_lang"]
                    else (row["refer_language"] if "refer_language" in keys else None)
                )
                prompt_text = str(raw_text).strip() if raw_text and str(raw_text).strip() else default_text
                prompt_lang = str(raw_lang).strip() if raw_lang and str(raw_lang).strip() else default_lang
                return resolved_file, prompt_text, prompt_lang

    except Exception:
        return None, default_text, default_lang
    finally:
        if cur is not None:
            try:
                cur.close()
            except Exception:
                pass
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    return None, default_text, default_lang


def _probe_synth_peak(host: str, port: int, timeout: float = 90.0) -> float | None:
    """
    Synthesizes one short test sentence via the engine's /tts endpoint and returns
    the WAV peak amplitude (0.0~1.0). Returns None when the probe is INCONCLUSIVE
    (timeout / connection / HTTP error / no reference audio) — None never means 'silent'.
    """
    import urllib.parse
    import urllib.request

    ref_audio: Path | None = None
    prompt_text = "テスト、聞こえていますか。"
    prompt_lang = "ja"

    # 1. 优先沿用角色包参考音频（优先 default 角色包，其次任意可用角色包）
    try:
        from galgame2voice.services.character_manager import get_character_manager
        mgr = get_character_manager()

        def _resolve_pkg_audio(pkg) -> tuple[Path | None, str, str]:
            if pkg and pkg.manifest and pkg.manifest.emotions:
                emo = pkg.manifest.emotions.get("gentle") or next(iter(pkg.manifest.emotions.values()), None)
                if emo:
                    resolved = pkg.resolve_audio_path(emo.audio)
                    if resolved and resolved.is_file():
                        return resolved, emo.text or prompt_text, emo.lang or prompt_lang
            return None, prompt_text, prompt_lang

        ref_audio, prompt_text, prompt_lang = _resolve_pkg_audio(mgr.get_default_character())
        if ref_audio is None:
            for p in mgr.get_available_characters():
                ref_audio, prompt_text, prompt_lang = _resolve_pkg_audio(p)
                if ref_audio is not None:
                    break
    except Exception:
        pass

    # 2. 未找到可用角色包时，回退读取 SQLite voice_profiles 中的自定义音色参考音频
    if ref_audio is None:
        try:
            ref_audio, prompt_text, prompt_lang = _find_custom_voice_ref_audio(prompt_text, prompt_lang)
        except Exception:
            pass

    # 3. 若既无角色包也无任何有效自定义参考音频文件，安全返回 None
    if ref_audio is None or not ref_audio.is_file():
        return None

    try:
        from galgame2voice.services.gpt_sovits_client import wav_peak_amplitude
    except Exception:
        from galgame2voice.utils.audio_spec import wav_peak_amplitude

    params = urllib.parse.urlencode({
        "text": "テスト、聞こえていますか。",
        "text_lang": "ja",
        "ref_audio_path": str(ref_audio),
        "prompt_text": prompt_text,
        "prompt_lang": prompt_lang,
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
        print("      [精度校准] 探针未完成 (未配置参考音频或网络/超时)，跳过本次校准，沿用当前精度。")
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


_PORT_READY_SETTLE_SECONDS = 2.0


def _wait_for_sovits_port(proc, host: str, port: int) -> bool:
    """Waits while proc stays alive until (host, port) starts accepting connections."""
    slow_hint_printed = False
    ticks = 0
    while proc is not None and proc.poll() is None:
        time.sleep(0.5)
        ticks += 1
        if proc.poll() is not None:
            break
        if is_port_in_use(port, host):
            return proc.poll() is None
        if not slow_hint_printed and ticks >= 240:
            print("\n      [提示] GPT-SoVITS 模型仍在后台加载中 (详见 logs/gpt_sovits.log)")
            slow_hint_printed = True
    return False


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
        return  # an already-resolved precision (cli/env/db/cache/yaml) or CPU mode skips the probe

    def probe() -> float | None:
        return _probe_synth_peak(host, port)

    def restart(new_is_half: bool):
        global _SPAWNED_SOVITS_PROC, _SPAWNED_SOVITS_LAUNCH, _SPAWNED_SOVITS_STARTED_AT
        from galgame2voice.routers import health
        from galgame2voice.services.gpt_sovits_client import get_gpt_sovits_client
        from galgame2voice.services.sovits_endpoint import parse_sovits_endpoint

        target_url = parse_sovits_endpoint(f"http://{host}:{port}", source="runtime").base_url
        health._engine_restart_lock.acquire()
        try:
            health._engine_restart_target = target_url
            try:
                get_gpt_sovits_client().invalidate_model_state()
            except Exception as exc:
                print(f"      [WARN] 作废客户端音色状态失败: {exc}")

            old = _SPAWNED_SOVITS_PROC
            if old is not None:
                try:
                    terminate_process_tree(old.pid)
                except Exception:
                    pass
            # Wait for the old engine to release the port before relaunching.
            port_released = False
            for _ in range(20):
                time.sleep(0.5)
                if not is_port_in_use(port, host):
                    port_released = True
                    break
            if old is not None:
                _SPAWNED_SOVITS_PROC = None
                _SPAWNED_SOVITS_LAUNCH = None
                _SPAWNED_SOVITS_STARTED_AT = None
                (PROJECT_ROOT / "gptsovits.pid").unlink(missing_ok=True)
            if not port_released:
                print(f"      [WARN] 旧 GPT-SoVITS 进程未在 10 秒内释放端口 {port}，取消拉起新进程。")
                return None
            try:
                proc = _spawn_sovits_process(sovits_dir, host, port, new_is_half, device=device)
            except Exception as exc:
                print(f"      [WARN] 引擎重启失败: {exc}")
                return None
        finally:
            health._engine_restart_target = None
            health._engine_restart_lock.release()

        if not _wait_for_sovits_port(proc, host, port):
            return None
        # Give the model a moment to finish loading weights after port bind.
        time.sleep(_PORT_READY_SETTLE_SECONDS)
        return proc if proc.poll() is None else None

    # Give the model a moment to finish loading weights after port bind.
    time.sleep(_PORT_READY_SETTLE_SECONDS)
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

    The engine address comes from the unified resolver (explicit process env >
    SQLite > .env > built-in default). Remote endpoints are never managed by
    this launcher: no fallback to a local 127.0.0.1:9880 process is performed.
    """
    sync_runtime_precision(
        fp16=fp16,
        fp32=fp32,
        cpu=cpu,
        precision=precision,
    )
    try:
        from galgame2voice.services.sovits_endpoint import (
            resolve_effective_sovits_endpoint_sync,
        )

        endpoint = resolve_effective_sovits_endpoint_sync()
        sovits_host, sovits_port = endpoint.host, endpoint.port
        sovits_is_local = endpoint.is_local
        sovits_base_url = endpoint.base_url
    except ValueError as exc:
        print(f"      [错误] GPT-SoVITS 配置地址无效 ({exc})，拒绝静默启动本地引擎。请检查配置。")
        return
    except Exception as exc:
        print(f"      [WARN] GPT-SoVITS 地址读取异常 ({exc})，回落到默认 127.0.0.1:9880")
        sovits_host, sovits_port = "127.0.0.1", 9880
        sovits_is_local = True
        sovits_base_url = "http://127.0.0.1:9880"
    print(f"[1/2] 正在检测 GPT-SoVITS 语音推理引擎 ({sovits_host}:{sovits_port})...")
    if is_port_in_use(sovits_port, sovits_host):
        print("      [OK] GPT-SoVITS 语音引擎已在运行")
        return
    if not sovits_is_local:
        print(
            f"      [WARN] 配置的远端 GPT-SoVITS ({sovits_base_url}) 不可达；"
            "本启动器不会回退到本地 127.0.0.1:9880，请在远端主机上自行启动引擎后重试。"
        )
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
        device, is_half, precision_source = resolve_initial_device_and_half(
            PROJECT_ROOT, sovits_dir, db_path=get_settings().db_path,
        )
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

        # Non-blocking parallel readiness monitor (engine logs written to gpt_sovits.log)
        print("      [..] GPT-SoVITS 正在后台加载模型 (伴侣服务先行启动)...")

        def _wait_for_sovits_readiness_worker():
            if _wait_for_sovits_port(proc, sovits_host, sovits_port):
                print(f"\n      [OK] GPT-SoVITS 语音引擎已就绪 (http://{sovits_host}:{sovits_port}/)")
                _calibrate_precision_after_ready(sovits_dir, sovits_host, sovits_port, is_half, precision_source, device=device)
                return
            if proc is not None and proc.poll() is not None:
                print(f"\n      [WARN] GPT-SoVITS 异常退出 (退出码: {proc.returncode})，详见 logs/gpt_sovits.log")

        monitor_thread = threading.Thread(
            target=_wait_for_sovits_readiness_worker,
            daemon=True,
            name="sovits-readiness-monitor",
        )
        monitor_thread.start()
    except Exception as e:
        print(f"      [WARN] 启动 GPT-SoVITS 失败: {e}")



def reserve_available_socket(preferred_port: int = 8080, host: str = "127.0.0.1") -> socket.socket:
    """Bind and retain a listening socket until the server takes ownership."""
    candidates = [preferred_port, 8081, 8082, 8085, 8088, 8888, 18080, 28080]
    seen = set()
    unique_candidates = [p for p in candidates if not (p in seen or seen.add(p))]
    family = socket.AF_INET6 if ":" in host else socket.AF_INET

    for p in [*unique_candidates, 0]:
        listener = None
        try:
            listener = socket.socket(family, socket.SOCK_STREAM)
            # Windows SO_REUSEADDR can bind over an existing listener that
            # also enabled reuse, so it cannot establish availability.
            option = socket.SO_EXCLUSIVEADDRUSE if sys.platform == "win32" else socket.SO_REUSEADDR
            listener.setsockopt(socket.SOL_SOCKET, option, 1)
            listener.bind((host, p))
            listener.listen(2048)
            return listener
        except OSError:
            if listener is not None:
                listener.close()
            if p == 0:
                raise

    raise RuntimeError("Could not reserve a listening port")


def find_available_port(preferred_port: int = 8080, host: str = "127.0.0.1") -> int:
    """Compatibility helper: report a port without keeping it reserved."""
    with reserve_available_socket(preferred_port, host) as listener:
        return listener.getsockname()[1]


def auto_open_browser(port: int = 8080, host: str = "127.0.0.1"):
    """Background thread that waits for the HTTP service to answer, then launches the browser."""
    import http.client

    def _runner():
        probe_host, display_host = browser_hosts(host)
        url = f"http://{display_host}:{port}/"
        expected_app = get_settings().app_name
        deadline = time.monotonic() + 60.0
        # Fast local probe directly via raw loopback socket bypassing OS proxy delay
        while time.monotonic() < deadline:
            time.sleep(min(0.3, max(0.0, deadline - time.monotonic())))
            conn = None
            ready = False
            try:
                conn = http.client.HTTPConnection(probe_host, port, timeout=0.2)
                conn.request("GET", "/api/health")
                resp = conn.getresponse()
                if resp.status == 200:
                    health = json.loads(resp.read(4096))
                    ready = isinstance(health, dict) and health.get("app") == expected_app and health.get("status") == "ok"
            except (OSError, ValueError, http.client.HTTPException):
                pass
            finally:
                if conn is not None:
                    conn.close()
            if ready:
                try:
                    if not webbrowser.open(url):
                        print(f"[提示] 未能自动打开浏览器，请手动访问：{url}")
                except Exception:
                    print(f"[提示] 未能自动打开浏览器，请手动访问：{url}")
                return
        print(f"[提示] 页面尚未确认就绪，请查看启动窗口；服务就绪后可手动访问：{url}")

    t = threading.Thread(target=_runner, daemon=True, name="browser-launcher")
    t.start()



def sync_runtime_address(*, host: str | None = None, port: int | None = None) -> None:
    """Synchronize runtime host/port across all environment aliases and clear Settings cache.

    Pydantic Settings configures AliasChoices("GALGAME2VOICE_HOST", "HOST", "GALGAME_HOST")
    and AliasChoices("GALGAME2VOICE_PORT", "PORT", "GALGAME_PORT"). To guarantee that
    all Settings consumers and lifespan security guards reflect the actual binding,
    every alias must be set and the lru_cache cleared.
    """
    if host is not None:
        canonical_host = str(host).strip()
        os.environ["GALGAME2VOICE_HOST"] = canonical_host
        os.environ["HOST"] = canonical_host
        os.environ["GALGAME_HOST"] = canonical_host
    if port is not None:
        canonical_port = str(port).strip()
        os.environ["GALGAME2VOICE_PORT"] = canonical_port
        os.environ["PORT"] = canonical_port
        os.environ["GALGAME_PORT"] = canonical_port
    try:
        from galgame2voice.config import get_settings
        get_settings.cache_clear()
    except Exception:
        pass


def parse_args(args: list[str] | None = None) -> argparse.Namespace:
    """Parses CLI flags for the Galgame2Voice server launcher."""
    parser = argparse.ArgumentParser(
        description="Galgame2Voice Server Entry Point & Enterprise Auto-Launcher",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    env_port = 8080
    env_port_str = os.environ.get("GALGAME2VOICE_PORT") or os.environ.get("PORT") or os.environ.get("GALGAME_PORT")
    if env_port_str:
        try:
            env_port = int(env_port_str)
        except ValueError:
            pass
    env_host = (
        os.environ.get("GALGAME2VOICE_HOST")
        or os.environ.get("HOST")
        or os.environ.get("GALGAME_HOST")
        or "127.0.0.1"
    )
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
        "--no-engine",
        action="store_true",
        help="Open the application without discovering or starting a local GPT-SoVITS process",
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
    parser.add_argument(
        "--profile",
        action="store_true",
        default=False,
        help="Enable latency waterfall profiling mode (shows TTFT, First Sentence, TTS Dispatch, TTS Inference, TTFA)",
    )
    return parser.parse_args(args)


def main(args: list[str] | None = None):
    parsed = parse_args(args)

    # Immediately synchronize the requested host/port across all environment aliases
    # and clear any early-cached Settings before any diagnostic or engine setup runs.
    sync_runtime_address(host=parsed.host, port=parsed.port)
    sync_runtime_precision(
        fp16=getattr(parsed, "fp16", False),
        fp32=getattr(parsed, "fp32", False),
        cpu=getattr(parsed, "cpu", False),
        precision=getattr(parsed, "precision", None),
    )

    if getattr(parsed, "profile", False):
        os.environ["GALGAME2VOICE_PROFILE"] = "1"
        print("[性能追踪] 已启用 --profile 实时延迟瀑布图分析模式 (GALGAME2VOICE_PROFILE=1)")

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

    listener = None
    try:
        # Step 1: GPT-SoVITS
        if getattr(parsed, "no_engine", False):
            print("[1/2] 已跳过语音引擎自动启动；可在网页中完成首次配置。")
        else:
            ensure_gpt_sovits_running(
                fp16=getattr(parsed, "fp16", False),
                fp32=getattr(parsed, "fp32", False),
                cpu=getattr(parsed, "cpu", False),
                precision=getattr(parsed, "precision", None),
            )

        # Step 2: Determine & Probe Port
        preferred_port = parsed.port
        bind_host = parsed.host
        listener = reserve_available_socket(preferred_port, host=bind_host)
        active_port = listener.getsockname()[1]
        if preferred_port != 0 and active_port != preferred_port:
            print(f"[提示] 默认端口 {preferred_port} 无法绑定 (可能被系统代理或其他程序占用)，已自动切换至可用端口: {active_port}")

        # Synchronize active port and host across all aliases and clear Settings cache
        sync_runtime_address(host=bind_host, port=active_port)
        try:
            (PROJECT_ROOT / "data" / "active_port.txt").write_text(str(active_port), encoding="utf-8")
            (PROJECT_ROOT / "galgame2voice.pid").write_text(str(os.getpid()), encoding="utf-8")
        except Exception:
            pass

        if getattr(sys, "frozen", False):
            from scripts.desktop_instance import write_instance_address
            write_instance_address(PROJECT_ROOT, bind_host, active_port)

        # Step 3: Auto Open Browser
        _, display_host = browser_hosts(bind_host)
        print(f"[2/2] 正在启动 Galgame2Voice 伴侣服务 ({bind_host}:{active_port})...")
        if not parsed.no_browser:
            auto_open_browser(active_port, host=bind_host)
            print(f"      [OK] 浏览器将在后台探测到 /api/health 返回 200 后自动打开: http://{display_host}:{active_port}/")
        else:
            print(f"      [提示] 已开启 --no-browser，跳过自动打开浏览器。访问地址: http://{display_host}:{active_port}/")
        print("      关闭此窗口即可退出并释放资源。")
        try:
            import uvicorn
            config = uvicorn.Config("galgame2voice.main:app", host=bind_host, port=active_port, log_level="info")
            server = uvicorn.Server(config)
            server.run(sockets=[listener])
            if not server.started:
                raise SystemExit(3)
        except KeyboardInterrupt:
            print("\n[提示] 服务已正常停止，正在释放资源...")
        except SystemExit as exc:
            if exc.code not in (None, 0):
                print("\n[启动失败] 本机服务未能就绪。请确认解压目录可写，并查看 logs 中的错误日志；若提示数据库损坏，请先保留 data 文件夹，再从备份恢复。")
                if getattr(sys, "frozen", False):
                    print("可双击「数据恢复.bat」选择经过校验的数据库备份，恢复前会另存原文件。")
                raise
            print("\n[提示] 服务已正常停止，正在释放资源...")
        except Exception as e:
            print(f"\n[错误] 服务运行异常: {e}")
            sys.exit(1)
    finally:
        if listener is not None:
            listener.close()
        cleanup_subprocesses()
    sys.exit(0)


if __name__ == "__main__":
    main()

