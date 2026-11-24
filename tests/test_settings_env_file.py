"""The documented .env beside the program applies however the launcher was started."""

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

_PROBE = """
import json, sys
from pathlib import Path
import galgame2voice.runtime_paths as runtime_paths
runtime_paths.get_install_root = lambda: Path(sys.argv[1])
from galgame2voice.config import Settings
s = Settings()
print(json.dumps({"log_level": s.log_level, "debug": s.debug}))
"""


def _settings_seen_from(cwd: Path, install: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GALGAME2VOICE_", "GALGAME_"))}
    env.pop("LOG_LEVEL", None)
    env.pop("DEBUG", None)
    env["PYTHONPATH"] = str(REPO)
    out = subprocess.run(
        [sys.executable, "-c", _PROBE, str(install)], cwd=str(cwd), env=env,
        capture_output=True, text=True, encoding="utf-8", timeout=60, check=True,
    )
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_install_root_env_applies_from_another_working_directory(tmp_path):
    install = tmp_path / "Galgame2Voice"
    install.mkdir()
    (install / ".env").write_text("GALGAME2VOICE_LOG_LEVEL=WARNING\n", encoding="utf-8")
    elsewhere = tmp_path / "home"  # e.g. `python D:\\G2V\\scripts\\run_server.py` from a user folder
    elsewhere.mkdir()
    assert _settings_seen_from(elsewhere, install)["log_level"] == "WARNING"


def test_install_root_env_wins_over_a_stray_env_in_the_working_directory(tmp_path):
    install = tmp_path / "Galgame2Voice"
    install.mkdir()
    (install / ".env").write_text("GALGAME2VOICE_LOG_LEVEL=WARNING\n", encoding="utf-8")
    elsewhere = tmp_path / "home"
    elsewhere.mkdir()
    (elsewhere / ".env").write_text("GALGAME2VOICE_LOG_LEVEL=DEBUG\nGALGAME2VOICE_DEBUG=1\n", encoding="utf-8")
    seen = _settings_seen_from(elsewhere, install)
    assert seen["log_level"] == "WARNING"
    assert seen["debug"] is True  # keys only the working-directory file sets still apply
