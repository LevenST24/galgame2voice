"""Model pointer files must preserve their target across UTF-8 encodings."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from galgame2voice.services import character_package
from galgame2voice.utils import path_guard


@pytest.fixture
def pointer_package(tmp_path, monkeypatch):
    settings = SimpleNamespace(project_root=tmp_path)
    monkeypatch.setattr(character_package, "get_settings", lambda: settings)
    monkeypatch.setattr(path_guard, "get_settings", lambda: settings)
    folder = tmp_path / "package"
    folder.mkdir()
    return folder


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig"])
@pytest.mark.parametrize("absolute", [True, False])
def test_model_pointer_resolves_target_with_or_without_bom(pointer_package, encoding, absolute):
    target = pointer_package.parent / "models" / "模型.pth"
    target.parent.mkdir()
    target.write_bytes(b"synthetic weights")
    text = str(target) if absolute else target.relative_to(pointer_package.parent).as_posix()
    pointer = pointer_package / "voice.pth"
    pointer.write_text(text + "\n", encoding=encoding)
    package = character_package.CharacterPackage(
        pointer_package, SimpleNamespace(sovits_weights="voice.pth", system_prompt=""),
    )
    resolved = package.resolve_weight_path("sovits_weights")
    resolved_path = Path(resolved)
    if not resolved_path.is_absolute():
        resolved_path = pointer_package.parent / resolved_path
    assert resolved_path == target
    assert resolved_path.is_file()


@pytest.mark.parametrize("payload", [b"\x80\x02synthetic binary", b"x" * 4096])
def test_binary_or_large_weights_are_not_interpreted_as_pointers(pointer_package, payload):
    pointer = pointer_package / "voice.pth"
    pointer.write_bytes(payload)
    assert character_package._resolve_pointer_file_target(pointer) is None


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig"])
def test_pointer_traversal_stays_rejected(pointer_package, encoding):
    pointer = pointer_package / "voice.pth"
    pointer.write_text("../outside.pth", encoding=encoding)
    assert character_package._resolve_pointer_file_target(pointer) == ""
