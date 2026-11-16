"""Hardware system memory probe tests across Windows/Linux/macOS."""

import pytest

from galgame2voice.utils.hardware import get_system_memory_status


def test_get_system_memory_status_returns_floats_or_none():
    total, avail = get_system_memory_status()
    if total is not None:
        assert isinstance(total, float)
        assert total > 0.0
    if avail is not None:
        assert isinstance(avail, float)
        assert avail >= 0.0


@pytest.mark.real_hardware_probe
def test_linux_proc_meminfo_parsing(monkeypatch, tmp_path):
    import galgame2voice.utils.hardware as hw

    meminfo_file = tmp_path / "meminfo"
    meminfo_file.write_text(
        "MemTotal:       16384000 kB\n"
        "MemFree:         4096000 kB\n"
        "MemAvailable:    8192000 kB\n"
        "Buffers:          500000 kB\n"
        "Cached:          3000000 kB\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(hw.sys, "platform", "linux")
    monkeypatch.setattr(hw.os.path, "exists", lambda p: p == "/proc/meminfo" or str(p) == "/proc/meminfo")
    real_open = open

    def fake_open(p, *args, **kwargs):
        if str(p) == "/proc/meminfo":
            return real_open(meminfo_file, *args, **kwargs)
        return real_open(p, *args, **kwargs)

    monkeypatch.setattr("builtins.open", fake_open)

    total, avail = hw.get_system_memory_status()
    assert total == 15.62
    assert avail == 7.81


@pytest.mark.real_hardware_probe
def test_linux_proc_meminfo_fallback_buffers_cached(monkeypatch, tmp_path):
    import galgame2voice.utils.hardware as hw

    meminfo_file = tmp_path / "meminfo_old"
    # Kernel without MemAvailable
    meminfo_file.write_text(
        "MemTotal:       16384000 kB\n"
        "MemFree:         2000000 kB\n"
        "Buffers:         1000000 kB\n"
        "Cached:          3000000 kB\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(hw.sys, "platform", "linux")
    monkeypatch.setattr(hw.os.path, "exists", lambda p: p == "/proc/meminfo" or str(p) == "/proc/meminfo")
    real_open = open

    def fake_open(p, *args, **kwargs):
        if str(p) == "/proc/meminfo":
            return real_open(meminfo_file, *args, **kwargs)
        return real_open(p, *args, **kwargs)

    monkeypatch.setattr("builtins.open", fake_open)

    total, avail = hw.get_system_memory_status()
    assert total == 15.62
    # (2000000 + 1000000 + 3000000) / (1024 * 1024) = 5.72
    assert avail == 5.72


@pytest.mark.real_hardware_probe
def test_macos_sysctl_memory_parsing(monkeypatch):
    import galgame2voice.utils.hardware as hw

    monkeypatch.setattr(hw.sys, "platform", "darwin")
    orig_exists = hw.os.path.exists
    monkeypatch.setattr(hw.os.path, "exists", lambda p: False if str(p) == "/proc/meminfo" else orig_exists(p))
    monkeypatch.delattr(hw.os, "sysconf", raising=False)
    monkeypatch.setattr(hw, "_exec_command_output", lambda *args, **kwargs: "17179869184\n")

    total, avail = hw.get_system_memory_status()
    assert total == 16.0
    assert avail is None
