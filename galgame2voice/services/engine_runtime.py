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
