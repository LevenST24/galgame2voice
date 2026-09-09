"""
Hardware Detection & Telemetry Utilities for galgame2voice.
Provides robust GPU capability detection, Turing TU116/TU117 identification,
and host system memory status telemetry.
"""

import os
import sys
import subprocess
from typing import Optional, Tuple

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
    Returns (None, None) if detection fails.
    """
    try:
        if sys.platform == "win32":
            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                return round(stat.ullTotalPhys / (1024 ** 3), 2), round(stat.ullAvailPhys / (1024 ** 3), 2)
        else:
            import psutil
            vm = psutil.virtual_memory()
            return round(vm.total / (1024 ** 3), 2), round(vm.available / (1024 ** 3), 2)
    except Exception:
        pass

    try:
        import psutil
        vm = psutil.virtual_memory()
        return round(vm.total / (1024 ** 3), 2), round(vm.available / (1024 ** 3), 2)
    except Exception:
        pass

    return None, None


def detect_gpu_capability() -> Tuple[bool, str, Optional[int]]:
    """
    Detects GPU compute availability and primary device metadata.
    Returns: (gpu_available, gpu_name, device_count)
    - gpu_available: True if a CUDA-compatible or NVIDIA accelerator is detected.
    - gpu_name: Name of the primary graphics device or 'N/A' if none found.
    - device_count: Number of detected discrete compute units (or None if unverified).
    """
    # 1. PyTorch CUDA inspection if available
    try:
        import torch
        if torch.cuda.is_available():
            count = torch.cuda.device_count()
            name = torch.cuda.get_device_name(0) if count > 0 else "CUDA Device"
            return True, name, count
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
            return True, names[0], len(names)
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
                nvidia_names = [
                    n for n in names
                    if any(k in n.lower() for k in ["nvidia", "geforce", "rtx", "gtx", "quadro", "tesla"])
                ]
                if nvidia_names:
                    return True, nvidia_names[0], len(nvidia_names)
                return False, names[0], len(names)
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
                nvidia_names = [
                    n for n in names
                    if any(k in n.lower() for k in ["nvidia", "geforce", "rtx", "gtx", "quadro", "tesla"])
                ]
                if nvidia_names:
                    return True, nvidia_names[0], len(nvidia_names)
                return False, names[0], len(names)
        except Exception:
            pass

    return False, "N/A", None


def is_turing_tu116_tu117_gpu(gpu_name_override: Optional[str] = None) -> bool:
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
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=2.0,
            )
            gpu_names.extend([line.strip().lower() for line in out.splitlines() if line.strip()])
        except Exception:
            pass

    # 3. Windows WMI / CIM query
    if not gpu_names and sys.platform == "win32":
        try:
            out = subprocess.check_output(
                ["wmic", "path", "win32_VideoController", "get", "name"],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=2.0,
            )
            gpu_names.extend([
                line.strip().lower() for line in out.splitlines()
                if line.strip() and line.strip().lower() != "name"
            ])
        except Exception:
            pass

        if not gpu_names:
            try:
                out = subprocess.check_output(
                    ["powershell", "-NoProfile", "-Command", "(Get-CimInstance Win32_VideoController).Name"],
                    text=True,
                    stderr=subprocess.DEVNULL,
                    timeout=3.0,
                )
                gpu_names.extend([line.strip().lower() for line in out.splitlines() if line.strip()])
            except Exception:
                pass

    all_names_str = " ".join(gpu_names).lower()
    return any(k in all_names_str for k in target_keywords)
