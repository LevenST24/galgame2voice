"""Tests for terminating the owned local engine when switching to a remote GPT-SoVITS endpoint."""

import subprocess
from unittest.mock import MagicMock

from httpx import ASGITransport, AsyncClient
import pytest

from galgame2voice.main import create_app
from galgame2voice.routers.config import _apply_sovits_url_update
from galgame2voice.services.engine_runtime import EngineLaunch
from scripts import run_server

HOST = "127.0.0.1"
PORT = 9880
REMOTE_URL = "http://10.0.0.8:9880"
LOCAL_URL = "http://127.0.0.1:9880"


@pytest.fixture(autouse=True)
def clean_sovits_env(monkeypatch, tmp_path):
    """Ensure explicit process env vars do not override DB values in tests and isolate PROJECT_ROOT."""
    monkeypatch.delenv("GALGAME2VOICE_GPT_SOVITS_BASE_URL", raising=False)
    monkeypatch.delenv("GPT_SOVITS_BASE_URL", raising=False)
    monkeypatch.setattr(run_server, "PROJECT_ROOT", tmp_path)


@pytest.mark.asyncio
async def test_saving_remote_url_stops_owned_local_engine(monkeypatch, tmp_path):
    monkeypatch.setattr(run_server, "PROJECT_ROOT", tmp_path)
    pid_file = tmp_path / "gptsovits.pid"
    pid_file.write_text("4242", encoding="utf-8")

    proc = MagicMock(pid=4242)
    proc.poll.return_value = None
    monkeypatch.setattr(run_server, "_SPAWNED_SOVITS_PROC", proc)
    monkeypatch.setattr(
        run_server,
        "_SPAWNED_SOVITS_LAUNCH",
        EngineLaunch(proc, HOST, PORT, "cuda", True, started_at=1.0),
    )
    monkeypatch.setattr(run_server, "_SPAWNED_SOVITS_STARTED_AT", 1.0)

    killed = []

    def terminate(pid, timeout=2.0):
        killed.append(pid)
        proc.poll.return_value = 0

    monkeypatch.setattr(run_server, "terminate_process_tree", terminate)

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/config", json={"gpt_sovits_url": REMOTE_URL})

    assert resp.status_code == 200
    assert resp.json()["effective_sovits_url"] == REMOTE_URL
    assert resp.json()["effective_sovits_source"] == "db"
    assert killed == [4242], f"Expected owned process 4242 to be terminated, got: {killed}"
    assert run_server._SPAWNED_SOVITS_PROC is None
    assert run_server._SPAWNED_SOVITS_LAUNCH is None
    assert run_server._SPAWNED_SOVITS_STARTED_AT is None
    assert not pid_file.exists()


@pytest.mark.asyncio
async def test_saving_local_url_preserves_owned_local_engine(monkeypatch, tmp_path):
    pid_file = tmp_path / "gptsovits.pid"
    pid_file.write_text("4242", encoding="utf-8")

    proc = MagicMock(pid=4242)
    proc.poll.return_value = None
    launch = EngineLaunch(proc, HOST, PORT, "cuda", True, started_at=1.0)
    monkeypatch.setattr(run_server, "_SPAWNED_SOVITS_PROC", proc)
    monkeypatch.setattr(run_server, "_SPAWNED_SOVITS_LAUNCH", launch)
    monkeypatch.setattr(run_server, "_SPAWNED_SOVITS_STARTED_AT", 1.0)

    killed = []
    monkeypatch.setattr(
        run_server,
        "terminate_process_tree",
        lambda pid, timeout=2.0: killed.append(pid),
    )

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/config", json={"gpt_sovits_url": LOCAL_URL})

    assert resp.status_code == 200
    assert resp.json()["effective_sovits_url"] == LOCAL_URL
    assert resp.json()["effective_sovits_source"] == "default"
    assert killed == [], f"Expected no process to be killed, got: {killed}"
    assert run_server._SPAWNED_SOVITS_PROC is proc
    assert run_server._SPAWNED_SOVITS_LAUNCH is launch
    assert run_server._SPAWNED_SOVITS_STARTED_AT == 1.0
    assert pid_file.exists()


@pytest.mark.asyncio
async def test_saving_remote_url_without_owned_engine_does_not_terminate(monkeypatch):
    monkeypatch.setattr(run_server, "_SPAWNED_SOVITS_PROC", None)
    monkeypatch.setattr(run_server, "_SPAWNED_SOVITS_LAUNCH", None)
    monkeypatch.setattr(run_server, "_SPAWNED_SOVITS_STARTED_AT", None)

    killed = []
    monkeypatch.setattr(
        run_server,
        "terminate_process_tree",
        lambda pid, timeout=2.0: killed.append(pid),
    )

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/config", json={"gpt_sovits_url": REMOTE_URL})

    assert resp.status_code == 200
    assert resp.json()["effective_sovits_url"] == REMOTE_URL
    assert resp.json()["effective_sovits_source"] == "db"
    assert killed == []
    assert run_server._SPAWNED_SOVITS_PROC is None
    assert run_server._SPAWNED_SOVITS_LAUNCH is None


@pytest.mark.asyncio
async def test_saving_remote_url_when_owned_engine_already_exited(monkeypatch, tmp_path):
    monkeypatch.setattr(run_server, "PROJECT_ROOT", tmp_path)
    pid_file = tmp_path / "gptsovits.pid"
    pid_file.write_text("4242", encoding="utf-8")

    proc = MagicMock(pid=4242)
    proc.poll.return_value = 1  # already dead
    monkeypatch.setattr(run_server, "_SPAWNED_SOVITS_PROC", proc)
    monkeypatch.setattr(
        run_server,
        "_SPAWNED_SOVITS_LAUNCH",
        EngineLaunch(proc, HOST, PORT, "cuda", True, started_at=1.0),
    )
    monkeypatch.setattr(run_server, "_SPAWNED_SOVITS_STARTED_AT", 1.0)

    killed = []
    monkeypatch.setattr(
        run_server,
        "terminate_process_tree",
        lambda pid, timeout=2.0: killed.append(pid),
    )

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/config", json={"gpt_sovits_url": REMOTE_URL})

    assert resp.status_code == 200
    assert resp.json()["effective_sovits_url"] == REMOTE_URL
    assert resp.json()["effective_sovits_source"] == "db"
    assert killed == [], "Must not call terminate_process_tree on an already dead process"
    assert run_server._SPAWNED_SOVITS_PROC is None
    assert run_server._SPAWNED_SOVITS_LAUNCH is None
    assert run_server._SPAWNED_SOVITS_STARTED_AT is None
    assert not pid_file.exists()


@pytest.mark.asyncio
async def test_explicit_local_env_var_overrides_remote_config_and_preserves_engine(monkeypatch, tmp_path):
    """When an explicit env var forces a local endpoint, saving a remote DB URL does not kill the engine."""
    pid_file = tmp_path / "gptsovits.pid"
    pid_file.write_text("4242", encoding="utf-8")

    monkeypatch.setenv("GALGAME2VOICE_GPT_SOVITS_BASE_URL", LOCAL_URL)
    proc = MagicMock(pid=4242)
    proc.poll.return_value = None
    launch = EngineLaunch(proc, HOST, PORT, "cuda", True, started_at=1.0)
    monkeypatch.setattr(run_server, "_SPAWNED_SOVITS_PROC", proc)
    monkeypatch.setattr(run_server, "_SPAWNED_SOVITS_LAUNCH", launch)
    monkeypatch.setattr(run_server, "_SPAWNED_SOVITS_STARTED_AT", 1.0)

    killed = []
    monkeypatch.setattr(
        run_server,
        "terminate_process_tree",
        lambda pid, timeout=2.0: killed.append(pid),
    )

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/config", json={"gpt_sovits_url": REMOTE_URL})

    assert resp.status_code == 200
    assert resp.json()["effective_sovits_url"] == LOCAL_URL
    assert resp.json()["effective_sovits_source"] == "env"
    assert killed == []
    assert run_server._SPAWNED_SOVITS_PROC is proc
    assert run_server._SPAWNED_SOVITS_LAUNCH is launch
    assert run_server._SPAWNED_SOVITS_STARTED_AT == 1.0
    assert pid_file.exists()


@pytest.mark.asyncio
async def test_explicit_remote_env_var_overrides_local_config_and_stops_engine(monkeypatch, tmp_path):
    """When an explicit env var forces a remote endpoint, saving a local DB URL still stops the engine."""
    pid_file = tmp_path / "gptsovits.pid"
    pid_file.write_text("4242", encoding="utf-8")

    monkeypatch.setenv("GALGAME2VOICE_GPT_SOVITS_BASE_URL", REMOTE_URL)
    proc = MagicMock(pid=4242)
    proc.poll.return_value = None
    monkeypatch.setattr(run_server, "_SPAWNED_SOVITS_PROC", proc)
    monkeypatch.setattr(
        run_server,
        "_SPAWNED_SOVITS_LAUNCH",
        EngineLaunch(proc, HOST, PORT, "cuda", True, started_at=1.0),
    )
    monkeypatch.setattr(run_server, "_SPAWNED_SOVITS_STARTED_AT", 1.0)

    killed = []

    def terminate(pid, timeout=2.0):
        killed.append(pid)
        proc.poll.return_value = 0

    monkeypatch.setattr(run_server, "terminate_process_tree", terminate)

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/config", json={"gpt_sovits_url": LOCAL_URL})

    assert resp.status_code == 200
    assert resp.json()["effective_sovits_url"] == REMOTE_URL
    assert resp.json()["effective_sovits_source"] == "env"
    assert killed == [4242]
    assert run_server._SPAWNED_SOVITS_PROC is None
    assert run_server._SPAWNED_SOVITS_LAUNCH is None
    assert run_server._SPAWNED_SOVITS_STARTED_AT is None
    assert not pid_file.exists()


@pytest.mark.asyncio
async def test_saving_remote_url_preserves_records_when_termination_fails_and_process_alive(monkeypatch, tmp_path):
    """If terminate_process_tree fails and process is still running, records and PID file are preserved."""
    monkeypatch.setattr(run_server, "PROJECT_ROOT", tmp_path)
    pid_file = tmp_path / "gptsovits.pid"
    pid_file.write_text("4242", encoding="utf-8")

    proc = MagicMock(pid=4242)
    proc.poll.return_value = None  # process is still running
    launch = EngineLaunch(proc, HOST, PORT, "cuda", True, started_at=1.0)
    monkeypatch.setattr(run_server, "_SPAWNED_SOVITS_PROC", proc)
    monkeypatch.setattr(run_server, "_SPAWNED_SOVITS_LAUNCH", launch)
    monkeypatch.setattr(run_server, "_SPAWNED_SOVITS_STARTED_AT", 1.0)

    def failing_kill(pid, timeout=2.0):
        raise RuntimeError("process kill failed")

    monkeypatch.setattr(run_server, "terminate_process_tree", failing_kill)

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/config", json={"gpt_sovits_url": REMOTE_URL})

    assert resp.status_code == 200
    assert resp.json()["effective_sovits_url"] == REMOTE_URL
    assert run_server._SPAWNED_SOVITS_PROC is proc
    assert run_server._SPAWNED_SOVITS_LAUNCH is launch
    assert run_server._SPAWNED_SOVITS_STARTED_AT == 1.0
    assert pid_file.exists()


@pytest.mark.asyncio
async def test_saving_remote_url_preserves_records_when_wait_times_out(monkeypatch, tmp_path):
    """If proc.wait times out and process is still running, records and PID file are preserved."""
    monkeypatch.setattr(run_server, "PROJECT_ROOT", tmp_path)
    pid_file = tmp_path / "gptsovits.pid"
    pid_file.write_text("4242", encoding="utf-8")

    proc = MagicMock(pid=4242)
    proc.poll.return_value = None
    proc.wait.side_effect = subprocess.TimeoutExpired(cmd="fake", timeout=3)
    launch = EngineLaunch(proc, HOST, PORT, "cuda", True, started_at=1.0)
    monkeypatch.setattr(run_server, "_SPAWNED_SOVITS_PROC", proc)
    monkeypatch.setattr(run_server, "_SPAWNED_SOVITS_LAUNCH", launch)
    monkeypatch.setattr(run_server, "_SPAWNED_SOVITS_STARTED_AT", 1.0)

    killed = []
    monkeypatch.setattr(
        run_server,
        "terminate_process_tree",
        lambda pid, timeout=2.0: killed.append(pid),
    )

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/config", json={"gpt_sovits_url": REMOTE_URL})

    assert resp.status_code == 200
    assert killed == [4242]
    assert run_server._SPAWNED_SOVITS_PROC is proc
    assert run_server._SPAWNED_SOVITS_LAUNCH is launch
    assert run_server._SPAWNED_SOVITS_STARTED_AT == 1.0
    assert pid_file.exists()


@pytest.mark.asyncio
async def test_direct_apply_sovits_url_update_stops_engine(monkeypatch, tmp_path):
    """Calling _apply_sovits_url_update directly terminates the owned engine and returns effective endpoint."""
    pid_file = tmp_path / "gptsovits.pid"
    pid_file.write_text("4242", encoding="utf-8")

    proc = MagicMock(pid=4242)
    proc.poll.return_value = None
    monkeypatch.setattr(run_server, "_SPAWNED_SOVITS_PROC", proc)
    monkeypatch.setattr(
        run_server,
        "_SPAWNED_SOVITS_LAUNCH",
        EngineLaunch(proc, HOST, PORT, "cuda", True, started_at=1.0),
    )
    monkeypatch.setattr(run_server, "_SPAWNED_SOVITS_STARTED_AT", 1.0)

    killed = []

    def terminate(pid, timeout=2.0):
        killed.append(pid)
        proc.poll.return_value = 0

    monkeypatch.setattr(run_server, "terminate_process_tree", terminate)

    result = await _apply_sovits_url_update(REMOTE_URL)
    assert result == {"base_url": REMOTE_URL, "source": "db"}
    assert killed == [4242]
    assert run_server._SPAWNED_SOVITS_PROC is None
    assert run_server._SPAWNED_SOVITS_LAUNCH is None
    assert run_server._SPAWNED_SOVITS_STARTED_AT is None
    assert not pid_file.exists()


@pytest.mark.asyncio
async def test_saving_remote_url_cleans_records_if_process_exits_after_wait_timeout(monkeypatch, tmp_path):
    """If proc.wait times out but the process has exited when polled, records and PID file are cleaned up."""
    pid_file = tmp_path / "gptsovits.pid"
    pid_file.write_text("4242", encoding="utf-8")

    proc = MagicMock(pid=4242)
    poll_states = [None, 0]
    proc.poll.side_effect = lambda: poll_states.pop(0) if poll_states else 0
    proc.wait.side_effect = subprocess.TimeoutExpired(cmd="fake", timeout=3)

    launch = EngineLaunch(proc, HOST, PORT, "cuda", True, started_at=1.0)
    monkeypatch.setattr(run_server, "_SPAWNED_SOVITS_PROC", proc)
    monkeypatch.setattr(run_server, "_SPAWNED_SOVITS_LAUNCH", launch)
    monkeypatch.setattr(run_server, "_SPAWNED_SOVITS_STARTED_AT", 1.0)

    killed = []
    monkeypatch.setattr(
        run_server,
        "terminate_process_tree",
        lambda pid, timeout=2.0: killed.append(pid),
    )

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/config", json={"gpt_sovits_url": REMOTE_URL})

    assert resp.status_code == 200
    assert killed == [4242]
    assert run_server._SPAWNED_SOVITS_PROC is None
    assert run_server._SPAWNED_SOVITS_LAUNCH is None
    assert run_server._SPAWNED_SOVITS_STARTED_AT is None
    assert not pid_file.exists()
