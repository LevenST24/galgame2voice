"""Contract tests for GET /api/system/engine-status driven purely via EngineSupervisor DI."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from galgame2voice.main import create_app
from galgame2voice.services.engine_recovery import ENGINE_LOG_START
from galgame2voice.services.engine_runtime import EngineLaunch
from galgame2voice.services.engine_supervisor import EngineSupervisor
from galgame2voice.services.sovits_endpoint import SovitsEndpoint, parse_sovits_endpoint


class FakeProcess:
    """Minimal process stand-in exposing poll()."""

    def __init__(self, exit_code: int | None = None) -> None:
        self.exit_code = exit_code

    def poll(self) -> int | None:
        return self.exit_code


class FakeEngineSupervisor:
    """Test double implementing the EngineSupervisor protocol without global state."""

    def __init__(
        self,
        *,
        endpoint: SovitsEndpoint | Exception | None = None,
        restarting: bool = False,
        launch: Any | None = None,
        reachable: bool = False,
        now: float = 100.0,
        log_path: Path | None = None,
        on_probe: Callable[[FakeEngineSupervisor], None] | None = None,
        on_log_path: Callable[[FakeEngineSupervisor], None] | None = None,
    ) -> None:
        self._endpoint = endpoint or parse_sovits_endpoint("http://127.0.0.1:9880", source="db")
        self.restarting = restarting
        self.launch = launch
        self.reachable = reachable
        self.now = now
        self._log_path = log_path or Path("logs/gpt_sovits.log")
        self._on_probe = on_probe
        self._on_log_path = on_log_path
        self.probe_calls: list[str] = []

    async def resolve_endpoint(self) -> SovitsEndpoint:
        if isinstance(self._endpoint, Exception):
            raise self._endpoint
        return self._endpoint

    def is_restarting(self, endpoint: SovitsEndpoint) -> bool:
        return self.restarting

    def get_managed_launch(self, endpoint: SovitsEndpoint) -> Any | None:
        return self.launch

    async def probe_endpoint(self, base_url: str) -> bool:
        self.probe_calls.append(base_url)
        if self._on_probe is not None:
            self._on_probe(self)
        return self.reachable

    def monotonic(self) -> float:
        return self.now

    def engine_log_path(self) -> Path:
        if self._on_log_path is not None:
            self._on_log_path(self)
        return self._log_path


async def _get_engine_status(supervisor: EngineSupervisor):
    app = create_app(engine_supervisor=supervisor)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8080") as client:
        return await client.get("/api/system/engine-status")


def _local_launch(*, exit_code: int | None = None, started_at: float | None = 100.0) -> EngineLaunch:
    return EngineLaunch(FakeProcess(exit_code), "127.0.0.1", 9880, "cuda", True, started_at)


async def test_engine_status_ready_for_reachable_managed_local_endpoint():
    endpoint = parse_sovits_endpoint("http://127.0.0.1:9880", source="db")
    assert endpoint.is_local is True
    supervisor = FakeEngineSupervisor(
        endpoint=endpoint, launch=_local_launch(exit_code=None, started_at=70.0), reachable=True, now=100.0
    )
    resp = await _get_engine_status(supervisor)
    assert resp.status_code == 200
    assert resp.json() == {
        "state": "ready",
        "ready": True,
        "message": "语音引擎已就绪。可在“角色与会话”中选择音色。",
        "base_url": "http://127.0.0.1:9880",
        "source": "db",
    }


async def test_engine_status_starting_while_managed_process_loads_within_budget():
    supervisor = FakeEngineSupervisor(
        launch=_local_launch(exit_code=None, started_at=70.0), reachable=False, now=100.0
    )
    resp = await _get_engine_status(supervisor)
    body = resp.json()
    assert resp.status_code == 200
    assert body["state"] == "starting" and body["ready"] is False
    assert body["message"] == "引擎正在加载模型，请稍候；文字聊天可继续使用。"


async def test_engine_status_loading_slow_after_three_minutes_unresponsive():
    supervisor = FakeEngineSupervisor(
        launch=_local_launch(exit_code=None, started_at=100.0), reachable=False, now=285.0
    )
    resp = await _get_engine_status(supervisor)
    body = resp.json()
    assert resp.status_code == 200
    assert body["state"] == "loading_slow" and body["ready"] is False
    assert "logs/gpt_sovits.log" in body["message"]


async def test_engine_status_failed_extracts_diagnosis_from_engine_log(tmp_path: Path):
    log_file = tmp_path / "gpt_sovits.log"
    log_file.write_text(f"{ENGINE_LOG_START}\nRuntimeError: CUDA out of memory\n", encoding="utf-8")
    supervisor = FakeEngineSupervisor(
        launch=_local_launch(exit_code=1, started_at=90.0), reachable=False, log_path=log_file
    )
    resp = await _get_engine_status(supervisor)
    body = resp.json()
    assert resp.status_code == 200
    assert body["state"] == "failed" and body["ready"] is False
    assert "内存或显存不足" in body["message"] and "CPU 模式" in body["message"]


@pytest.mark.parametrize("flip_during_probe", [False, True])
async def test_engine_status_starting_when_endpoint_is_restarting(flip_during_probe: bool):
    supervisor = FakeEngineSupervisor(
        restarting=not flip_during_probe,
        launch=_local_launch(exit_code=None, started_at=80.0),
        reachable=True,
        on_probe=(lambda sup: setattr(sup, "restarting", True)) if flip_during_probe else None,
    )
    resp = await _get_engine_status(supervisor)
    body = resp.json()
    assert resp.status_code == 200
    assert body["state"] == "starting" and body["ready"] is False
    assert body["message"] == "引擎正在重启，请等待当前操作完成。"
    assert len(supervisor.probe_calls) == (1 if flip_during_probe else 0)


@pytest.mark.parametrize("phase", ["probe", "log_diagnosis"])
async def test_engine_status_checking_when_managed_launch_switches(phase: str, tmp_path: Path):
    replacement = _local_launch(exit_code=None, started_at=99.0)
    supervisor = FakeEngineSupervisor(
        launch=_local_launch(exit_code=None if phase == "probe" else 1, started_at=80.0),
        reachable=phase == "probe",
        log_path=tmp_path / "missing.log",
        on_probe=(lambda sup: setattr(sup, "launch", replacement)) if phase == "probe" else None,
        on_log_path=(lambda sup: setattr(sup, "launch", replacement)) if phase == "log_diagnosis" else None,
    )
    resp = await _get_engine_status(supervisor)
    body = resp.json()
    assert resp.status_code == 200
    assert body["state"] == "checking" and body["ready"] is False
    assert body["message"] == "引擎进程已变更，正在重新核对服务状态。请稍候，无需再次重启。"


@pytest.mark.parametrize(
    ("raw_url", "expect_local", "reachable", "expected_state"),
    [
        ("http://127.0.0.1:9880", True, False, "unavailable"),
        ("http://192.168.1.50:9880", False, False, "unavailable"),
        ("http://192.168.1.50:9880", False, True, "ready"),
    ],
)
async def test_engine_status_unmanaged_local_and_remote_endpoints(
    raw_url: str, expect_local: bool, reachable: bool, expected_state: str
):
    endpoint = parse_sovits_endpoint(raw_url, source="env")
    assert endpoint.is_local is expect_local
    supervisor = FakeEngineSupervisor(endpoint=endpoint, launch=None, reachable=reachable)
    resp = await _get_engine_status(supervisor)
    body = resp.json()
    assert resp.status_code == 200
    assert body["state"] == expected_state and body["ready"] is reachable
    assert body["base_url"] == raw_url and body["source"] == "env"


async def test_engine_status_returns_400_for_invalid_endpoint_configuration():
    supervisor = FakeEngineSupervisor(endpoint=ValueError("bad url"))
    resp = await _get_engine_status(supervisor)
    assert resp.status_code == 400
    assert resp.json()["detail"] == "语音引擎服务地址无效。请在「语音服务」检查并保存服务地址。"
