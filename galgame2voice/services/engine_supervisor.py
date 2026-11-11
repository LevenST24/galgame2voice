"""Injectable supervision seam for reading local/remote GPT-SoVITS engine state."""

from pathlib import Path
from typing import Any, Protocol

from fastapi import Request

from galgame2voice.services.sovits_endpoint import SovitsEndpoint


class EngineSupervisor(Protocol):
    """Read-only supervision seam consumed by GET /api/system/engine-status."""

    async def resolve_endpoint(self) -> SovitsEndpoint: ...
    def is_restarting(self, endpoint: SovitsEndpoint) -> bool: ...
    def get_managed_launch(self, endpoint: SovitsEndpoint) -> Any | None: ...
    async def probe_endpoint(self, base_url: str) -> bool: ...
    def monotonic(self) -> float: ...
    def engine_log_path(self) -> Path: ...


class DefaultEngineSupervisor:
    """Production adapter delegating dynamically to the existing runtime state."""

    async def resolve_endpoint(self) -> SovitsEndpoint:
        from galgame2voice.routers import health

        return await health.resolve_effective_sovits_endpoint()

    def is_restarting(self, endpoint: SovitsEndpoint) -> bool:
        from galgame2voice.routers import health

        return health._restarting_endpoint(endpoint)

    def get_managed_launch(self, endpoint: SovitsEndpoint) -> Any | None:
        from scripts import run_server

        return run_server.get_managed_engine_launch(endpoint)

    async def probe_endpoint(self, base_url: str) -> bool:
        from galgame2voice.routers import health

        return (await health._probe_gpt_sovits(base_url)).status == "reachable"

    def monotonic(self) -> float:
        from scripts import run_server

        return run_server.time.monotonic()

    def engine_log_path(self) -> Path:
        from galgame2voice.routers import health

        return health.get_settings().project_root / "logs" / "gpt_sovits.log"


def get_engine_supervisor(request: Request | None = None) -> EngineSupervisor:
    """Returns the app-injected EngineSupervisor, falling back to DefaultEngineSupervisor."""
    if request is not None:
        supervisor = getattr(request.app.state, "engine_supervisor", None)
        if supervisor is not None:
            return supervisor
    return DefaultEngineSupervisor()


__all__ = [
    "DefaultEngineSupervisor",
    "EngineSupervisor",
    "get_engine_supervisor",
]
