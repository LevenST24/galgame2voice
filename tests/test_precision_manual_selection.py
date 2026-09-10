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
from galgame2voice.utils.precision import read_precision_cache, write_precision_cache
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


@pytest.mark.asyncio
class TestPrecisionAPI:
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

    async def test_system_status_telemetry_includes_precision(self):
        app = create_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/system/status")
            assert resp.status_code == 200
            data = resp.json()
            assert "hardware" in data
            hw = data["hardware"]
            assert "inference_precision" in hw
            assert hw["inference_precision"] in ("FP16", "FP32")
            assert "configured_precision" in hw

    async def test_restart_sovits_endpoint_missing_sovits_dir(self, tmp_path, monkeypatch):
        settings = get_settings()
        monkeypatch.setattr(settings, "project_root", tmp_path)
        app = create_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/api/system/restart_sovits")
            assert resp.status_code == 400
            assert "data/sovits_dir.txt" in resp.json()["detail"]

    async def test_restart_sovits_endpoint_with_explicit_precision(self, tmp_path, monkeypatch):
        settings = get_settings()
        monkeypatch.setattr(settings, "project_root", tmp_path)
        (tmp_path / "data").mkdir(parents=True, exist_ok=True)
        fake_engine = tmp_path / "dummy_sovits"
        fake_engine.mkdir()
        (tmp_path / "data" / "sovits_dir.txt").write_text(str(fake_engine), encoding="utf-8")

        spawn_calls = []
        class FakeProc:
            pid = 9999

        import scripts.run_server as rs
        monkeypatch.setattr(
            rs,
            "_spawn_sovits_process",
            lambda s_dir, host, port, is_half, device="cuda": spawn_calls.append((is_half, device)) or FakeProc()
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
            assert spawn_calls[-1] == (False, "cuda")
            cache1 = read_precision_cache(tmp_path)
            assert cache1["is_half"] is False

            # 2. Restart with FP16
            r2 = await client.post("/api/system/restart_sovits", json={"precision": "fp16"})
            assert r2.status_code == 200
            data2 = r2.json()
            assert data2["is_half"] is True
            assert data2["precision"] == "FP16"
            assert "FP16" in data2["message"]
            assert spawn_calls[-1] == (True, "cuda")
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
            assert spawn_calls[-1] == (False, "cpu")
            cache3 = read_precision_cache(tmp_path)
            assert cache3["device"] == "cpu"
            assert cache3["is_half"] is False
