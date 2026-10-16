"""The Docker build context and runtime folders match what the image needs."""

import fnmatch
import logging
from pathlib import Path
import re

from galgame2voice.config import get_settings

ROOT = Path(__file__).resolve().parents[1]


def _dockerignore_patterns():
    patterns = []
    for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            negate = line.startswith("!")
            patterns.append((negate, line.lstrip("!").strip("/")))
    return patterns


def _sent_to_build(path: str) -> bool:
    """Docker semantics for these patterns: last match wins, parents count."""
    parts = path.split("/")
    included = True
    for negate, pattern in _dockerignore_patterns():
        if any(fnmatch.fnmatchcase("/".join(parts[:i]), pattern) for i in range(1, len(parts) + 1)):
            included = negate
    return included


def test_build_context_has_every_copied_path_and_no_user_assets():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    sources = []
    for match in re.finditer(r"^COPY\s+(?!--from)(.+)$", dockerfile, flags=re.MULTILINE):
        sources.extend(match.group(1).split()[:-1])
    assert sources, "Dockerfile COPY instructions not found"
    for source in sources:
        sample = source.replace("*", "")
        sample = f"{sample.rstrip('/')}/sample.txt" if source.endswith("/") else sample
        assert _sent_to_build(sample), f"{source} is copied by the Dockerfile but excluded by .dockerignore"

    for private in (
        "characters/爱丽丝/gpt.ckpt", "characters/爱丽丝/refs/gentle.ogg", "data/galgame2voice.db",
        "data/.master_key", "audio/cache/a.wav", "logs/galgame2voice.log", ".env", "dist/release.zip",
        "build/portable/Galgame2Voice.exe", "tests/test_x.py", "frontend/node_modules/vite/index.js",
        "galgame2voice/__pycache__/main.cpython-312.pyc",
    ):
        assert not _sent_to_build(private), f"{private} would be sent to the Docker build context"


def test_image_and_compose_provide_a_writable_characters_folder():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert re.search(r"mkdir -p [^\n]*/app/characters", dockerfile)
    assert re.search(r"chown -R app:app [^\n]*/app/characters", dockerfile)
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "./characters:/app/characters" in compose


def test_startup_continues_without_a_writable_characters_folder(tmp_path, monkeypatch, caplog):
    import galgame2voice.main as main
    from galgame2voice.services.character_manager import CharacterManager

    monkeypatch.setenv("GALGAME2VOICE_PROJECT_ROOT", str(tmp_path))
    get_settings.cache_clear()
    settings = get_settings()
    original_mkdir = Path.mkdir

    def read_only_characters(self, *args, **kwargs):
        if self == settings.characters_dir:
            raise PermissionError(13, "Permission denied", str(self))
        return original_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", read_only_characters)
    with caplog.at_level(logging.WARNING, logger="galgame2voice.main"):
        main._init_directories(settings)

    assert settings.data_dir.is_dir() and settings.audio_dir.is_dir() and settings.logs_dir.is_dir()
    assert not settings.characters_dir.exists()
    assert "character packages disabled" in caplog.text
    assert CharacterManager(settings.characters_dir).discover_characters() == []
