"""Adapt legacy engine entrypoints without modifying the installed engine."""

import hashlib
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any


@dataclass(frozen=True)
class EngineLaunch:
    """The parameters handed to a specific owned process, independent of preferences."""

    process: Any
    host: str
    port: int
    device: str
    is_half: bool
    started_at: float | None = None


_LEGACY_TEMP = re.compile(
    r'^_temp_dir\s*=\s*os\.path\.join\(now_dir,\s*[\'"]TEMP[\'"]\)\s*$',
    re.MULTILINE,
)


def prepare_engine_entrypoint(engine_dir: Path, cache_dir: Path) -> Path:
    """Honor TMPDIR in the known legacy API that unconditionally selects engine/TEMP.

    Keep the engine as the working directory, preserving its relative imports and
    model paths. Other entrypoints are executed unchanged; no general source rewrite
    or changes to the engine installation are performed.
    """
    original = engine_dir / "api_v2.py"
    source = original.read_text(encoding="utf-8-sig")
    adapted, count = _LEGACY_TEMP.subn(
        '_temp_dir = os.environ.get("TMPDIR") or os.path.join(now_dir, "TEMP")', source,
    )
    if not count:
        return original
    digest = hashlib.sha256((str(original.resolve()) + "\0" + adapted).encode()).hexdigest()[:20]
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / f"api_v2-{digest}.py"
    target.write_text(adapted, encoding="utf-8")
    return target


_ENGINE_GUARD_SOURCE = """\
import runpy
import sys
from urllib.parse import urlsplit
from fastapi import FastAPI
from starlette.responses import PlainTextResponse
class EngineGuardMiddleware:
    def __init__(self, app):
        self.app = app
        self.allowed = {"127.0.0.1", "localhost", "::1"}
        for i, arg in enumerate(sys.argv[1:-1], 1):
            if arg == "-a" and (h := sys.argv[i + 1].strip("[]").lower()) not in {"", "0.0.0.0", "::"}:
                self.allowed.add(h)
    async def __call__(self, scope, recv, send):
        if scope["type"] == "http":
            headers = {k.lower(): v.decode("latin-1") for k, v in scope.get("headers") or ()}
            host = urlsplit("//" + headers.get(b"host", "").strip()).hostname
            site = headers.get(b"sec-fetch-site", "").strip().lower()
            if host not in self.allowed or (site and site not in {"none", "same-origin"}):
                return await PlainTextResponse("语音引擎只接受本程序自己的请求。", status_code=403)(scope, recv, send)
        await self.app(scope, recv, send)
if __name__ == "__main__":
    _orig_init = FastAPI.__init__
    def _guarded_init(self, *a, **kw):
        _orig_init(self, *a, **kw)
        self.add_middleware(EngineGuardMiddleware)
    FastAPI.__init__ = _guarded_init
    entry = sys.argv[1]
    sys.argv = [entry, *sys.argv[2:]]
    runpy.run_path(entry, run_name="__main__")
"""


def write_engine_guard(cache_dir: Path) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    (target := cache_dir / "engine_guard.py").write_text(_ENGINE_GUARD_SOURCE, encoding="utf-8")
    return target
