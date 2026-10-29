"""File and engine identity shared by audio reuse and the inference boundary."""

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from galgame2voice.config import get_settings
from galgame2voice.utils.path_guard import resolve_existing_audio_path, resolve_weight_file_path


def database_scope(path: str | Path) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def _path_text(value: Any) -> str:
    # Mock clients and adapters can expose arbitrary attributes; only paths count.
    return os.fspath(value).strip() if isinstance(value, (str, Path)) else ""


@dataclass(frozen=True)
class SourceFile:
    path: str
    revision: tuple[int, ...] | None

    @classmethod
    def capture(cls, value: Any, *, audio: bool = False) -> "SourceFile":
        raw = _path_text(value)
        if not raw:
            return cls("", None)
        resolved = resolve_existing_audio_path(raw) if audio else resolve_weight_file_path(raw)
        path = Path(resolved or raw)
        if not path.is_absolute():
            path = get_settings().project_root / path
        canonical = database_scope(path)
        try:
            stat = Path(canonical).stat()
            revision = (stat.st_mtime_ns, stat.st_size, stat.st_ctime_ns, stat.st_dev, stat.st_ino)
        except OSError:
            # An external engine can own files unavailable on the application host.
            revision = None
        return cls(canonical, revision)

    def fingerprint(self) -> tuple[str, tuple[int, ...] | None]:
        return self.path, self.revision


@dataclass(frozen=True)
class VoicePreparation:
    """Internal capability issued only after resolving a database voice context."""
    name: str


@dataclass(frozen=True)
class AudioIdentity:
    gpt: SourceFile
    sovits: SourceFile
    reference: SourceFile
    engine_url: str = ""

    @classmethod
    def capture(cls, options: dict[str, Any]) -> "AudioIdentity":
        return cls(
            SourceFile.capture(options.get("gpt_weights_path")),
            SourceFile.capture(options.get("sovits_weights_path")),
            SourceFile.capture(options.get("ref_audio_path") or options.get("refer_audio_path"), audio=True),
            str(options.get("_engine_url") or "").rstrip("/"),
        )

    def validate_sources(self, options: dict[str, Any], client: Any) -> None:
        """Check pinned files and endpoint before preparing or using models."""
        current_files = self.capture(options)
        if (self.gpt, self.sovits, self.reference) != (current_files.gpt, current_files.sovits, current_files.reference):
            raise ValueError("音色文件在等待合成期间发生变化，请重新选择音色后重试。")
        endpoint = _path_text(getattr(client, "base_url", None)).rstrip("/")
        if self.engine_url and endpoint and self.engine_url != endpoint:
            raise ValueError("语音引擎地址已改变，请重新选择音色后重试。")

    def models_match(self, client: Any) -> bool:
        if getattr(client, '_model_state_uncertain', False) is True:
            return False
        for name, expected in (("gpt", self.gpt), ("sovits", self.sovits)):
            current = _path_text(getattr(client, f'current_{name}_weights', None))
            if expected.path and (not current or SourceFile.capture(current).path != expected.path):
                return False
            loaded = getattr(client, f'_loaded_{name}_source', None)
            if isinstance(loaded, SourceFile) and loaded != expected:
                return False
        return True

    def validate(self, options: dict[str, Any], client: Any) -> None:
        """Run after taking the engine mutex, before sending any synthesis bytes."""
        if getattr(client, "_model_state_uncertain", False) is True:
            raise ValueError("语音模型切换尚未完成，请重新选择音色后重试。")
        self.validate_sources(options, client)
        for name, expected in (("gpt", self.gpt), ("sovits", self.sovits)):
            current = _path_text(getattr(client, f"current_{name}_weights", None))
            if not expected.path or not current:
                continue
            if SourceFile.capture(current).path != expected.path:
                raise ValueError("语音引擎当前模型与所选音色不一致，请重新选择音色后重试。")
            loaded = getattr(client, f"_loaded_{name}_source", None)
            if isinstance(loaded, SourceFile) and loaded != expected:
                raise ValueError("音色模型文件已更新，请重新加载音色后重试。")


async def bind_audio_options(options: dict[str, Any], client: Any) -> dict[str, Any]:
    """Pin effective client defaults and capture files without blocking the event loop."""
    opts = dict(options)
    for field, aliases, attr in (
        ("gpt_weights_path", (), "current_gpt_weights"),
        ("sovits_weights_path", (), "current_sovits_weights"),
        ("ref_audio_path", ("refer_audio_path",), "current_refer_audio"),
        ("prompt_text", ("refer_text",), "current_refer_text"),
        ("prompt_lang", ("refer_language", "prompt_language"), "current_refer_language"),
    ):
        if field == "prompt_text" and opts.get("_profile_prompt_resolved") and opts.get(field) is not None:
            continue
        if not opts.get(field) and not any(opts.get(alias) for alias in aliases):
            value = _path_text(getattr(client, attr, None))
            if value:
                opts[field] = value
    endpoint = _path_text(getattr(client, "base_url", None))
    if endpoint:
        opts.setdefault("_engine_url", endpoint.rstrip("/"))
    identity = opts.get("_audio_identity")
    if not isinstance(identity, AudioIdentity):
        if any(_path_text(opts.get(field)) for field in (
            "gpt_weights_path", "sovits_weights_path", "ref_audio_path", "refer_audio_path",
        )):
            identity = await asyncio.to_thread(AudioIdentity.capture, opts)
        else:
            identity = AudioIdentity.capture(opts)
        opts["_audio_identity"] = identity
    return opts
