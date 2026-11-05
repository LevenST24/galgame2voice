"""The host capability probes in conftest run once per pytest session and must not leave temp directories behind."""
import os
import tempfile

from tests.conftest import _probe_writable_private_temp_dir


def _probe_dirs(root):
    return sorted(name for name in os.listdir(root) if name.startswith("g2v_cap_"))


def test_writable_temp_probe_removes_only_its_own_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    (tmp_path / "g2v_cap_existing").mkdir()

    assert _probe_writable_private_temp_dir() is True
    assert _probe_dirs(tmp_path) == ["g2v_cap_existing"]


def test_writable_temp_probe_removes_its_directory_when_writing_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    real_makedirs = os.makedirs

    def deny_subdirectory(path, *args, **kwargs):
        if os.path.basename(path) == "sub":
            raise PermissionError("denied")
        return real_makedirs(path, *args, **kwargs)

    monkeypatch.setattr(os, "makedirs", deny_subdirectory)

    assert _probe_writable_private_temp_dir() is False
    assert _probe_dirs(tmp_path) == []


def test_writable_temp_probe_reports_an_uncreatable_directory(monkeypatch):
    def refuse(*args, **kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr(tempfile, "mkdtemp", refuse)

    assert _probe_writable_private_temp_dir() is False
