"""Saved preferences must not masquerade as the running engine's precision."""

from types import SimpleNamespace
from unittest.mock import MagicMock
import sqlite3

from httpx import ASGITransport, AsyncClient
import pytest

from galgame2voice.config import get_settings
from galgame2voice.main import create_app
from galgame2voice.routers import health
from galgame2voice.services.sovits_endpoint import parse_sovits_endpoint
from galgame2voice.utils.precision import resolve_initial_device_and_half
from scripts import run_server


@pytest.mark.asyncio
async def test_saving_precision_never_rewrites_original_engine_config(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "project_root", tmp_path)
    engine = tmp_path / "engine"
    original = engine / "GPT_SoVITS/configs/tts_infer.yaml"
    original.parent.mkdir(parents=True)
    content = "custom:\n  device: cuda\n  is_half: true\n"
    original.write_text(content)
    (engine / "api_v2.py").write_text("# synthetic engine")
    (tmp_path / "data").mkdir()
    (tmp_path / "data/sovits_dir.txt").write_text(str(engine))
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test") as client:
        response = await client.post("/api/config", json={"inference_precision": "cpu"})
    assert response.status_code == 200
    assert response.json()["settings"]["inference_precision"] == "cpu"
    assert original.read_text() == content


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["managed", "exited", "other_endpoint", "external", "replaced_process"])
async def test_status_distinguishes_launch_mode_from_saved_preference(monkeypatch, mode):
    endpoint = parse_sovits_endpoint("http://127.0.0.1:19230", source="db")
    proc = MagicMock()
    proc.poll.return_value = 1 if mode == "exited" else None
    monkeypatch.setattr(run_server, "_SPAWNED_SOVITS_PROC", proc if mode != "external" else None)
    if mode == "replaced_process":
        monkeypatch.setattr(run_server, "_SPAWNED_SOVITS_PROC", MagicMock())
    launch = SimpleNamespace(process=proc, host="127.0.0.1", port=19231 if mode == "other_endpoint" else 19230,
                             device="cuda", is_half=True)
    monkeypatch.setattr(run_server, "_SPAWNED_SOVITS_LAUNCH", launch, raising=False)
    monkeypatch.setattr(health, "_get_gpu_telemetry_cached", lambda: (True, "Synthetic GPU"))
    monkeypatch.setattr(health, "get_system_memory_status", lambda: (16.0, 8.0))
    monkeypatch.setattr(health, "read_db_precision", lambda *args, **kwargs: "cpu")
    monkeypatch.setattr(health, "resolve_initial_device_and_half", lambda *args, **kwargs: ("cpu", False, "db"))

    async def resolve():
        return endpoint

    async def probe(*args):
        return health.GptSovitsTelemetry(status="reachable", base_url=endpoint.base_url)

    monkeypatch.setattr(health, "_resolve_sovits_endpoint_best_effort", resolve)
    monkeypatch.setattr(health, "_probe_gpt_sovits", probe)
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test") as client:
        response = await client.get("/api/system/status")
    assert response.status_code == 200
    hardware = response.json()["hardware"]
    assert hardware["configured_precision"] == "cpu"
    if mode == "managed":
        assert hardware["inference_precision"] == "FP16"
        assert hardware["precision_pending_restart"] is True
        assert hardware["precision_source"] == "managed_launch"
    else:
        assert hardware["inference_precision"] is None
        assert hardware["precision_pending_restart"] is None
        assert hardware["precision_source"] == "unknown"


def test_launch_precision_reads_the_configured_database_instead_of_a_stale_default(tmp_path):
    default = tmp_path / "data/galgame2voice.db"
    default.parent.mkdir()
    custom = tmp_path / "custom.db"
    for path, precision in ((default, "fp16"), (custom, "cpu")):
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE settings (id INTEGER PRIMARY KEY, inference_precision TEXT)")
            connection.execute("INSERT INTO settings VALUES (1, ?)", (precision,))
    assert resolve_initial_device_and_half(tmp_path, tmp_path, environ={}, db_path=custom) == ("cpu", False, "db")
