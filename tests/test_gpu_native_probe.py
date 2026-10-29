"""Hardware status should not initialize a training framework for a native result."""
import sys

import pytest

from galgame2voice.utils import hardware


@pytest.mark.parametrize("probe,platform", [("smi", "win32"), ("windows", "win32"), ("linux", "linux")])
def test_native_gpu_metadata_does_not_cold_import_torch(monkeypatch, probe, platform):
    monkeypatch.delitem(sys.modules, "torch", raising=False)
    monkeypatch.setattr(sys, "platform", platform)
    for name in ("_detect_nvidia_smi_gpus", "_detect_windows_gpus", "_detect_linux_lspci_gpus"):
        monkeypatch.setattr(hardware, name, lambda: [])
    selected = {"smi": "_detect_nvidia_smi_gpus", "windows": "_detect_windows_gpus", "linux": "_detect_linux_lspci_gpus"}[probe]
    monkeypatch.setattr(hardware, selected, lambda: ["NVIDIA native probe"])
    def unexpected_torch():
        pytest.fail("Native GPU metadata was available but detection initialized PyTorch")
    monkeypatch.setattr(hardware, "_detect_torch_gpus", unexpected_torch)
    assert hardware.detect_gpu_capability() == (True, "NVIDIA native probe", 1)
    assert "torch" not in sys.modules


def test_loaded_torch_keeps_its_existing_device_precedence(monkeypatch):
    monkeypatch.setitem(sys.modules, "torch", object())
    monkeypatch.setattr(hardware, "_detect_torch_gpus", lambda: ["NVIDIA loaded CUDA device", "NVIDIA second device"])
    def unexpected_native():
        pytest.fail("A loaded CUDA inventory was already available")
    monkeypatch.setattr(hardware, "_detect_nvidia_smi_gpus", unexpected_native)
    assert hardware.detect_gpu_capability() == (True, "NVIDIA loaded CUDA device", 2)


@pytest.mark.parametrize("names,expected", [(["NVIDIA fallback device"], (True, "NVIDIA fallback device", 1)), ([], (False, "N/A", None))])
def test_torch_remains_a_fallback_when_native_inventory_is_unavailable(monkeypatch, names, expected):
    monkeypatch.delitem(sys.modules, "torch", raising=False)
    for name in ("_detect_nvidia_smi_gpus", "_detect_windows_gpus", "_detect_linux_lspci_gpus"):
        monkeypatch.setattr(hardware, name, lambda: [])
    calls = []
    def fallback():
        calls.append(True)
        return names
    monkeypatch.setattr(hardware, "_detect_torch_gpus", fallback)
    assert hardware.detect_gpu_capability() == expected
    assert calls == [True]
