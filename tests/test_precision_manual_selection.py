"""
Unit tests for manual precision selection, persistence, CLI flags, and restart endpoint:
1. scripts/run_server.py parse_args with --precision, --fp16, and --fp32.
2. POST /api/config persistence of inference_precision and sync with data/precision.json.
3. GET /api/system/status reporting of inference_precision and configured_precision.
4. POST /api/system/restart_sovits error handling and success flow.
"""

import os
import json
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
from httpx import AsyncClient, ASGITransport

from galgame2voice.main import create_app
from galgame2voice.config import get_settings
from galgame2voice.services.sovits_endpoint import parse_sovits_endpoint
from galgame2voice.services.engine_runtime import EngineLaunch
from galgame2voice.utils.precision import (
    read_precision_cache,
    write_precision_cache,
    resolve_initial_device_and_half,
)
from galgame2voice.routers.health import _collect_hardware_telemetry_sync
import scripts.run_server as rs


class TestPrecisionCLI:
    def test_parse_args_precision_defaults_none(self):
        args = rs.parse_args([])
        assert args.precision is None
        assert args.fp16 is False
        assert args.fp32 is False
        assert args.cpu is False

    def test_parse_args_cpu_flag(self):
        args = rs.parse_args(["--cpu"])
        assert args.cpu is True

    def test_parse_args_fp16_flag(self):
        args = rs.parse_args(["--fp16"])
        assert args.fp16 is True

    def test_parse_args_fp32_flag(self):
        args = rs.parse_args(["--fp32"])
        assert args.fp32 is True

    def test_parse_args_precision_choices(self):
        args_fp16 = rs.parse_args(["--precision", "fp16"])
        assert args_fp16.precision == "fp16"

        args_fp32 = rs.parse_args(["--precision", "fp32"])
        assert args_fp32.precision == "fp32"

        args_cpu = rs.parse_args(["--precision", "cpu"])
        assert args_cpu.precision == "cpu"

        args_auto = rs.parse_args(["--precision", "auto"])
        assert args_auto.precision == "auto"

    def test_sync_runtime_precision_pure_mapping(self):
        env = {}
        assert rs.sync_runtime_precision(environ=env) is None
        assert env == {}

        # --cpu
        assert rs.sync_runtime_precision(cpu=True, environ=env) == "cpu"
        assert env["GPT_SOVITS_PRECISION"] == "cpu"
        assert env["GPT_SOVITS_DEVICE"] == "cpu"

        # --fp16 removes leftover cpu device
        assert rs.sync_runtime_precision(fp16=True, environ=env) == "fp16"
        assert env["GPT_SOVITS_PRECISION"] == "fp16"
        assert "GPT_SOVITS_DEVICE" not in env

        # --fp32
        assert rs.sync_runtime_precision(fp32=True, environ=env) == "fp32"
        assert env["GPT_SOVITS_PRECISION"] == "fp32"

        # --precision auto removes leftover cpu device if present
        env["GPT_SOVITS_DEVICE"] = "cpu"
        assert rs.sync_runtime_precision(precision="auto", environ=env) == "auto"
        assert env["GPT_SOVITS_PRECISION"] == "auto"
        assert "GPT_SOVITS_DEVICE" not in env

        # --precision variants and case-insensitivity
        assert rs.sync_runtime_precision(precision="cpu", environ=env) == "cpu"
        assert env["GPT_SOVITS_PRECISION"] == "cpu"
        assert env["GPT_SOVITS_DEVICE"] == "cpu"

        assert rs.sync_runtime_precision(precision="FP16", environ=env) == "fp16"
        assert env["GPT_SOVITS_PRECISION"] == "fp16"
        assert "GPT_SOVITS_DEVICE" not in env

        assert rs.sync_runtime_precision(precision="FP32", environ=env) == "fp32"
        assert env["GPT_SOVITS_PRECISION"] == "fp32"

        assert rs.sync_runtime_precision(precision="AUTO", environ=env) == "auto"
        assert env["GPT_SOVITS_PRECISION"] == "auto"

        # Precedence: cpu > fp16 > fp32 > auto
        assert rs.sync_runtime_precision(cpu=True, fp16=True, fp32=True, precision="auto", environ=env) == "cpu"
        assert rs.sync_runtime_precision(fp16=True, fp32=True, precision="auto", environ=env) == "fp16"
        assert rs.sync_runtime_precision(fp32=True, precision="auto", environ=env) == "fp32"


@pytest.mark.asyncio
class TestPrecisionAPI:
    @pytest.fixture(autouse=True)
    def clean_precision_env(self):
        os.environ.pop("GPT_SOVITS_PRECISION", None)
        os.environ.pop("GPT_SOVITS_DEVICE", None)
        try:
            yield
        finally:
            os.environ.pop("GPT_SOVITS_PRECISION", None)
            os.environ.pop("GPT_SOVITS_DEVICE", None)

    async def test_update_precision_in_config_and_cache(self, tmp_path, monkeypatch):
        settings = get_settings()
        monkeypatch.setattr(settings, "project_root", tmp_path)
        (tmp_path / "data").mkdir(parents=True, exist_ok=True)
        (tmp_path / "data" / "sovits_dir.txt").write_text("D:/dummy_sovits", encoding="utf-8")

        app = create_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # Update to fp16
            resp = await client.post("/api/config", json={"settings": {"inference_precision": "fp16"}})
            assert resp.status_code == 200
            data = resp.json()
            assert data["settings"]["inference_precision"] == "fp16"

            # Check precision.json was written
            cache = read_precision_cache(tmp_path)
            assert cache is not None
            assert cache["is_half"] is True

            # Update to fp32
            resp2 = await client.post("/api/config", json={"settings": {"inference_precision": "fp32"}})
            assert resp2.status_code == 200
            cache2 = read_precision_cache(tmp_path)
            assert cache2 is not None
            assert cache2["is_half"] is False

            # Update to auto (should clear cache)
            resp3 = await client.post("/api/config", json={"settings": {"inference_precision": "auto"}})
            assert resp3.status_code == 200
            cache3 = read_precision_cache(tmp_path)
            assert cache3 is None

    async def test_auto_precision_preserves_launcher_calibration_on_config_save(self, tmp_path, monkeypatch):
        """When settings.inference_precision is 'auto' (default), saving unrelated global
        settings that include 'inference_precision: auto' must NOT delete the launcher-calibrated
        data/precision.json cache file. Only switching from manual (e.g. fp32) to auto clears it."""
        settings = get_settings()
        monkeypatch.setattr(settings, "project_root", tmp_path)
        (tmp_path / "data").mkdir(parents=True, exist_ok=True)
        (tmp_path / "data" / "sovits_dir.txt").write_text("D:/dummy_sovits", encoding="utf-8")

        # Simulate launcher calibration having written precision.json for GTX 16xx (is_half=False, device="cuda")
        write_precision_cache(tmp_path, "D:/dummy_sovits", is_half=False, device="cuda")
        initial_cache = read_precision_cache(tmp_path)
        assert initial_cache is not None
        assert initial_cache["is_half"] is False
        assert initial_cache["device"] == "cuda"

        app = create_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # 1. DB inference_precision defaults to 'auto'. Save unrelated global settings carrying inference_precision: "auto"
            resp = await client.post(
                "/api/config",
                json={"settings": {"audio_retention_minutes": 60, "inference_precision": "auto"}},
            )
            assert resp.status_code == 200
            assert resp.json()["settings"]["audio_retention_minutes"] == 60
            assert resp.json()["settings"]["inference_precision"] == "auto"

            # Cache file MUST remain intact
            preserved_cache = read_precision_cache(tmp_path)
            assert preserved_cache is not None, "precision.json should be preserved when precision is already auto"
            assert preserved_cache["is_half"] is False
            assert preserved_cache["device"] == "cuda"

            # 2. Switch to manual fp32
            resp2 = await client.post("/api/config", json={"settings": {"inference_precision": "fp32"}})
            assert resp2.status_code == 200
            assert read_precision_cache(tmp_path)["is_half"] is False

            # 3. Switching back from manual fp32 to auto SHOULD clear precision.json
            resp3 = await client.post("/api/config", json={"settings": {"inference_precision": "auto"}})
            assert resp3.status_code == 200
            assert read_precision_cache(tmp_path) is None

    async def test_system_status_telemetry_includes_precision(self, monkeypatch):
        monkeypatch.setattr(
            "galgame2voice.utils.hardware.detect_gpu_capability",
            lambda: (True, "Mock GPU", 0),
        )
        app = create_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/system/status")
            assert resp.status_code == 200
            data = resp.json()
            assert "hardware" in data
            hw = data["hardware"]
            assert "inference_precision" in hw
            assert hw["inference_precision"] is None
            assert hw["precision_source"] == "unknown"
            assert "configured_precision" in hw

    async def test_restart_sovits_endpoint_missing_sovits_dir(self, tmp_path, monkeypatch):
        settings = get_settings()
        monkeypatch.setattr(settings, "project_root", tmp_path)
        app = create_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/api/system/restart_sovits")
            assert resp.status_code == 400
            assert "选择文件夹" in resp.json()["detail"]

    async def test_restart_sovits_endpoint_with_explicit_precision(self, tmp_path, monkeypatch):
        settings = get_settings()
        monkeypatch.setattr(settings, "project_root", tmp_path)
        (tmp_path / "data").mkdir(parents=True, exist_ok=True)
        fake_engine = tmp_path / "dummy_sovits"
        fake_engine.mkdir()
        (fake_engine / "api_v2.py").write_text("# test engine marker\n")
        monkeypatch.setattr(rs, "_SPAWNED_SOVITS_PROC", None)
        monkeypatch.setattr(rs, "is_port_in_use", lambda *args: False)
        (tmp_path / "data" / "sovits_dir.txt").write_text(str(fake_engine), encoding="utf-8")

        # Pin the effective endpoint to a local custom port so the test proves
        # the spawned host/port come from the resolver and are NOT hardcoded 9880.
        async def _local_custom_resolver():
            return parse_sovits_endpoint("http://127.0.0.1:9999", source="db")

        monkeypatch.setattr(
            "galgame2voice.routers.health.resolve_effective_sovits_endpoint",
            _local_custom_resolver,
        )

        spawn_calls = []
        class FakeProc:
            pid = 9999

            def poll(self):
                return None

        monkeypatch.setattr(
            rs,
            "_spawn_sovits_process",
            lambda s_dir, host, port, is_half, device="cuda": spawn_calls.append(
                (host, port, is_half, device)
            ) or FakeProc()
        )

        app = create_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # 1. Restart with FP32
            r1 = await client.post("/api/system/restart_sovits", json={"precision": "fp32"})
            assert r1.status_code == 200
            data1 = r1.json()
            assert data1["is_half"] is False
            assert data1["precision"] == "FP32"
            assert "FP32" in data1["message"]
            assert spawn_calls[-1] == ("127.0.0.1", 9999, False, "cuda")
            cache1 = read_precision_cache(tmp_path)
            assert cache1["is_half"] is False

            # 2. Restart with FP16
            r2 = await client.post("/api/system/restart_sovits", json={"precision": "fp16"})
            assert r2.status_code == 200
            data2 = r2.json()
            assert data2["is_half"] is True
            assert data2["precision"] == "FP16"
            assert "FP16" in data2["message"]
            assert spawn_calls[-1] == ("127.0.0.1", 9999, True, "cuda")
            cache2 = read_precision_cache(tmp_path)
            assert cache2["is_half"] is True

            # 3. Restart with CPU
            r3 = await client.post("/api/system/restart_sovits", json={"precision": "cpu"})
            assert r3.status_code == 200
            data3 = r3.json()
            assert data3["is_half"] is False
            assert data3["device"] == "cpu"
            assert data3["precision"] == "CPU"
            assert "CPU" in data3["message"]
            assert spawn_calls[-1] == ("127.0.0.1", 9999, False, "cpu")
            cache3 = read_precision_cache(tmp_path)
            assert cache3["device"] == "cpu"
            assert cache3["is_half"] is False

    async def test_restart_sovits_auto_preserves_calibrated_cache_and_clears_on_manual_switch(
        self, tmp_path, monkeypatch
    ):
        settings = get_settings()
        monkeypatch.setattr(settings, "project_root", tmp_path)
        (tmp_path / "data").mkdir(parents=True, exist_ok=True)
        fake_engine = tmp_path / "dummy_sovits"
        fake_engine.mkdir()
        (fake_engine / "api_v2.py").write_text("# test engine marker\n")
        (tmp_path / "data" / "sovits_dir.txt").write_text(str(fake_engine), encoding="utf-8")

        monkeypatch.setattr(rs, "_SPAWNED_SOVITS_PROC", None)
        monkeypatch.setattr(rs, "is_port_in_use", lambda *args: False)
        monkeypatch.setattr(
            "galgame2voice.utils.hardware.detect_gpu_capability",
            lambda: (True, "Mock NVIDIA GPU", 0),
        )

        async def _local_custom_resolver():
            return parse_sovits_endpoint("http://127.0.0.1:9880", source="db")

        monkeypatch.setattr(
            "galgame2voice.routers.health.resolve_effective_sovits_endpoint",
            _local_custom_resolver,
        )

        spawn_calls = []

        class FakeProc:
            pid = 9999

            def poll(self):
                return None

        monkeypatch.setattr(
            rs,
            "_spawn_sovits_process",
            lambda s_dir, host, port, is_half, device="cuda": spawn_calls.append(
                (host, port, is_half, device)
            ) or FakeProc(),
        )

        # 1. Launcher has calibrated FP32 cache (is_half=False, device="cuda")
        write_precision_cache(tmp_path, str(fake_engine), is_half=False, device="cuda")
        initial_cache = read_precision_cache(tmp_path)
        assert initial_cache is not None
        assert initial_cache["is_half"] is False
        assert initial_cache["device"] == "cuda"

        app = create_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # 1. Default auto state (before settings row is explicitly written) preserves calibrated precision.json
            r1 = await client.post("/api/system/restart_sovits", json={"precision": "auto"})
            assert r1.status_code == 200
            data1 = r1.json()
            assert data1["is_half"] is False
            assert data1["precision"] == "FP32"
            assert data1["device"] == "cuda"
            assert spawn_calls[-1] == ("127.0.0.1", 9880, False, "cuda")
            assert read_precision_cache(tmp_path) is not None

            # 2. Explicit "auto" row in DB still preserves calibrated precision.json on subsequent auto restart
            r2 = await client.post("/api/system/restart_sovits", json={"precision": "auto"})
            assert r2.status_code == 200
            data2 = r2.json()
            assert data2["is_half"] is False
            assert data2["precision"] == "FP32"
            assert data2["device"] == "cuda"
            assert spawn_calls[-1] == ("127.0.0.1", 9880, False, "cuda")
            cache_after = read_precision_cache(tmp_path)
            assert cache_after is not None, "precision.json should be preserved when DB precision is auto"
            assert cache_after["is_half"] is False
            assert cache_after["device"] == "cuda"

            # 3. Switch to manual fp32
            r3 = await client.post("/api/system/restart_sovits", json={"precision": "fp32"})
            assert r3.status_code == 200
            assert spawn_calls[-1] == ("127.0.0.1", 9880, False, "cuda")
            cache_manual = read_precision_cache(tmp_path)
            assert cache_manual is not None
            assert cache_manual["is_half"] is False

            # 4. Switching back from manual fp32 to auto clears precision.json and reverts to fallback
            r4 = await client.post("/api/system/restart_sovits", json={"precision": "auto"})
            assert r4.status_code == 200
            assert read_precision_cache(tmp_path) is None
            data4 = r4.json()
            assert data4["is_half"] is True
            assert data4["precision"] == "FP16"
            assert data4["device"] == "cuda"
            assert spawn_calls[-1] == ("127.0.0.1", 9880, True, "cuda")

    async def test_restart_sovits_endpoint_remote_returns_409(self, monkeypatch):
        """A remote effective endpoint must not be managed by this host:
        restart returns 409 REMOTE_SOVITS_NOT_MANAGED and never spawns."""
        async def _remote_resolver():
            return parse_sovits_endpoint("http://10.0.0.20:9880", source="db")

        monkeypatch.setattr(
            "galgame2voice.routers.health.resolve_effective_sovits_endpoint",
            _remote_resolver,
        )

        spawn_calls = []
        class FakeProc:
            pid = 9999

        import scripts.run_server as rs
        def _recording_spawn(*args, **kwargs):
            spawn_calls.append((args, kwargs))
            return FakeProc()

        monkeypatch.setattr(rs, "_spawn_sovits_process", _recording_spawn)

        app = create_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/api/system/restart_sovits", json={"precision": "fp32"})
            assert resp.status_code == 409
            detail = resp.json()["detail"]
            assert detail["code"] == "REMOTE_SOVITS_NOT_MANAGED"
            assert detail["base_url"] == "http://10.0.0.20:9880"
            assert spawn_calls == []

    async def test_sync_runtime_precision_telemetry_cpu_and_fp32(self, tmp_path, monkeypatch):
        settings = get_settings()
        monkeypatch.setattr(settings, "project_root", tmp_path)
        (tmp_path / "data").mkdir(parents=True, exist_ok=True)
        fake_engine = tmp_path / "dummy_sovits"
        fake_engine.mkdir()
        (fake_engine / "api_v2.py").write_text("# dummy")
        (tmp_path / "data" / "sovits_dir.txt").write_text(str(fake_engine), encoding="utf-8")

        app = create_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # Seed SQLite with manual fp16 to ensure CLI flags override stale DB entries
            resp = await client.post("/api/config", json={"settings": {"inference_precision": "fp16"}})
            assert resp.status_code == 200

        endpoint = parse_sovits_endpoint("http://127.0.0.1:9880", source="default")

        class FakeProc:
            def poll(self):
                return None

        proc = FakeProc()
        monkeypatch.setattr(rs, "_SPAWNED_SOVITS_PROC", proc)

        # 1. Test --cpu
        os.environ.pop("GPT_SOVITS_PRECISION", None)
        os.environ.pop("GPT_SOVITS_DEVICE", None)
        rs.sync_runtime_precision(cpu=True)
        monkeypatch.setattr(
            rs,
            "_SPAWNED_SOVITS_LAUNCH",
            EngineLaunch(proc, "127.0.0.1", 9880, "cpu", False),
        )

        telem_cpu = _collect_hardware_telemetry_sync(endpoint, gpu_snapshot=(True, "NVIDIA GeForce RTX 3060"))
        assert telem_cpu.configured_precision == "cpu"
        assert telem_cpu.inference_precision == "CPU"
        assert telem_cpu.next_inference_precision == "CPU"
        assert telem_cpu.precision_pending_restart is False

        # 2. Test --fp32
        os.environ.pop("GPT_SOVITS_PRECISION", None)
        os.environ.pop("GPT_SOVITS_DEVICE", None)
        rs.sync_runtime_precision(fp32=True)
        monkeypatch.setattr(
            rs,
            "_SPAWNED_SOVITS_LAUNCH",
            EngineLaunch(proc, "127.0.0.1", 9880, "cuda", False),
        )

        telem_fp32 = _collect_hardware_telemetry_sync(endpoint, gpu_snapshot=(True, "NVIDIA GeForce RTX 3060"))
        assert telem_fp32.configured_precision == "fp32"
        assert telem_fp32.inference_precision == "FP32"
        assert telem_fp32.next_inference_precision == "FP32"
        assert telem_fp32.precision_pending_restart is False

    async def test_cli_precision_auto_skips_db_and_retains_cache(self, tmp_path, monkeypatch):
        settings = get_settings()
        monkeypatch.setattr(settings, "project_root", tmp_path)
        (tmp_path / "data").mkdir(parents=True, exist_ok=True)
        fake_engine = tmp_path / "dummy_sovits"
        fake_engine.mkdir()
        (fake_engine / "api_v2.py").write_text("# dummy")
        (tmp_path / "data" / "sovits_dir.txt").write_text(str(fake_engine), encoding="utf-8")

        app = create_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # 1. Put manual fp16 into SQLite database via /api/config
            resp = await client.post("/api/config", json={"settings": {"inference_precision": "fp16"}})
            assert resp.status_code == 200

            # Seed calibration cache with FP32 (is_half=False) to prove DB fp16 is bypassed
            write_precision_cache(tmp_path, str(fake_engine), is_half=False, device="cuda")
            assert read_precision_cache(tmp_path)["is_half"] is False

            # 2. Simulate launcher starting with --precision auto
            rs.sync_runtime_precision(precision="auto")

            # resolve_initial_device_and_half should skip DB fp16 and hit precision.json cache
            dev, is_half, source = resolve_initial_device_and_half(
                tmp_path, fake_engine, db_path=settings.db_path, gpu_available=True
            )
            assert dev == "cuda"
            assert is_half is False
            assert source == "cache"

            # Check telemetry reports configured_precision as "auto" despite DB holding "fp16"
            endpoint = parse_sovits_endpoint("http://127.0.0.1:9880", source="default")
            telem_auto = _collect_hardware_telemetry_sync(endpoint, gpu_snapshot=(True, "NVIDIA GeForce RTX 3060"))
            assert telem_auto.configured_precision == "auto"

            # 3. GET /api/config should align settings.inference_precision to "auto"
            cfg_resp = await client.get("/api/config")
            assert cfg_resp.status_code == 200
            assert cfg_resp.json()["settings"]["inference_precision"] == "auto"

            # 4. POST /api/system/restart_sovits without precision parameter should preserve auto mode
            spawn_calls = []
            class FakeProc:
                pid = 9999
                def poll(self):
                    return None

            monkeypatch.setattr(rs, "_SPAWNED_SOVITS_PROC", None)
            monkeypatch.setattr(rs, "is_port_in_use", lambda *args: False)
            monkeypatch.setattr(
                rs,
                "_spawn_sovits_process",
                lambda s_dir, host, port, half, device="cuda": spawn_calls.append(
                    (host, port, half, device)
                ) or FakeProc(),
            )

            r_restart = await client.post("/api/system/restart_sovits")
            assert r_restart.status_code == 200
            assert r_restart.json()["is_half"] is False
            assert r_restart.json()["precision"] == "FP32"
            assert spawn_calls[-1] == ("127.0.0.1", 9880, False, "cuda")
