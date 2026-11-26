"""Pointer-safe Windows APIs shared by the launcher and desktop instance lock."""

import ctypes
from ctypes import wintypes
from functools import lru_cache
import sys


@lru_cache(maxsize=1)
def kernel32():
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "CreateJobObjectW": ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
        "SetInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
        "AssignProcessToJobObject": ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
        "CreateMutexW": ([ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR], wintypes.HANDLE),
        "ReleaseMutex": ([wintypes.HANDLE], wintypes.BOOL),
        "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
        "GetStdHandle": ([wintypes.DWORD], wintypes.HANDLE),
        "GetConsoleMode": ([wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
        "SetConsoleMode": ([wintypes.HANDLE, wintypes.DWORD], wintypes.BOOL),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(api, name)
        function.argtypes = arguments
        function.restype = result
    return api


def disable_console_quick_edit() -> None:
    """Turn off Windows console QuickEdit so clicking the window cannot freeze log writes."""
    if sys.platform != "win32":
        return
    api = kernel32()
    handle = api.GetStdHandle(-10)  # STD_INPUT_HANDLE
    mode = wintypes.DWORD()
    if not api.GetConsoleMode(handle, ctypes.pointer(mode)):
        return
    # Clear ENABLE_QUICK_EDIT_MODE (0x0040); ENABLE_EXTENDED_FLAGS (0x0080) makes that bit take effect.
    api.SetConsoleMode(handle, (mode.value & ~0x0040) | 0x0080)
