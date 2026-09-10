"""
Evidence-based precision calibration tests:
1. precision store read/write and resolve_initial_is_half priority (env > cache > default).
2. calibrate_engine_precision decision table (audible / silent / inconclusive), with a
   fake probe + fake restart — no real engine or GPU needed.
"""

import json
from types import SimpleNamespace

import pytest

from galgame2voice.utils.precision import (
    read_precision_cache,
    resolve_initial_is_half,
    write_precision_cache,
)


class TestPrecisionStore:
    def test_write_and_read_roundtrip(self, tmp_path):
        write_precision_cache(tmp_path, str(tmp_path / "engine"), False)
        data = read_precision_cache(tmp_path)
        assert data is not None
        assert data["is_half"] is False
        assert data["sovits_dir"] == str(tmp_path / "engine")

    def test_missing_cache_returns_none(self, tmp_path):
        assert read_precision_cache(tmp_path) is None

    def test_corrupt_cache_returns_none(self, tmp_path):
        (tmp_path / "data").mkdir()
        (tmp_path / "data" / "precision.json").write_text("{not json", encoding="utf-8")
        assert read_precision_cache(tmp_path) is None

    def test_utf8_bom_tolerated(self, tmp_path):
        (tmp_path / "data").mkdir()
        payload = json.dumps({"is_half": True, "sovits_dir": "x", "verified_at": 0})
        (tmp_path / "data" / "precision.json").write_bytes(b"\xef\xbb\xbf" + payload.encode("utf-8"))
        assert read_precision_cache(tmp_path)["is_half"] is True


class TestResolveInitialIsHalf:
    def test_env_override_fp32_wins(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GPT_SOVITS_PRECISION", "fp32")
        is_half, source = resolve_initial_is_half(tmp_path, tmp_path / "engine")
        assert is_half is False and source == "env"

    def test_env_override_fp16_wins(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GPT_SOVITS_PRECISION", "FP16")
        is_half, source = resolve_initial_is_half(tmp_path, tmp_path / "engine")
        assert is_half is True and source == "env"

    def test_cache_used_when_engine_dir_matches(self, tmp_path, monkeypatch):
        monkeypatch.delenv("GPT_SOVITS_PRECISION", raising=False)
        engine = tmp_path / "engine"
        write_precision_cache(tmp_path, str(engine), False)
        is_half, source = resolve_initial_is_half(tmp_path, engine)
        assert is_half is False and source == "cache"

    def test_cache_ignored_when_engine_dir_changed(self, tmp_path, monkeypatch):
        monkeypatch.delenv("GPT_SOVITS_PRECISION", raising=False)
        write_precision_cache(tmp_path, str(tmp_path / "old_engine"), False)
        is_half, source = resolve_initial_is_half(tmp_path, tmp_path / "new_engine")
        assert is_half is True and source == "default"

    def test_default_is_fp16(self, tmp_path, monkeypatch):
        monkeypatch.delenv("GPT_SOVITS_PRECISION", raising=False)
        is_half, source = resolve_initial_is_half(tmp_path, tmp_path / "engine")
        assert is_half is True and source == "default"


class TestCalibrateEnginePrecision:
    """Exercises scripts.run_server.calibrate_engine_precision with fake probe/restart."""

    @pytest.fixture
    def calibrate(self):
        import scripts.run_server as rs
        return rs.calibrate_engine_precision

    def test_fp16_audible_caches_fp16(self, calibrate, tmp_path, monkeypatch):
        monkeypatch.setattr("scripts.run_server.PROJECT_ROOT", tmp_path)
        probes = iter([0.5])
        is_half, calibrated = calibrate(
            tmp_path / "engine", True, lambda: next(probes), lambda fh: (_ for _ in ()).throw(AssertionError("no restart expected")),
        )
        assert is_half is True and calibrated is True
        assert read_precision_cache(tmp_path)["is_half"] is True

    def test_fp16_silent_restarts_fp32_and_caches(self, calibrate, tmp_path, monkeypatch):
        monkeypatch.setattr("scripts.run_server.PROJECT_ROOT", tmp_path)
        # First probe (FP16): silent. Second probe (after FP32 restart): audible.
        probes = iter([0.0, 0.42])
        restarts = []

        def fake_restart(is_half):
            restarts.append(is_half)
            return object()

        is_half, calibrated = calibrate(tmp_path / "engine", True, lambda: next(probes), fake_restart)
        assert restarts == [False]
        assert is_half is False and calibrated is True
        assert read_precision_cache(tmp_path)["is_half"] is False

    def test_fp32_still_silent_no_cache(self, calibrate, tmp_path, monkeypatch):
        monkeypatch.setattr("scripts.run_server.PROJECT_ROOT", tmp_path)
        probes = iter([0.0, 0.0])
        is_half, calibrated = calibrate(tmp_path / "engine", True, lambda: next(probes), lambda fh: object())
        assert is_half is False and calibrated is False
        assert read_precision_cache(tmp_path) is None

    def test_inconclusive_probe_no_cache(self, calibrate, tmp_path, monkeypatch):
        monkeypatch.setattr("scripts.run_server.PROJECT_ROOT", tmp_path)
        is_half, calibrated = calibrate(tmp_path / "engine", True, lambda: None, lambda fh: object())
        assert is_half is True and calibrated is False
        assert read_precision_cache(tmp_path) is None

    def test_fp32_silent_from_start_no_restart(self, calibrate, tmp_path, monkeypatch):
        monkeypatch.setattr("scripts.run_server.PROJECT_ROOT", tmp_path)
        probes = iter([0.0])
        restarts = []

        is_half, calibrated = calibrate(tmp_path / "engine", False, lambda: next(probes), lambda fh: restarts.append(fh))
        assert restarts == []  # already FP32 — restarting changes nothing
        assert is_half is False and calibrated is False


class TestResolveWeightFilePath:
    """Weights can live inside the character package (project-relative) or the
    engine dir (engine-relative); the switch flow absolutizes either form."""

    def test_package_relative_weight_absolutized(self, tmp_path, monkeypatch):
        import galgame2voice.utils.path_guard as pg
        pkg = tmp_path / "characters" / "kanna" / "gpt.ckpt"
        pkg.parent.mkdir(parents=True)
        pkg.write_bytes(b"w")
        monkeypatch.setattr(pg, "get_settings", lambda: SimpleNamespace(project_root=tmp_path, audio_dir=tmp_path / "audio"))
        monkeypatch.setattr(pg, "get_authorized_roots", lambda include_sovits=True: [])
        assert pg.resolve_weight_file_path("characters/kanna/gpt.ckpt") == str(pkg)

    def test_engine_relative_weight_absolutized(self, tmp_path, monkeypatch):
        import galgame2voice.utils.path_guard as pg
        engine = tmp_path / "engine"
        w = engine / "GPT_weights_v2ProPlus" / "kanna-e50.ckpt"
        w.parent.mkdir(parents=True)
        w.write_bytes(b"w")
        monkeypatch.setattr(pg, "get_settings", lambda: SimpleNamespace(project_root=tmp_path, audio_dir=tmp_path / "audio"))
        monkeypatch.setattr(pg, "get_authorized_roots", lambda include_sovits=True: [engine])
        assert pg.resolve_weight_file_path("GPT_weights_v2ProPlus/kanna-e50.ckpt") == str(w)

    def test_absolute_untouched_and_missing_passthrough(self, tmp_path, monkeypatch):
        import galgame2voice.utils.path_guard as pg
        monkeypatch.setattr(pg, "get_settings", lambda: SimpleNamespace(project_root=tmp_path, audio_dir=tmp_path / "audio"))
        monkeypatch.setattr(pg, "get_authorized_roots", lambda include_sovits=True: [])
        assert pg.resolve_weight_file_path("GPT_weights_v2ProPlus/missing.ckpt") == "GPT_weights_v2ProPlus/missing.ckpt"
        assert pg.resolve_weight_file_path("") == ""
