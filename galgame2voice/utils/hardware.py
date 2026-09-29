"""
Hardware Detection & Telemetry Utilities for galgame2voice.
Provides robust GPU capability detection, Turing TU116/TU117 identification,
and cross-platform host system memory status telemetry.
"""

import os
import sys
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple

BYTES_PER_KB: int = 1024
BYTES_PER_MB: int = 1024 * 1024
BYTES_PER_GB: int = 1024 ** 3
DEFAULT_SUBPROCESS_TIMEOUT: float = 2.0

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


def _exec_command_output(cmd: List[str], timeout: float = DEFAULT_SUBPROCESS_TIMEOUT) -> Optional[str]:
    """Safely executes a system command, suppressing stderr and subprocess exceptions."""
    try:
        return subprocess.check_output(
            cmd,
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
        )
    except (subprocess.SubprocessError, OSError, UnicodeDecodeError):
        return None


def _resolve_cgroup_paths(root_path: Path) -> List[Path]:
    """
    Returns a list of candidate cgroup directory paths to inspect for the current process,
    ordered from most specific (container subpath via /proc/self/cgroup) to root_path.
    """
    candidates: List[Path] = []

    def _add_if_dir(cand: Path) -> None:
        if cand.is_dir() and cand not in candidates:
            candidates.append(cand)

    # 1. Inspect /proc/self/cgroup to detect specific container slices in K8s, Docker, systemd
    proc_cgroup = Path("/proc/self/cgroup")
    if proc_cgroup.is_file():
        try:
            for line in proc_cgroup.read_text(encoding="utf-8").splitlines():
                parts = line.strip().split(":")
                if len(parts) != 3:
                    continue
                subpath = parts[2].lstrip("/")
                if not subpath:
                    continue

                # cgroups v2 entry: 0::<path>
                if parts[0] == "0" and parts[1] == "":
                    _add_if_dir(root_path / subpath)
                # cgroups v1 entry: <num>:memory:<path>
                elif "memory" in parts[1].split(","):
                    # On cgroups v1, controllers are submounted under root_path/memory/
                    _add_if_dir(root_path / "memory" / subpath)
                    _add_if_dir(root_path / subpath)
        except (OSError, UnicodeDecodeError):
            pass

    # 2. Add root_path as fallback (for container environments with private cgroup namespaces)
    if root_path not in candidates:
        candidates.append(root_path)

    return candidates


def get_cgroup_memory_available_gb(cgroup_root: Optional[str] = None) -> Optional[float]:
    """
    Detects container memory quota limits via Linux cgroups (v2 and v1).
    Inspects container-specific cgroup hierarchies (e.g. Kubernetes, Docker) via /proc/self/cgroup
    as well as container root cgroup mounts.
    Returns available memory in GB within the container limit, or None if no quota is configured.
    """
    try:
        root_path = Path(cgroup_root or os.getenv("GALGAME2VOICE_CGROUP_ROOT", "/sys/fs/cgroup"))
        if not root_path.exists():
            return None

        candidate_dirs = _resolve_cgroup_paths(root_path)

        # 1. Check cgroups v2 (memory.max & memory.current)
        for cdir in candidate_dirs:
            cg2_max = cdir / "memory.max"
            cg2_curr = cdir / "memory.current"
            if cg2_max.is_file() and cg2_curr.is_file():
                max_val = cg2_max.read_text(encoding="utf-8").strip()
                if max_val and max_val != "max":
                    limit_bytes = int(max_val)
                    curr_bytes = int(cg2_curr.read_text(encoding="utf-8").strip())
                    return max(0.0, (limit_bytes - curr_bytes) / BYTES_PER_GB)

        # 2. Check cgroups v1 (memory.limit_in_bytes & memory.usage_in_bytes)
        for cdir in candidate_dirs:
            cg1_candidates = [
                (cdir / "memory.limit_in_bytes", cdir / "memory.usage_in_bytes"),
                (cdir / "memory" / "memory.limit_in_bytes", cdir / "memory" / "memory.usage_in_bytes"),
            ]
            for lim_p, use_p in cg1_candidates:
                if lim_p.is_file() and use_p.is_file():
                    raw_lim = lim_p.read_text(encoding="utf-8").strip()
                    if raw_lim:
                        limit_bytes = int(raw_lim)
                        # cgroups v1 unlimited sentinel is typically >= 1 << 60 (e.g. 0x7FFFFFFFFFFFF000)
                        if limit_bytes < (1 << 60):
                            usage_bytes = int(use_p.read_text(encoding="utf-8").strip())
                            return max(0.0, (limit_bytes - usage_bytes) / BYTES_PER_GB)
    except (OSError, ValueError, UnicodeDecodeError):
        pass

    return None


def get_system_memory_status() -> Tuple[Optional[float], Optional[float]]:
    """
    Returns (total_ram_gb, available_ram_gb) for the host system.
    In containerized environments (Docker, Kubernetes, cgroups v1/v2) the available
    value is capped at min(host_available, cgroup_available).
    Returns (None, None) if all detection methods fail.
    """
    total_gb, avail_gb = _detect_host_memory_status()
    if avail_gb is not None and (
        sys.platform.startswith("linux")
        or os.getenv("GALGAME2VOICE_CGROUP_ROOT")
        or os.path.exists("/sys/fs/cgroup")
    ):
        cgroup_avail = get_cgroup_memory_available_gb()
        if cgroup_avail is not None:
            avail_gb = min(avail_gb, cgroup_avail)
    return total_gb, avail_gb


def _detect_windows_memory() -> Tuple[Optional[float], Optional[float]]:
    """Native Windows GlobalMemoryStatusEx memory inspection."""
    if sys.platform != "win32":
        return None, None
    try:
        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
            return round(stat.ullTotalPhys / BYTES_PER_GB, 2), round(stat.ullAvailPhys / BYTES_PER_GB, 2)
    except (OSError, AttributeError):
        pass
    return None, None


def _detect_linux_proc_meminfo() -> Tuple[Optional[float], Optional[float]]:
    """Zero-dependency Linux /proc/meminfo inspection."""
    if not (sys.platform.startswith("linux") or os.path.exists("/proc/meminfo")):
        return None, None
    try:
        mem_info: Dict[str, float] = {}
        with open("/proc/meminfo", "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2:
                    try:
                        mem_info[parts[0].rstrip(":")] = float(parts[1])
                    except ValueError:
                        continue

        mem_total_kb = mem_info.get("MemTotal")
        if mem_total_kb is None:
            return None, None

        total_gb = round(mem_total_kb / BYTES_PER_MB, 2)
        mem_avail_kb = mem_info.get("MemAvailable")
        mem_free_kb = mem_info.get("MemFree")
        buffers_kb = mem_info.get("Buffers")
        cached_kb = mem_info.get("Cached")

        if mem_avail_kb is not None:
            avail_gb = round(mem_avail_kb / BYTES_PER_MB, 2)
        elif mem_free_kb is not None and buffers_kb is not None and cached_kb is not None:
            avail_gb = round((mem_free_kb + buffers_kb + cached_kb) / BYTES_PER_MB, 2)
        elif mem_free_kb is not None:
            avail_gb = round(mem_free_kb / BYTES_PER_MB, 2)
        else:
            avail_gb = None
        return total_gb, avail_gb
    except (OSError, UnicodeDecodeError):
        pass
    return None, None


def _detect_darwin_memory() -> Tuple[Optional[float], Optional[float]]:
    """macOS sysctl / os.sysconf inspection."""
    if sys.platform != "darwin":
        return None, None
    try:
        total_bytes = None
        if hasattr(os, "sysconf") and "SC_PAGE_SIZE" in os.sysconf_names and "SC_PHYS_PAGES" in os.sysconf_names:
            total_bytes = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
        if total_bytes is None:
            out = _exec_command_output(["sysctl", "-n", "hw.memsize"])
            if out:
                total_bytes = int(out.strip())
        if total_bytes is not None:
            total_gb = round(total_bytes / BYTES_PER_GB, 2)
            # macOS does not expose a single trivial available sysctl; return total
            return total_gb, None
    except (OSError, ValueError):
        pass
    return None, None


def _detect_psutil_memory() -> Tuple[Optional[float], Optional[float]]:
    """Cross-platform psutil fallback inspection."""
    try:
        import psutil
        vm = psutil.virtual_memory()
        return round(vm.total / BYTES_PER_GB, 2), round(vm.available / BYTES_PER_GB, 2)
    except (ImportError, Exception):
        pass
    return None, None


def _detect_host_memory_status() -> Tuple[Optional[float], Optional[float]]:
    """
    Returns (total_ram_gb, available_ram_gb) for the host system.
    Supports Windows (Win32 GlobalMemoryStatusEx), Linux (/proc/meminfo),
    macOS (sysctl/sysconf), with graceful psutil fallback.
    Returns (None, None) if all detection methods fail.
    """
    # 1. Windows Win32 API
    total, avail = _detect_windows_memory()
    if total is not None:
        return total, avail

    # 2. Linux /proc/meminfo
    total, avail = _detect_linux_proc_meminfo()
    if total is not None:
        return total, avail

    # 3. macOS sysctl / os.sysconf
    total, avail = _detect_darwin_memory()
    if total is not None:
        return total, avail

    # 4. Cross-platform psutil fallback
    return _detect_psutil_memory()


def _get_all_detected_gpu_names() -> List[str]:
    """
    Internal helper collecting graphics device names from PyTorch, nvidia-smi,
    Windows WMI/CIM, or Linux lspci.
    """
    gpu_names: List[str] = []

    # 1. PyTorch CUDA inspection if available
    try:
        import torch
        if torch.cuda.is_available():
            count = torch.cuda.device_count()
            for i in range(count):
                try:
                    name = torch.cuda.get_device_name(i)
                    if name:
                        gpu_names.append(name)
                except Exception:
                    pass
            if gpu_names:
                return gpu_names
    except Exception:
        pass

    # 2. nvidia-smi tool inspection
    out = _exec_command_output(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"])
    if out:
        names = [line.strip() for line in out.splitlines() if line.strip()]
        if names:
            gpu_names.extend(names)
            return gpu_names

    # 3. Windows WMI / CIM query
    if sys.platform == "win32":
        out = _exec_command_output(["wmic", "path", "win32_VideoController", "get", "name"])
        if out:
            names = [
                line.strip() for line in out.splitlines()
                if line.strip() and line.strip().lower() != "name"
            ]
            if names:
                return names

        out = _exec_command_output(
            ["powershell", "-NoProfile", "-Command", "(Get-CimInstance Win32_VideoController).Name"],
            timeout=3.0,
        )
        if out:
            names = [line.strip() for line in out.splitlines() if line.strip()]
            if names:
                return names

    # 4. Linux lspci query
    if sys.platform.startswith("linux"):
        out = _exec_command_output(["lspci"])
        if out:
            vga_lines = [line.strip() for line in out.splitlines() if any(k in line.lower() for k in ["vga", "3d controller", "display"])]
            if vga_lines:
                names = []
                for line in vga_lines:
                    parts = line.split(":")
                    names.append(parts[-1].strip() if len(parts) >= 3 else line)
                return names

    return gpu_names


def detect_gpu_capability() -> Tuple[bool, str, Optional[int]]:
    """
    Detects GPU compute availability and primary device metadata.
    Returns: (gpu_available, gpu_name, device_count)
    - gpu_available: True if a CUDA-compatible or NVIDIA accelerator is detected.
    - gpu_name: Name of the primary graphics device or 'N/A' if none found.
    - device_count: Number of detected discrete compute units (or None if unverified).
    """
    gpu_names = _get_all_detected_gpu_names()
    if not gpu_names:
        return False, "N/A", None

    nvidia_names = [
        n for n in gpu_names
        if any(k in n.lower() for k in ["nvidia", "geforce", "rtx", "gtx", "quadro", "tesla"])
    ]
    if nvidia_names:
        return True, nvidia_names[0], len(nvidia_names)

    return False, gpu_names[0], len(gpu_names)


def release_system_memory() -> None:
    """
    Explicitly invokes Python garbage collection and safely clears PyTorch
    cached memory allocations (CUDA / MPS) if PyTorch is loaded.
    Prevents resident memory accumulation during model swaps on constrained hosts (e.g. 16GB RAM).
    """
    import gc
    gc.collect()
    if "torch" in sys.modules:
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
            if hasattr(torch, "mps") and hasattr(torch.mps, "empty_cache"):
                torch.mps.empty_cache()
        except Exception:
            pass


def get_gpu_vram_status() -> Tuple[Optional[float], Optional[float]]:
    """
    Returns (total_vram_gb, free_vram_gb) of primary NVIDIA GPU if available, else (None, None).
    Inspects nvidia-smi first (fast, zero PyTorch CUDA context overhead), falling back to PyTorch.
    """
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.total,memory.free", "--format=csv,noheader,nounits"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=DEFAULT_SUBPROCESS_TIMEOUT,
        )
        first_line = out.strip().splitlines()[0]
        parts = [float(x.strip()) for x in first_line.split(",")]
        if len(parts) >= 2:
            return round(parts[0] / BYTES_PER_KB, 2), round(parts[1] / BYTES_PER_KB, 2)
    except (subprocess.SubprocessError, OSError, ValueError, IndexError):
        pass

    try:
        import torch
        if torch.cuda.is_available():
            props = torch.cuda.get_device_properties(0)
            total_gb = round(props.total_memory / BYTES_PER_GB, 2)
            free_bytes, _ = torch.cuda.mem_get_info()
            return total_gb, round(free_bytes / BYTES_PER_GB, 2)
    except Exception:
        pass

    return None, None

