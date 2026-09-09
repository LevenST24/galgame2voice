"""
Hardware Detection & Telemetry Utilities for galgame2voice.
Provides robust GPU capability detection, Turing TU116/TU117 identification,
and cross-platform host system memory status telemetry.
"""

import os
import sys
import subprocess
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


def get_system_memory_status() -> Tuple[Optional[float], Optional[float]]:
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


def is_turing_tu116_tu117_gpu(gpu_name_override: Optional[str] = None) -> bool:
    """
    Detects if the system has an NVIDIA Turing TU116 or TU117 architecture GPU.
    Affected models: GeForce MX450, MX550, GTX 1650, GTX 1660, GTX 1630, etc.
    On these GPUs, FP16 half-precision inference causes PyTorch to produce NaN and zero-amplitude (silent) audio.
    """
    target_keywords = ["mx450", "mx550", "1650", "1660", "1630", "tu117", "tu116"]
    if gpu_name_override is not None:
        return any(k in gpu_name_override.lower() for k in target_keywords)

    gpu_names = _get_all_detected_gpu_names()
    all_names_str = " ".join(gpu_names).lower()
    return any(k in all_names_str for k in target_keywords)
