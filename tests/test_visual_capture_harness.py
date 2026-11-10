"""Unit tests for the asset-free browser visual scene capture harness."""

from __future__ import annotations

import logging
from pathlib import Path
import struct

import pytest

import scripts.capture_visual_scenes as harness
from scripts.capture_visual_scenes import (
    DEFAULT_OUTPUT_DIR,
    FORBIDDEN_PORTS,
    SCENES,
    VIEWPORTS,
    allocate_ephemeral_ports,
    build_capture_plan,
    create_synthetic_character,
    main,
    resolve_output_dir,
    validate_port,
    write_synthetic_png,
)
from galgame2voice.services.portrait_manifest import load_portrait_manifest
from galgame2voice.schemas.character_manifest import validate_character_package

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _read_png_ihdr(path: Path) -> tuple[int, int, int, int]:
    raw = path.read_bytes()
    assert raw[:8] == PNG_SIGNATURE
    length, chunk_type = struct.unpack(">I4s", raw[8:16])
    assert length == 13
    assert chunk_type == b"IHDR"
    width, height, bit_depth, color_type = struct.unpack(">IIBB", raw[16:26])
    return width, height, bit_depth, color_type


def test_synthetic_character_portrait_and_package_valid(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    sample_png = tmp_path / "sample.png"
    write_synthetic_png(sample_png, 32, 48, [(4, 6, 12, 20, (80, 140, 220, 255))])
    assert _read_png_ihdr(sample_png) == (32, 48, 8, 6)

    char_dir = create_synthetic_character(tmp_path / "characters")
    assert (char_dir / "manifest.json").is_file()
    assert (char_dir / "gentle.wav").is_file()

    expected_layers = (
        "body.png",
        "eyes_open.png",
        "eyes_closed.png",
        "mouth_closed.png",
        "mouth_open.png",
        "blush.png",
    )
    for layer_name in expected_layers:
        layer_path = char_dir / "portrait" / "layers" / "c1" / layer_name
        assert layer_path.is_file(), f"Missing synthetic layer {layer_name}"
        assert _read_png_ihdr(layer_path) == (600, 1200, 8, 6)

    with caplog.at_level(logging.WARNING, logger="galgame2voice.services.portrait_manifest"):
        portrait = load_portrait_manifest(char_dir / "portrait")
    assert portrait is not None
    assert portrait["version"] == 3
    assert portrait["enabled"] is True
    assert caplog.records == []

    is_valid, errors = validate_character_package(char_dir)
    assert is_valid is True, f"Character package validation failed: {errors}"
    assert errors == []


def test_capture_plan_output_paths_and_port_guards(tmp_path: Path) -> None:
    assert SCENES == ("stage", "settings", "log", "loading", "error")
    assert VIEWPORTS == (
        ("1920x1080", 1920, 1080),
        ("1366x768", 1366, 768),
        ("narrow_900x700", 900, 700),
    )

    out_dir = tmp_path / "captures"
    plan = build_capture_plan(out_dir)
    assert len(plan) == 15
    assert {(item["viewport_id"], item["scene"]) for item in plan} == {
        (vp[0], scene) for vp in VIEWPORTS for scene in SCENES
    }
    for item in plan:
        assert item["path"] == out_dir / item["viewport_id"] / f"{item['scene']}.png"

    with pytest.raises(ValueError, match="scene"):
        build_capture_plan(out_dir, scenes=("stage", "nonexistent"))
    with pytest.raises(ValueError, match="viewport"):
        build_capture_plan(out_dir, viewports=(("unknown_vp", 800, 600),))

    assert DEFAULT_OUTPUT_DIR.parts[0] == ".local"
    resolved_default = resolve_output_dir(DEFAULT_OUTPUT_DIR, project_root=tmp_path)
    assert resolved_default == (tmp_path / ".local" / "visual-captures").resolve()

    for forbidden_rel in (
        Path("."),
        Path("characters"),
        Path("characters/synth_heroine"),
        Path("backgrounds/pack"),
        Path("skins/custom"),
        Path("galgame2voice/static"),
        Path("galgame2voice/static/captures"),
        Path("frontend/screenshots"),
        Path("data/shots"),
        Path("audio/shots"),
    ):
        with pytest.raises(ValueError):
            resolve_output_dir(forbidden_rel, project_root=tmp_path)

    assert FORBIDDEN_PORTS == frozenset({8080, 8081, 9880, 18080, 18996, 18997, 18998})
    for bad_port in (*FORBIDDEN_PORTS, 0, -1, 70000):
        with pytest.raises(ValueError):
            validate_port(bad_port)
    assert validate_port(24567) == 24567

    ports = allocate_ephemeral_ports(3)
    assert len(ports) == 3
    assert len(set(ports)) == 3
    assert all(20000 <= p <= 29999 and p not in FORBIDDEN_PORTS for p in ports)

    code_forbidden_port = main(["--output-dir", str(tmp_path / "out_port"), "--app-port", "8080"])
    assert code_forbidden_port == 2


def test_missing_browser_exits_nonzero_with_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(harness, "find_browser", lambda: None)
    code = main(["--output-dir", str(tmp_path / "out")])
    captured = capsys.readouterr()
    assert code == 2
    assert "--browser" in captured.err
    assert any(name in captured.err for name in ("Chrome", "Chromium", "Edge"))

    code_explicit = main(
        [
            "--output-dir",
            str(tmp_path / "out2"),
            "--browser",
            str(tmp_path / "does_not_exist_browser.exe"),
        ]
    )
    captured_explicit = capsys.readouterr()
    assert code_explicit == 2
    assert "--browser" in captured_explicit.err
