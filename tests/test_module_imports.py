"""Verify every galgame2voice module can be imported first in a fresh Python process."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
import pkgutil
import subprocess
import sys

import galgame2voice

# Modules that legitimately require optional dependencies not installed in the base test env.
EXCLUDED_MODULES: dict[str, str] = {}


def _discover_modules() -> list[str]:
    modules = [galgame2voice.__name__]
    for info in sorted(
        pkgutil.walk_packages(galgame2voice.__path__, prefix=f"{galgame2voice.__name__}."),
        key=lambda item: item.name,
    ):
        if info.name in EXCLUDED_MODULES:
            continue
        modules.append(info.name)
    return modules


def _import_in_fresh_process(module_name: str) -> tuple[str, int, str]:
    proc = subprocess.run(
        [sys.executable, "-c", f"import {module_name}"],
        capture_output=True,
        text=True,
        timeout=30,
        env=os.environ.copy(),
    )
    return module_name, proc.returncode, proc.stderr.strip()


def test_all_galgame2voice_modules_import_cleanly() -> None:
    modules = _discover_modules()
    max_workers = min(8, os.cpu_count() or 4)
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        results = list(pool.map(_import_in_fresh_process, modules))

    failures = [
        (name, code, stderr)
        for name, code, stderr in results
        if code != 0
    ]
    if failures:
        details = "\n\n".join(
            f"[{name}] exited with {code}:\n{stderr}"
            for name, code, stderr in failures
        )
        raise AssertionError(f"{len(failures)} module(s) failed standalone import:\n\n{details}")

