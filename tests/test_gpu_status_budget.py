"""A status read must not cold-import PyTorch or repeat a GPU measurement."""
from importlib.machinery import ModuleSpec
import sys
from types import ModuleType, SimpleNamespace

import pytest

from galgame2voice.routers import health
from galgame2voice.utils import hardware, precision
from scripts import run_server


@pytest.mark.parametrize("initializing", [False, True])
def test_status_without_native_inventory_never_initializes_torch(monkeypatch, initializing):
    monkeypatch.delitem(sys.modules, "torch", raising=False)
    if initializing:
        torch = ModuleType("torch")
        torch.__spec__ = ModuleSpec("torch", loader=None)
        torch.__spec__._initializing = True
        monkeypatch.setitem(sys.modules, "torch", torch)
    for name in ("_detect_nvidia_smi_gpus", "_detect_windows_gpus", "_detect_linux_lspci_gpus"):
        monkeypatch.setattr(hardware, name, lambda: [])
    def cold_import():
        pytest.fail("Reading status attempted to initialize or wait for PyTorch")
    monkeypatch.setattr(hardware, "_detect_torch_gpus", cold_import)
    monkeypatch.setattr(health, "_gpu_telemetry_cache", None)
    assert health._get_gpu_telemetry_cached() == (False, "N/A")


@pytest.mark.parametrize("snapshot,configured,expected", [
    ((True, "NVIDIA synthetic"), None, "FP16"),
    ((False, "N/A"), None, None),
    ((False, "N/A"), "cpu", "CPU"),
])
def test_status_precision_reuses_the_sample_and_preserves_unknown_hardware(
    tmp_path, monkeypatch, snapshot, configured, expected,
):
    monkeypatch.delenv("GPT_SOVITS_PRECISION", raising=False)
    monkeypatch.delenv("GPT_SOVITS_DEVICE", raising=False)
    monkeypatch.setattr(health, "get_settings", lambda: SimpleNamespace(project_root=tmp_path, db_path=tmp_path/"probe.db"))
    monkeypatch.setattr(health, "get_system_memory_status", lambda: (16, 8))
    monkeypatch.setattr(health, "read_sovits_directory", lambda root: None)
    monkeypatch.setattr(health, "read_db_precision", lambda *args, **kwargs: configured)
    monkeypatch.setattr(precision, "read_db_precision", lambda *args, **kwargs: configured)
    monkeypatch.setattr(run_server, "get_managed_engine_precision", lambda endpoint: None)
    measurements = []
    def repeated_probe():
        measurements.append(True)
        return True, "NVIDIA should not be sampled again", 1
    monkeypatch.setattr(hardware, "detect_gpu_capability", repeated_probe)
    result = health._collect_hardware_telemetry_sync(None, snapshot)
    assert measurements == []
    assert result.next_inference_precision == expected
    assert result.precision_pending_restart is None
    assert result.inference_precision is None
