from importlib.metadata import PackagePath
from pathlib import Path
import sys

SOURCE_ROOT = Path(__file__).resolve().parent.parent
if str(SOURCE_ROOT) not in sys.path or sys.path[0] != str(SOURCE_ROOT):
    sys.path.insert(0, str(SOURCE_ROOT))

import pytest  # noqa: E402

from scripts.build_windows_portable import copy_runtime_licenses  # noqa: E402


class _SyntheticDistribution:
    """Fake metadata distribution providing genuine PackagePath objects."""

    def __init__(self, name: str, version: str, files: list[PackagePath], file_map: dict[str, Path]):
        self.metadata = {"Name": name}
        self.version = version
        self.files = files
        self._file_map = file_map

    def locate_file(self, relative: PackagePath) -> Path:
        return self._file_map[str(relative)]


@pytest.mark.parametrize("rel_path_str, expected_leaf, content", [
    ("demo_pkg-1.0.dist-info/LICENSE", "LICENSE", b"forward-slash-license"),
    (r"demo_pkg-1.0.dist-info\LICENSE", "LICENSE", b"backslash-license"),
    (r"nested/folder\COPYING.txt", "COPYING.txt", b"mixed-separator-copying"),
])
def test_copy_runtime_licenses_separator_compatibility(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rel_path_str: str, expected_leaf: str, content: bytes
) -> None:
    source_files_dir = tmp_path / "pkg_sources"
    source_files_dir.mkdir()
    real_file = source_files_dir / expected_leaf
    real_file.write_bytes(content)

    package_path = PackagePath(rel_path_str)
    dist = _SyntheticDistribution(
        name="test_pkg",
        version="1.0.0",
        files=[package_path],
        file_map={rel_path_str: real_file},
    )

    monkeypatch.setattr("importlib.metadata.distributions", lambda: [dist])

    fake_py_base = tmp_path / "fake_py_base"
    fake_py_base.mkdir()
    py_license = fake_py_base / "LICENSE-python.txt"
    py_license.write_bytes(b"python-license-text")
    monkeypatch.setattr(sys, "base_prefix", str(fake_py_base))

    dest = tmp_path / "THIRD_PARTY_LICENSES"
    copy_runtime_licenses(dest)

    expected_pkg_folder = dest / "test_pkg-1.0.0"
    assert expected_pkg_folder.is_dir()

    # Destination folder must only contain the normalized leaf filename, no subdirectories
    shipped_files = [p.name for p in expected_pkg_folder.iterdir() if p.is_file()]
    assert shipped_files == [expected_leaf]
    shipped_subdirs = [p.name for p in expected_pkg_folder.iterdir() if p.is_dir()]
    assert shipped_subdirs == []

    target_file = expected_pkg_folder / expected_leaf
    assert target_file.read_bytes() == content

    # Python base license preserved
    shipped_py_license = dest / "Python-LICENSE-python.txt"
    assert shipped_py_license.is_file()
    assert shipped_py_license.read_bytes() == b"python-license-text"
