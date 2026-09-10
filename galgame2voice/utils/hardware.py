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


def _resolve_cgroup_paths(root_path: Path) -> List[Path]:
    """
    Returns a list of candidate cgroup directory paths to inspect for the current process,
    ordered from most specific (container subpath via /proc/self/cgroup) to root_path.
    """
    candidates: List[Path] = []
    # 1. Inspect /proc/self/cgroup to detect specific container slices in K8s, Docker, systemd
    proc_cgroup = Path("/proc/self/cgroup")
    if proc_cgroup.is_file():
        try:
            for line in proc_cgroup.read_text(encoding="utf-8").splitlines():
                parts = line.strip().split(":")
                if len(parts) == 3:
                    subpath = parts[2].lstrip("/")
                    if subpath:
                        # cgroups v2 entry: 0::<path>
                        if parts[0] == "0" and parts[1] == "":
                            cand = root_path / subpath
                            if cand.is_dir() and cand not in candidates:
                                candidates.append(cand)
                        # cgroups v1 entry: <num>:memory:<path>
                        elif "memory" in parts[1].split(","):
                            # On cgroups v1, controllers are submounted under root_path/memory/
                            cand_mem = root_path / "memory" / subpath
                            if cand_mem.is_dir() and cand_mem not in candidates:
                                candidates.append(cand_mem)
                            cand_direct = root_path / subpath
                            if cand_direct.is_dir() and cand_direct not in candidates:
                                candidates.append(cand_direct)
        except Exception:
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
                    return max(0.0, (limit_bytes - curr_bytes) / (1024 ** 3))

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
                            return max(0.0, (limit_bytes - usage_bytes) / (1024 ** 3))
    except Exception:
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


def _detect_host_memory_status() -> Tuple[Optional[float], Optional[float]]:
    """
    Returns (total_ram_gb, available_ram_gb) for the host system.
    Supports Windows (Win32 GlobalMemoryStatusEx), Linux (/proc/meminfo),
    macOS (sysctl/sysconf), with graceful psutil fallback.
    Returns (None, None) if all detection methods fail.
    """
    # 1. Windows Win32 API
    if sys.platform == "win32":
        try:
            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                return round(stat.ullTotalPhys / (1024 ** 3), 2), round(stat.ullAvailPhys / (1024 ** 3), 2)
        except Exception:
            pass

    # 2. Linux /proc/meminfo (zero-dependency native inspection)
    if sys.platform.startswith("linux") or os.path.exists("/proc/meminfo"):
        try:
            mem_total_kb = None
            mem_avail_kb = None
            mem_free_kb = None
            buffers_kb = None
            cached_kb = None
            with open("/proc/meminfo", "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) >= 2:
                        key = parts[0].rstrip(":")
                        try:
                            val = float(parts[1])
                        except ValueError:
                            continue
                        if key == "MemTotal":
                            mem_total_kb = val
                        elif key == "MemAvailable":
                            mem_avail_kb = val
                        elif key == "MemFree":
                            mem_free_kb = val
                        elif key == "Buffers":
                            buffers_kb = val
                        elif key == "Cached":
                            cached_kb = val
            if mem_total_kb is not None:
                total_gb = round(mem_total_kb / (1024 * 1024), 2)
                if mem_avail_kb is not None:
                    avail_gb = round(mem_avail_kb / (1024 * 1024), 2)
                elif mem_free_kb is not None and buffers_kb is not None and cached_kb is not None:
                    avail_gb = round((mem_free_kb + buffers_kb + cached_kb) / (1024 * 1024), 2)
                elif mem_free_kb is not None:
                    avail_gb = round(mem_free_kb / (1024 * 1024), 2)
                else:
                    avail_gb = None
                return total_gb, avail_gb
        except Exception:
            pass

    # 3. macOS sysctl / os.sysconf inspection
    if sys.platform == "darwin":
        try:
            total_bytes = None
            if hasattr(os, "sysconf") and "SC_PAGE_SIZE" in os.sysconf_names and "SC_PHYS_PAGES" in os.sysconf_names:
                total_bytes = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
            if total_bytes is None:
                out = subprocess.check_output(
                    ["sysctl", "-n", "hw.memsize"],
                    text=True,
                    stderr=subprocess.DEVNULL,
                    timeout=2.0,
                )
                total_bytes = int(out.strip())
            total_gb = round(total_bytes / (1024 ** 3), 2)
            # macOS does not expose a single trivial available sysctl; return total
            return total_gb, None
        except Exception:
            pass

    # 4. Cross-platform psutil fallback
    try:
        import psutil
        vm = psutil.virtual_memory()
        return round(vm.total / (1024 ** 3), 2), round(vm.available / (1024 ** 3), 2)
    except Exception:
        pass

    return None, None


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
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=2.0,
        )
        names = [line.strip() for line in out.splitlines() if line.strip()]
        if names:
            gpu_names.extend(names)
            return gpu_names
    except Exception:
        pass

    # 3. Windows WMI / CIM query
    if sys.platform == "win32":
        try:
            out = subprocess.check_output(
                ["wmic", "path", "win32_VideoController", "get", "name"],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=2.0,
            )
            names = [
                line.strip() for line in out.splitlines()
                if line.strip() and line.strip().lower() != "name"
            ]
            if names:
                return names
        except Exception:
            pass

        try:
            out = subprocess.check_output(
                ["powershell", "-NoProfile", "-Command", "(Get-CimInstance Win32_VideoController).Name"],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=3.0,
            )
            names = [line.strip() for line in out.splitlines() if line.strip()]
            if names:
                return names
        except Exception:
            pass

    # 4. Linux lspci query
    if sys.platform.startswith("linux"):
        try:
            out = subprocess.check_output(
                ["lspci"],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=2.0,
            )
            vga_lines = [l.strip() for l in out.splitlines() if any(k in l.lower() for k in ["vga", "3d controller", "display"])]
            if vga_lines:
                names = []
                for line in vga_lines:
                    parts = line.split(":")
                    names.append(parts[-1].strip() if len(parts) >= 3 else line)
                return names
        except Exception:
            pass

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
