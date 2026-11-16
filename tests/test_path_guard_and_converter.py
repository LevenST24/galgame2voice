"""
Unit & Integration tests for:
1. Path Traversal & File Boundary Guard (galgame2voice.utils.path_guard)
2. Persistent FFmpeg Discovery, Caching & Format Short-Circuiting (galgame2voice.utils.audio_converter)
3. In-Memory Disk Cache Metadata Tracking in TtsCacheManager (galgame2voice.services.tts_cache_manager)
"""

import asyncio
import io
import os
import struct
import sys
import tempfile
import wave
from pathlib import Path
from unittest.mock import MagicMock, patch

import imageio_ffmpeg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from galgame2voice.config import get_settings
from galgame2voice.database.session import init_db
from galgame2voice.main import create_app
from galgame2voice.routers.health import DatabaseTelemetry, router as health_router
from galgame2voice.utils.path_guard import (
    PathTraversalError,
    contains_traversal_payload,
    is_safe_filename,
    is_windows_device_name,
    safe_resolve_audio_path,
    validate_path_containment,
    validate_voice_profile_paths,
)
from galgame2voice.utils.audio_converter import (
    convert_ogg_to_wav,
    convert_wav_to_ogg,
    find_ffmpeg,
    is_ffmpeg_available,
    is_target_wav_pcm,
    reset_ffmpeg_cache,
)
from galgame2voice.services.tts_cache_manager import TtsCacheManager


def _create_wav(sample_rate: int = 16000, channels: int = 1, sampwidth: int = 2, duration_s: float = 0.1) -> bytes:
    """Helper creating raw WAV PCM bytes."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sampwidth)
        wf.setframerate(sample_rate)
        num_frames = int(sample_rate * duration_s)
        wf.writeframes(struct.pack(f"<{num_frames * channels}h", *([0] * (num_frames * channels))))
    return buf.getvalue()


# ============================================================================
# 1. Path Guard Tests
# ============================================================================

def test_windows_device_names():
    assert is_windows_device_name("CON")
    assert is_windows_device_name("prn.txt")
    assert is_windows_device_name("aux.wav")
    assert is_windows_device_name("NUL")
    assert is_windows_device_name("COM1")
    assert is_windows_device_name("lpt9.bin")
    assert not is_windows_device_name("console.wav")
    assert not is_windows_device_name("gentle.ogg")


def test_contains_traversal_payload():
    assert contains_traversal_payload("../etc/passwd")
    assert contains_traversal_payload("..\\windows\\system32")
    assert contains_traversal_payload("audio/../../secret.txt")
    assert contains_traversal_payload("%2e%2e/passwd")
    assert contains_traversal_payload("audio\x00/file.wav")
    assert contains_traversal_payload(r"\\remote-server\share\audio.wav")
    assert contains_traversal_payload("//remote-server/share/audio.wav")
    assert not contains_traversal_payload("audio/references/natsume/gentle.ogg")
    assert not contains_traversal_payload("nat002_032.ogg")


def test_is_safe_filename():
    assert is_safe_filename("audio_123.wav")
    assert is_safe_filename("gentle.ogg")
    assert not is_safe_filename("../audio.wav")
    assert not is_safe_filename("sub/audio.wav")
    assert not is_safe_filename("aux.wav")
    assert not is_safe_filename("")


def test_validate_path_containment_valid():
    settings = get_settings()
    ref_audio = settings.project_root / "audio" / "references" / "natsume" / "gentle.ogg"
    if ref_audio.is_file():
        resolved = validate_path_containment("audio/references/natsume/gentle.ogg")
        assert resolved == ref_audio.resolve()


def test_validate_path_containment_rejection():
    with pytest.raises(PathTraversalError):
        validate_path_containment("../../windows/system32/cmd.exe")

    with pytest.raises(PathTraversalError):
        validate_path_containment(r"\\192.168.1.100\share\evil.wav")

    with pytest.raises(PathTraversalError):
        validate_path_containment("CON")


def test_validate_voice_profile_paths_rejection():
    with pytest.raises(PathTraversalError):
        validate_voice_profile_paths(
            gpt_weights_path="../../secret.ckpt",
            sovits_weights_path="models/model.pth",
        )


@pytest.mark.asyncio
async def test_multi_drive_and_nested_gpt_sovits_discovery_and_guard(tmp_path, monkeypatch):
    """
    Simulates GPT-SoVITS located on another drive or in parent directory,
    with single-level and nested GPT-SoVITS/GPT-SoVITS directory structure,
    as well as hint file configurations.
    Verifies that discover_gpt_sovits_roots, get_authorized_roots,
    validate_voice_profile_paths, and POST /api/voice/profiles
    consistently authorize the scanned weights and reference audio, while
    unauthorized external paths remain strictly rejected.
    """
    import glob
    from httpx import ASGITransport, AsyncClient
    from galgame2voice.database.session import init_db
    from galgame2voice.main import create_app
    from galgame2voice.routers.voice import _scan_models_sync
    from galgame2voice.utils.path_guard import (
        discover_gpt_sovits_roots,
        get_authorized_roots,
        get_effective_sovits_dir,
    )

    project_root = tmp_path / "workspace" / "project"
    project_root.mkdir(parents=True)
    monkeypatch.setenv("GALGAME2VOICE_PROJECT_ROOT", str(project_root))
    monkeypatch.setenv("DATA_DIR_NAME", "data")
    monkeypatch.delenv("GPT_SOVITS_DIR", raising=False)
    get_settings.cache_clear()
    await init_db(get_settings().db_path)

    # 1. Simulate D: drive with nested GPT-SoVITS-v2pro/GPT-SoVITS-v2pro
    d_drive_single = tmp_path / "mock_d_drive" / "GPT-SoVITS-v2pro"
    d_drive_nested = d_drive_single / "GPT-SoVITS-v2pro"
    d_drive_nested.mkdir(parents=True)
    d_ckpt = d_drive_nested / "model_d.ckpt"
    d_pth = d_drive_nested / "model_d.pth"
    d_wav = d_drive_nested / "ref_d.wav"
    d_ckpt.write_bytes(b"ckpt_d")
    d_pth.write_bytes(b"pth_d")
    d_wav.write_bytes(b"wav_d")

    # 2. Simulate project_root.parent with single-level and nested structure
    p_parent_single = project_root.parent / "GPT-SoVITS-parent-single"
    p_parent_single.mkdir(parents=True)
    ps_ckpt = p_parent_single / "model_ps.ckpt"
    ps_pth = p_parent_single / "model_ps.pth"
    ps_wav = p_parent_single / "ref_ps.wav"
    ps_ckpt.write_bytes(b"ckpt_ps")
    ps_pth.write_bytes(b"pth_ps")
    ps_wav.write_bytes(b"wav_ps")

    p_parent_nested = project_root.parent / "GPT-SoVITS-parent-nested" / "GPT-SoVITS-inner"
    p_parent_nested.mkdir(parents=True)
    pn_ckpt = p_parent_nested / "model_pn.ckpt"
    pn_pth = p_parent_nested / "model_pn.pth"
    pn_wav = p_parent_nested / "ref_pn.wav"
    pn_ckpt.write_bytes(b"ckpt_pn")
    pn_pth.write_bytes(b"pth_pn")
    pn_wav.write_bytes(b"wav_pn")

    # 3. Simulate project_root / "GPT-SoVITS"
    p_in_sovits = project_root / "GPT-SoVITS"
    p_in_sovits.mkdir(parents=True)
    in_ckpt = p_in_sovits / "model_in.ckpt"
    in_pth = p_in_sovits / "model_in.pth"
    in_ckpt.write_bytes(b"ckpt_in")
    in_pth.write_bytes(b"pth_in")

    # 4. Simulate configured directory via sovits_dir.txt
    custom_engine = tmp_path / "custom_configured_engine"
    custom_engine.mkdir(parents=True)
    (project_root / "data").mkdir(parents=True, exist_ok=True)
    (project_root / "data" / "sovits_dir.txt").write_text(str(custom_engine), encoding="utf-8")
    ce_ckpt = custom_engine / "model_ce.ckpt"
    ce_pth = custom_engine / "model_ce.pth"
    ce_ckpt.write_bytes(b"ckpt_ce")
    ce_pth.write_bytes(b"pth_ce")

    orig_glob = glob.glob

    def mock_glob(pattern, *args, **kwargs):
        if "D:\\GPT-SoVITS*\\GPT-SoVITS*" in pattern or "D:/GPT-SoVITS*/GPT-SoVITS*" in pattern:
            return [str(d_drive_nested)]
        if "D:\\GPT-SoVITS*" in pattern or "D:/GPT-SoVITS*" in pattern:
            return [str(d_drive_single)]
        return orig_glob(pattern, *args, **kwargs)

    monkeypatch.setattr(glob, "glob", mock_glob)
    monkeypatch.setattr("galgame2voice.utils.path_guard.glob.glob", mock_glob, raising=False)

    # Effective configured sovits dir check
    assert get_effective_sovits_dir(project_root) == custom_engine.resolve()

    # Discovered roots check
    discovered = discover_gpt_sovits_roots(project_root)
    assert custom_engine.resolve() in discovered
    assert p_in_sovits.resolve() in discovered
    assert p_parent_single.resolve() in discovered
    assert p_parent_nested.resolve() in discovered
    assert d_drive_single.resolve() in discovered
    assert d_drive_nested.resolve() in discovered

    # Scanned models check
    scan_res = _scan_models_sync()
    scanned_gpt_paths = {item["path"] for item in scan_res["gpt_weights"]}
    assert str(d_ckpt) in scanned_gpt_paths
    assert str(ps_ckpt) in scanned_gpt_paths
    assert str(pn_ckpt) in scanned_gpt_paths
    assert str(in_ckpt) in scanned_gpt_paths
    assert str(ce_ckpt) in scanned_gpt_paths

    # Authorized roots check
    roots = get_authorized_roots(include_sovits=True)
    for expected in (custom_engine, p_in_sovits, p_parent_single, p_parent_nested, d_drive_single, d_drive_nested):
        assert expected.resolve() in roots, f"{expected} must be in get_authorized_roots()"

    # validate_voice_profile_paths must not raise PathTraversalError on any scanned assets
    validate_voice_profile_paths(
        gpt_weights_path=str(d_ckpt),
        sovits_weights_path=str(d_pth),
        ref_audio_path=str(d_wav),
    )
    validate_voice_profile_paths(
        gpt_weights_path=str(ps_ckpt),
        sovits_weights_path=str(ps_pth),
        ref_audio_path=str(ps_wav),
    )
    validate_voice_profile_paths(
        gpt_weights_path=str(pn_ckpt),
        sovits_weights_path=str(pn_pth),
        ref_audio_path=str(pn_wav),
    )

    # API verification: POST /api/voice/profiles creates profile using scanned multi-drive paths
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as client:
        resp = await client.post("/api/voice/profiles", json={
            "name": "MultiDriveVoice",
            "gpt_weights_path": str(d_ckpt),
            "sovits_weights_path": str(d_pth),
            "ref_audio_path": str(d_wav),
            "prompt_text": "你好",
        })
        assert resp.status_code == 201, resp.text
        created = resp.json()
        assert created["name"] == "MultiDriveVoice"
        assert created["profile"]["gpt_weights_path"] == str(d_ckpt)

    # Negative check: an arbitrary outside directory remains rejected
    unauthorized = tmp_path / "private_stolen_dir"
    unauthorized.mkdir(parents=True)
    unauth_ckpt = unauthorized / "evil.ckpt"
    unauth_ckpt.write_bytes(b"evil")

    with pytest.raises(PathTraversalError):
        validate_voice_profile_paths(
            gpt_weights_path=str(unauth_ckpt),
            sovits_weights_path=str(d_pth),
        )


@pytest.mark.asyncio
async def test_sovits_dir_hint_priority_and_coexistence_with_env_var(tmp_path, monkeypatch):
    """
    Verifies that when both GPT_SOVITS_DIR and data/sovits_dir.txt point to
    different valid directories:
    1. get_effective_sovits_dir() prioritizes sovits_dir.txt over GPT_SOVITS_DIR,
       and falls back to GPT_SOVITS_DIR when sovits_dir.txt is absent or invalid.
    2. discover_gpt_sovits_roots() and get_authorized_roots() include BOTH
       directories so models in either directory can be scanned and used.
    3. When an explicit project_root differing from settings.project_root is passed,
       p_root / "data" / "sovits_dir.txt" takes precedence over settings.data_dir.
    """
    from galgame2voice.database.session import init_db
    from galgame2voice.routers.voice import _scan_models_sync
    from galgame2voice.utils.path_guard import (
        discover_gpt_sovits_roots,
        get_authorized_roots,
        get_effective_sovits_dir,
    )

    project_root = tmp_path / "workspace" / "project"
    project_root.mkdir(parents=True)
    monkeypatch.setenv("GALGAME2VOICE_PROJECT_ROOT", str(project_root))
    monkeypatch.setenv("DATA_DIR_NAME", "data")

    env_engine = tmp_path / "env_configured_engine"
    env_engine.mkdir(parents=True)
    env_ckpt = env_engine / "model_env.ckpt"
    env_pth = env_engine / "model_env.pth"
    env_wav = env_engine / "ref_env.wav"
    env_ckpt.write_bytes(b"ckpt_env")
    env_pth.write_bytes(b"pth_env")
    env_wav.write_bytes(b"wav_env")

    hint_engine = tmp_path / "web_saved_engine"
    hint_engine.mkdir(parents=True)
    hint_ckpt = hint_engine / "model_hint.ckpt"
    hint_pth = hint_engine / "model_hint.pth"
    hint_wav = hint_engine / "ref_hint.wav"
    hint_ckpt.write_bytes(b"ckpt_hint")
    hint_pth.write_bytes(b"pth_hint")
    hint_wav.write_bytes(b"wav_hint")

    monkeypatch.setenv("GPT_SOVITS_DIR", str(env_engine))
    (project_root / "data").mkdir(parents=True, exist_ok=True)
    hint_file = project_root / "data" / "sovits_dir.txt"
    hint_file.write_text(str(hint_engine), encoding="utf-8")

    get_settings.cache_clear()
    await init_db(get_settings().db_path)

    # 1. sovits_dir.txt takes precedence over GPT_SOVITS_DIR
    assert get_effective_sovits_dir(project_root) == hint_engine.resolve()

    # 2. Both sovits_dir.txt and GPT_SOVITS_DIR are included in discovered & authorized roots
    discovered = discover_gpt_sovits_roots(project_root)
    assert hint_engine.resolve() in discovered
    assert env_engine.resolve() in discovered

    roots = get_authorized_roots(include_sovits=True)
    assert hint_engine.resolve() in roots
    assert env_engine.resolve() in roots

    # Model scan finds weights from both directories
    scan_res = _scan_models_sync()
    scanned_gpt_paths = {item["path"] for item in scan_res["gpt_weights"]}
    assert str(hint_ckpt) in scanned_gpt_paths
    assert str(env_ckpt) in scanned_gpt_paths

    # Both directories pass path guard validation
    validate_voice_profile_paths(
        gpt_weights_path=str(hint_ckpt),
        sovits_weights_path=str(hint_pth),
        ref_audio_path=str(hint_wav),
    )
    validate_voice_profile_paths(
        gpt_weights_path=str(env_ckpt),
        sovits_weights_path=str(env_pth),
        ref_audio_path=str(env_wav),
    )

    # 3. Explicit project_root differing from settings.project_root prioritizes its own data/sovits_dir.txt
    other_root = tmp_path / "other_project"
    other_engine = tmp_path / "other_project_engine"
    other_engine.mkdir(parents=True)
    (other_root / "data").mkdir(parents=True, exist_ok=True)
    (other_root / "data" / "sovits_dir.txt").write_text(str(other_engine), encoding="utf-8")
    assert get_effective_sovits_dir(other_root) == other_engine.resolve()

    # 4. Fallback to GPT_SOVITS_DIR when sovits_dir.txt is removed
    hint_file.unlink()
    assert get_effective_sovits_dir(project_root) == env_engine.resolve()


# ============================================================================
# 2. Audio Converter & FFmpeg Discovery Tests
# ============================================================================

def test_find_ffmpeg_caching():
    reset_ffmpeg_cache()
    found1 = find_ffmpeg()
    found2 = find_ffmpeg()
    assert found1 == found2
    if found1:
        assert Path(found1).is_file() or Path(f"{found1}.exe").is_file()


def test_is_target_wav_pcm():
    wav_16k = _create_wav(sample_rate=16000, channels=1, sampwidth=2)
    wav_24k = _create_wav(sample_rate=24000, channels=1, sampwidth=2)
    wav_stereo = _create_wav(sample_rate=16000, channels=2, sampwidth=2)

    assert is_target_wav_pcm(wav_16k, sample_rate=16000, channels=1)
    assert not is_target_wav_pcm(wav_24k, sample_rate=16000, channels=1)
    assert not is_target_wav_pcm(wav_stereo, sample_rate=16000, channels=1)
    assert not is_target_wav_pcm(b"OggS\x00\x02dummy_ogg_data")
    assert not is_target_wav_pcm(b"")


@pytest.mark.asyncio
async def test_convert_ogg_to_wav_short_circuit():
    """Verifies that an audio byte stream already in target format bypasses ffmpeg subprocess."""
    wav_16k = _create_wav(sample_rate=16000, channels=1, sampwidth=2)
    # Even if an invalid ffmpeg_path is provided, short-circuiting must return wav_16k without error
    result = await convert_ogg_to_wav(wav_16k, sample_rate=16000, channels=1, ffmpeg_path="non_existent_ffmpeg_bin")
    assert result == wav_16k


@pytest.mark.asyncio
async def test_convert_ogg_to_wav_with_mocked_ffmpeg(monkeypatch):
    """Verifies convert_ogg_to_wav executes conversion pipeline and cleans up temp files."""
    monkeypatch.setattr("galgame2voice.utils.audio_converter.find_ffmpeg", lambda path=None: "/mock/ffmpeg")

    mock_wav = _create_wav(sample_rate=16000, channels=1, sampwidth=2)
    captured_paths = []

    async def fake_run_ffmpeg(*cmd_args, **kwargs):
        out_path = Path(cmd_args[-1])
        in_path = Path(cmd_args[cmd_args.index("-i") + 1])
        assert in_path.exists()
        out_path.write_bytes(mock_wav)
        captured_paths.extend([in_path, out_path])

    monkeypatch.setattr("galgame2voice.utils.audio_converter.run_ffmpeg_command", fake_run_ffmpeg)

    dummy_ogg = b"OggS" + b"\x00" * 32
    result = await convert_ogg_to_wav(dummy_ogg)

    assert result == mock_wav
    assert len(captured_paths) == 2
    for p in captured_paths:
        assert not p.exists()


@pytest.mark.asyncio
async def test_convert_wav_to_ogg_with_mocked_ffmpeg(monkeypatch):
    """Verifies convert_wav_to_ogg executes conversion pipeline and cleans up temp files."""
    monkeypatch.setattr("galgame2voice.utils.audio_converter.find_ffmpeg", lambda path=None: "/mock/ffmpeg")

    mock_ogg = b"OggS" + b"\x00" * 32
    captured_paths = []

    async def fake_run_ffmpeg(*cmd_args, **kwargs):
        out_path = Path(cmd_args[-1])
        in_path = Path(cmd_args[cmd_args.index("-i") + 1])
        assert in_path.exists()
        out_path.write_bytes(mock_ogg)
        captured_paths.extend([in_path, out_path])

    monkeypatch.setattr("galgame2voice.utils.audio_converter.run_ffmpeg_command", fake_run_ffmpeg)

    wav_input = _create_wav(sample_rate=16000, channels=1, sampwidth=2)
    result = await convert_wav_to_ogg(wav_input)

    assert result == mock_ogg
    assert len(captured_paths) == 2
    for p in captured_paths:
        assert not p.exists()


class TestFFmpegDiscoveryBundledFallback:
    """Verifies that find_ffmpeg discovers imageio-ffmpeg when PATH is stripped."""

    @pytest.mark.requires_piped_subprocess
    def test_find_ffmpeg_path_stripped_discovers_bundled_imageio(self, monkeypatch):
        """When PATH is empty and no env override exists, find_ffmpeg must return imageio-ffmpeg."""
        reset_ffmpeg_cache()
        monkeypatch.setenv("PATH", "")
        monkeypatch.delenv("FFMPEG_PATH", raising=False)
        monkeypatch.delenv("FFMPEG_BIN", raising=False)
        monkeypatch.setattr("galgame2voice.utils.audio_converter._find_ffmpeg_in_python_env", lambda *a: None)
        monkeypatch.setattr("galgame2voice.utils.audio_converter._find_ffmpeg_in_project_dirs", lambda *a: None)

        # Ensure imageio_ffmpeg binary path is valid on this machine
        expected_bin = imageio_ffmpeg.get_ffmpeg_exe()
        assert Path(expected_bin).is_file(), f"imageio_ffmpeg binary missing at {expected_bin}"

        discovered = find_ffmpeg()
        assert discovered is not None
        assert Path(discovered).is_file()
        assert Path(discovered).resolve() == Path(expected_bin).resolve()
        assert is_ffmpeg_available() is True

        # Second invocation must be served from cache
        cached = find_ffmpeg()
        assert cached == discovered

    @pytest.mark.requires_piped_subprocess
    def test_find_ffmpeg_custom_path_override(self, tmp_path):
        """Explicit custom_path takes precedence when valid, and returns None if invalid."""
        reset_ffmpeg_cache()
        # Non-existent file
        assert find_ffmpeg(str(tmp_path / "non_existent_binary")) is None

        # Existing file probe
        fake_bin = tmp_path / "custom_ffmpeg"
        fake_bin.touch(mode=0o755)
        assert find_ffmpeg(str(fake_bin)) == str(fake_bin.resolve())

    @pytest.mark.requires_piped_subprocess
    def test_find_ffmpeg_missing_imageio_handles_cleanly(self, monkeypatch):
        """If imageio_ffmpeg raises an error or is missing, find_ffmpeg falls back gracefully to None."""
        reset_ffmpeg_cache()
        monkeypatch.setenv("PATH", "")
        monkeypatch.delenv("FFMPEG_PATH", raising=False)
        monkeypatch.delenv("FFMPEG_BIN", raising=False)
        monkeypatch.setattr("galgame2voice.utils.audio_converter._find_ffmpeg_in_python_env", lambda *a: None)
        monkeypatch.setattr("galgame2voice.utils.audio_converter._find_ffmpeg_in_project_dirs", lambda *a: None)

        with patch("imageio_ffmpeg.get_ffmpeg_exe", side_effect=RuntimeError("Bundled binary corrupted")):
            assert find_ffmpeg() is None
            assert is_ffmpeg_available() is False

    def test_find_ffmpeg_in_python_env_skips_posix_system_prefixes(self, monkeypatch, tmp_path):
        """_find_ffmpeg_in_python_env ignores global /usr prefix on POSIX while discovering isolated venvs."""
        from galgame2voice.utils.audio_converter import _find_ffmpeg_in_python_env

        venv_dir = tmp_path / "isolated_venv"
        bin_dir = venv_dir / "bin"
        bin_dir.mkdir(parents=True)
        fake_ffmpeg = bin_dir / "ffmpeg"
        fake_ffmpeg.write_text("#!/bin/sh\n")

        monkeypatch.setattr("sys.platform", "linux")
        monkeypatch.setattr("sys.prefix", "/usr")
        monkeypatch.setattr("sys.base_prefix", "/usr")
        assert _find_ffmpeg_in_python_env("ffmpeg", "bin") is None

        monkeypatch.setattr("sys.prefix", str(venv_dir))
        assert _find_ffmpeg_in_python_env("ffmpeg", "bin") == str(fake_ffmpeg.resolve())

    @pytest.mark.requires_piped_subprocess
    def test_find_ffmpeg_prefers_bundled_tools_over_system_path(self, monkeypatch, tmp_path):
        """Bundled project/portable tools/ffmpeg must take precedence over system PATH."""
        reset_ffmpeg_cache()
        monkeypatch.delenv("FFMPEG_PATH", raising=False)
        monkeypatch.delenv("FFMPEG_BIN", raising=False)

        is_win = sys.platform == "win32"
        exe_name = "ffmpeg.exe" if is_win else "ffmpeg"

        fake_install_root = tmp_path / "portable_root"
        tools_dir = fake_install_root / "tools"
        tools_dir.mkdir(parents=True)
        bundled_bin = tools_dir / exe_name
        bundled_bin.write_bytes(b"fake bundled binary")

        system_bin = tmp_path / "system_bin" / exe_name
        system_bin.parent.mkdir(parents=True)
        system_bin.write_bytes(b"fake system binary")

        from scripts import desktop_entry

        monkeypatch.setattr("galgame2voice.runtime_paths.get_install_root", lambda: fake_install_root)
        monkeypatch.setattr(desktop_entry, "get_install_root", lambda: fake_install_root)
        from galgame2voice.config import get_settings
        monkeypatch.setattr(get_settings(), "project_root", fake_install_root)
        monkeypatch.setattr("shutil.which", lambda cmd: str(system_bin.resolve()) if "ffmpeg" in cmd else None)

        discovered = find_ffmpeg()
        assert discovered is not None
        assert Path(discovered).resolve() == bundled_bin.resolve()
        assert Path(discovered).resolve() != system_bin.resolve()

        mock_tkinter = MagicMock()
        mock_tkinter.Tcl.return_value.eval.return_value = "2"
        with patch.dict(sys.modules, {"tkinter": mock_tkinter}), \
             patch("galgame2voice.utils.audio_converter.convert_wav_to_ogg", return_value=b"OggS_fake_stream"):
            from scripts.desktop_entry import verify_portable_runtime
            verify_portable_runtime()

    @pytest.mark.requires_piped_subprocess
    def test_find_ffmpeg_prefers_bundled_tools_over_virtualenv_and_system_path(self, monkeypatch, tmp_path):
        """Bundled tools takes precedence over both virtualenv and system PATH."""
        reset_ffmpeg_cache()
        monkeypatch.delenv("FFMPEG_PATH", raising=False)
        monkeypatch.delenv("FFMPEG_BIN", raising=False)

        is_win = sys.platform == "win32"
        exe_name = "ffmpeg.exe" if is_win else "ffmpeg"

        fake_install_root = tmp_path / "portable_root"
        tools_dir = fake_install_root / "tools"
        tools_dir.mkdir(parents=True)
        bundled_bin = tools_dir / exe_name
        bundled_bin.write_bytes(b"fake bundled binary")

        venv_bin = tmp_path / "venv_bin" / exe_name
        venv_bin.parent.mkdir(parents=True)
        venv_bin.write_bytes(b"fake venv binary")

        system_bin = tmp_path / "system_bin" / exe_name
        system_bin.parent.mkdir(parents=True)
        system_bin.write_bytes(b"fake system binary")

        monkeypatch.setattr("galgame2voice.runtime_paths.get_install_root", lambda: fake_install_root)
        from galgame2voice.config import get_settings
        monkeypatch.setattr(get_settings(), "project_root", fake_install_root)
        monkeypatch.setattr("galgame2voice.utils.audio_converter._find_ffmpeg_in_python_env", lambda *a: str(venv_bin.resolve()))
        monkeypatch.setattr("shutil.which", lambda cmd: str(system_bin.resolve()) if "ffmpeg" in cmd else None)

        discovered = find_ffmpeg()
        assert discovered is not None
        assert Path(discovered).resolve() == bundled_bin.resolve()

    @pytest.mark.requires_piped_subprocess
    def test_find_ffmpeg_prefers_virtualenv_over_system_path(self, monkeypatch, tmp_path):
        """When bundled tools is missing, virtualenv takes precedence over system PATH."""
        reset_ffmpeg_cache()
        monkeypatch.delenv("FFMPEG_PATH", raising=False)
        monkeypatch.delenv("FFMPEG_BIN", raising=False)

        is_win = sys.platform == "win32"
        exe_name = "ffmpeg.exe" if is_win else "ffmpeg"

        empty_root = tmp_path / "empty_root"
        empty_root.mkdir(parents=True)

        venv_bin = tmp_path / "venv_bin" / exe_name
        venv_bin.parent.mkdir(parents=True)
        venv_bin.write_bytes(b"fake venv binary")

        system_bin = tmp_path / "system_bin" / exe_name
        system_bin.parent.mkdir(parents=True)
        system_bin.write_bytes(b"fake system binary")

        monkeypatch.setattr("galgame2voice.runtime_paths.get_install_root", lambda: empty_root)
        from galgame2voice.config import get_settings
        monkeypatch.setattr(get_settings(), "project_root", empty_root)
        monkeypatch.setattr("galgame2voice.utils.audio_converter._find_ffmpeg_in_python_env", lambda *a: str(venv_bin.resolve()))
        monkeypatch.setattr("shutil.which", lambda cmd: str(system_bin.resolve()) if "ffmpeg" in cmd else None)

        discovered = find_ffmpeg()
        assert discovered is not None
        assert Path(discovered).resolve() == venv_bin.resolve()
        assert Path(discovered).resolve() != system_bin.resolve()

    @pytest.mark.requires_piped_subprocess
    def test_find_ffmpeg_in_project_dirs_checks_both_install_root_and_project_root(self, monkeypatch, tmp_path):
        """_find_ffmpeg_in_project_dirs inspects both get_install_root and settings.project_root."""
        from galgame2voice.utils.audio_converter import _find_ffmpeg_in_project_dirs

        is_win = sys.platform == "win32"
        exe_name = "ffmpeg.exe" if is_win else "ffmpeg"

        empty_install = tmp_path / "empty_install"
        empty_install.mkdir(parents=True)

        custom_project_root = tmp_path / "project_root"
        tools_dir = custom_project_root / "tools"
        tools_dir.mkdir(parents=True)
        bin_path = tools_dir / exe_name
        bin_path.write_bytes(b"fake")

        monkeypatch.setattr("galgame2voice.runtime_paths.get_install_root", lambda: empty_install)
        from galgame2voice.config import get_settings
        monkeypatch.setattr(get_settings(), "project_root", custom_project_root)

        found = _find_ffmpeg_in_project_dirs(exe_name)
        assert found is not None
        assert Path(found).resolve() == bin_path.resolve()

    @pytest.mark.requires_piped_subprocess
    def test_find_ffmpeg_falls_back_to_system_path_when_bundled_missing(self, monkeypatch, tmp_path):
        """When bundled tools/ffmpeg does not exist, find_ffmpeg falls back to system PATH."""
        reset_ffmpeg_cache()
        monkeypatch.delenv("FFMPEG_PATH", raising=False)
        monkeypatch.delenv("FFMPEG_BIN", raising=False)

        is_win = sys.platform == "win32"
        exe_name = "ffmpeg.exe" if is_win else "ffmpeg"

        empty_install_root = tmp_path / "empty_root"
        empty_install_root.mkdir(parents=True)

        system_bin = tmp_path / "system_bin" / exe_name
        system_bin.parent.mkdir(parents=True)
        system_bin.write_bytes(b"fake system binary")

        monkeypatch.setattr("galgame2voice.runtime_paths.get_install_root", lambda: empty_install_root)
        from galgame2voice.config import get_settings
        monkeypatch.setattr(get_settings(), "project_root", empty_install_root)
        monkeypatch.setattr("galgame2voice.utils.audio_converter._find_ffmpeg_in_python_env", lambda *a: None)
        monkeypatch.setattr("shutil.which", lambda cmd: str(system_bin.resolve()) if "ffmpeg" in cmd else None)

        discovered = find_ffmpeg()
        assert discovered is not None
        assert Path(discovered).resolve() == system_bin.resolve()

    @pytest.mark.requires_piped_subprocess
    @pytest.mark.asyncio
    async def test_audio_converter_roundtrip_with_bundled_imageio(self, monkeypatch):
        """Verifies convert_wav_to_ogg and convert_ogg_to_wav roundtrip without global ffmpeg on PATH."""
        reset_ffmpeg_cache()
        monkeypatch.setenv("PATH", "")
        monkeypatch.delenv("FFMPEG_PATH", raising=False)
        monkeypatch.delenv("FFMPEG_BIN", raising=False)

        sample_wav = _create_wav(sample_rate=16000, channels=1, duration_s=0.2)
        assert is_target_wav_pcm(sample_wav, sample_rate=16000, channels=1)

        # Transcode PCM WAV -> OGG Opus
        converted_ogg = await convert_wav_to_ogg(sample_wav)
        assert len(converted_ogg) > 0
        assert converted_ogg.startswith(b"OggS")

        # Transcode OGG Opus -> PCM WAV (16kHz mono 16-bit)
        roundtrip_wav = await convert_ogg_to_wav(converted_ogg, sample_rate=16000, channels=1)
        assert roundtrip_wav.startswith(b"RIFF")
        assert is_target_wav_pcm(roundtrip_wav, sample_rate=16000, channels=1)

    @pytest.mark.requires_piped_subprocess
    @pytest.mark.asyncio
    async def test_convert_ogg_to_wav_adversarial_corrupted_input(self, monkeypatch):
        """Adversarial corrupted payload must raise ValueError and clean up processes."""
        reset_ffmpeg_cache()
        monkeypatch.setenv("PATH", "")
        monkeypatch.delenv("FFMPEG_PATH", raising=False)
        monkeypatch.delenv("FFMPEG_BIN", raising=False)

        with pytest.raises(ValueError, match="Corrupted or unsupported|too short"):
            await convert_ogg_to_wav(b"\x00\x01\x02\x03CORRUPTED_NON_AUDIO_BYTES_PAYLOAD")

    @pytest.mark.requires_piped_subprocess
    def test_find_ffmpeg_unset_path_still_discovers_binary(self, monkeypatch):
        """Deleting PATH must not hide a discovered ffmpeg binary."""
        monkeypatch.delenv("FFMPEG_PATH", raising=False)
        monkeypatch.delenv("FFMPEG_BIN", raising=False)
        monkeypatch.delenv("PATH", raising=False)
        reset_ffmpeg_cache()

        discovered = find_ffmpeg()
        assert discovered is not None, "Discovery failed under PATH=None"
        assert Path(discovered).is_file()
        assert is_ffmpeg_available() is True

    @pytest.mark.requires_piped_subprocess
    def test_find_ffmpeg_garbage_path_still_discovers_binary(self, monkeypatch):
        """A PATH of nonexistent directories must not hide a discovered ffmpeg binary."""
        monkeypatch.delenv("FFMPEG_PATH", raising=False)
        monkeypatch.delenv("FFMPEG_BIN", raising=False)
        monkeypatch.setenv("PATH", "/nonexistent/dir:/dev/null/notdir")
        reset_ffmpeg_cache()

        discovered = find_ffmpeg()
        assert discovered is not None, "Discovery failed under PATH=/nonexistent/dir:/dev/null/notdir"
        assert Path(discovered).is_file()
        assert is_ffmpeg_available() is True

    @pytest.mark.requires_piped_subprocess
    def test_find_ffmpeg_directory_and_nonexecutable_custom_paths(self, tmp_path):
        """A directory is rejected. A non-executable file resolves to itself."""
        reset_ffmpeg_cache()

        sub_dir = tmp_path / "not_a_binary_dir"
        sub_dir.mkdir()
        assert find_ffmpeg(str(sub_dir)) is None

        script = tmp_path / "fake_ffmpeg.sh"
        script.write_text("#!/bin/sh\necho not ffmpeg")
        script.chmod(0o644)
        # shutil.which returns None for non-executable, then is_file() resolves the path.
        assert find_ffmpeg(str(script)) == str(script.resolve())

    @pytest.mark.requires_piped_subprocess
    def test_find_ffmpeg_imageio_generic_exception_and_missing_path(self, monkeypatch):
        """A generic imageio exception, or a path that is not a file, yields None."""
        monkeypatch.setenv("PATH", "")
        monkeypatch.delenv("FFMPEG_PATH", raising=False)
        monkeypatch.delenv("FFMPEG_BIN", raising=False)
        monkeypatch.setattr(
            "galgame2voice.utils.audio_converter._find_ffmpeg_in_python_env", lambda *a: None
        )
        monkeypatch.setattr(
            "galgame2voice.utils.audio_converter._find_ffmpeg_in_project_dirs", lambda *a: None
        )

        reset_ffmpeg_cache()
        with patch("imageio_ffmpeg.get_ffmpeg_exe", side_effect=Exception("Simulated DLL missing")):
            assert find_ffmpeg() is None
            assert is_ffmpeg_available() is False

        reset_ffmpeg_cache()
        with patch("imageio_ffmpeg.get_ffmpeg_exe", return_value="/nonexistent/ffmpeg/bin"):
            assert find_ffmpeg() is None
            assert is_ffmpeg_available() is False

    @pytest.mark.requires_piped_subprocess
    async def test_find_ffmpeg_concurrent_probes_with_cache_resets(self):
        """Fifty overlapping probes, resetting the cache every tenth call, all succeed."""
        results = []
        errors = []

        async def worker(idx: int):
            try:
                if idx % 10 == 0:
                    reset_ffmpeg_cache()
                res = await asyncio.to_thread(find_ffmpeg)
                results.append(res)
            except Exception as exc:
                errors.append(exc)

        await asyncio.gather(*(worker(i) for i in range(50)))
        assert len(errors) == 0, f"Encountered errors during concurrent probes: {errors}"
        assert len(results) == 50
        assert all(r is not None for r in results)


# ============================================================================
# 3. TTS Cache In-Memory Tracking Tests
# ============================================================================

@pytest.mark.asyncio
async def test_tts_cache_manager_in_memory_tracking(tmp_path):
    cache_dir = tmp_path / "cache"
    db_path = tmp_path / "test.db"
    
    # Initialize DB
    from galgame2voice.database.session import init_db
    await init_db(db_path)

    mgr = TtsCacheManager(cache_dir=cache_dir, db_path=db_path, max_cache_mb=10, max_entries=50)

    # Initial stats uninitialized
    assert mgr._disk_bytes_total is None
    assert mgr._stats_initialized is False

    # Initialize stats via get_stats
    await mgr.get_stats()
    assert mgr._disk_bytes_total == 0
    assert mgr._disk_files_total == 0
    assert mgr._stats_initialized is True

    # Store first item
    data1 = b"RIFF" + b"\x00" * 40
    url1, path1, size1 = await mgr.put(
        cache_key="key1",
        text="text1",
        clean_text="text1",
        voice_profile_id=1,
        params_hash="hash1",
        audio_bytes=data1,
    )
    assert mgr._disk_bytes_total == len(data1)
    assert mgr._disk_files_total == 1

    # Store second item
    data2 = b"RIFF" + b"\x00" * 100
    url2, path2, size2 = await mgr.put(
        cache_key="key2",
        text="text2",
        clean_text="text2",
        voice_profile_id=1,
        params_hash="hash2",
        audio_bytes=data2,
    )
    assert mgr._disk_bytes_total == len(data1) + len(data2)
    assert mgr._disk_files_total == 2

    # Clear cache
    await mgr.clear()
    assert mgr._disk_bytes_total == 0
    assert mgr._disk_files_total == 0
    assert mgr._stats_initialized is True


# ============================================================================
# 4. Database Telemetry Path Normalization & Path Traversal Security Tests
# ============================================================================


class TestDatabaseTelemetryPathNormalization:
    """Adversarial testing of DatabaseTelemetry path normalization across OS platforms."""

    def test_database_telemetry_schema(self):
        """Verify DatabaseTelemetry pydantic model accepts relative path and wal_mode."""
        t = DatabaseTelemetry(status="connected", wal_mode=True, path="data/galgame2voice.db")
        assert t.path == "data/galgame2voice.db"
        assert t.wal_mode is True

    @pytest.mark.parametrize(
        "simulated_project_root,simulated_db_path,expected_relative_path",
        [
            # Standard Windows relative path
            (
                Path("C:/Users/JIYIF/Documents/New project/galgame2voice"),
                Path("C:/Users/JIYIF/Documents/New project/galgame2voice/data/galgame2voice.db"),
                "data/galgame2voice.db",
            ),
            # Custom subdirectory in project
            (
                Path("C:/Users/JIYIF/Documents/New project/galgame2voice"),
                Path("C:/Users/JIYIF/Documents/New project/galgame2voice/custom_data/app.db"),
                "custom_data/app.db",
            ),
            # Linux / POSIX project root
            (
                Path("/home/user/workspace/galgame2voice"),
                Path("/home/user/workspace/galgame2voice/data/galgame2voice.db"),
                "data/galgame2voice.db",
            ),
            # Windows different drive letter (outside project root) -> fallback
            (
                Path("C:/Users/JIYIF/galgame2voice"),
                Path("D:/external_data/test_db.sqlite3"),
                "data/test_db.sqlite3",
            ),
            # POSIX /tmp/ directory (outside project root) -> fallback
            (
                Path("/app/galgame2voice"),
                Path("/tmp/pytest-of-root/temp.db"),
                "data/temp.db",
            ),
            # UNC network share (outside project root) -> fallback
            pytest.param(
                Path("C:/galgame2voice"),
                Path(r"\\remote-server\share\data.db"),
                "data/data.db",
                marks=pytest.mark.skipif(
                    sys.platform != "win32",
                    reason="UNC path parsing is Windows-specific; POSIX treats backslashes as literal characters",
                ),
            ),
        ],
    )
    def test_path_normalization_logic(
        self, simulated_project_root, simulated_db_path, expected_relative_path
    ):
        """
        Verify path normalization logic:
        1. If db_path is within project_root, returns relative_to(project_root).as_posix()
        2. If outside project_root, safely falls back to f"{data_dir_name}/{db_path.name}"
        3. Never leaks full host machine drive letters or user home directory.
        """
        from galgame2voice.routers.health import _resolve_rel_db_path
        rel_db_path = _resolve_rel_db_path(simulated_db_path, simulated_project_root, "data")

        assert rel_db_path == expected_relative_path
        assert not rel_db_path.startswith("C:")
        assert not rel_db_path.startswith("D:")
        assert not rel_db_path.startswith("/home/")
        assert not rel_db_path.startswith("/Users/")
        assert "\\" not in rel_db_path  # Must be normalized POSIX forward slashes

    @pytest.mark.parametrize("configured_folder", ["absolute", "../private"])
    def test_external_data_directory_never_leaks_its_location(self, tmp_path, configured_folder):
        from galgame2voice.routers.health import _resolve_rel_db_path
        external = tmp_path / "private" / "history.db"
        folder = str(external.parent) if configured_folder == "absolute" else configured_folder
        assert _resolve_rel_db_path(external, tmp_path / "app", folder) == "data/history.db"

    async def test_live_system_status_endpoint_returns_normalized_path(self):
        """
        Empirically invoke GET /api/system/status with an ASGI client and verify
        that response['database']['path'] is strictly relative.
        """
        app = FastAPI()
        app.include_router(health_router)

        # Bypass auth for status test
        with patch("galgame2voice.routers.health.require_auth", return_value="admin"):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                response = await client.get("/api/system/status", headers={"X-Auth-Token": "test_token"})

            assert response.status_code == 200, f"Health endpoint returned status {response.status_code}: {response.text}"
            data = response.json()
            assert "database" in data
            db_info = data["database"]
            assert "path" in db_info
            path_val = db_info["path"]

            # Assert path is relative POSIX
            settings = get_settings()
            # The isolation fixture deliberately keeps the DB outside the repo.
            # The endpoint must conceal that absolute temp path using its fallback.
            if settings.db_path.is_relative_to(settings.project_root):
                expected = settings.db_path.relative_to(settings.project_root).as_posix()
            else:
                expected = f"data/{settings.db_path.name}"
            assert path_val == expected
            assert "C:" not in path_val
            assert "Users" not in path_val
            assert "\\" not in path_val


@pytest.fixture
def clean_db():
    """Provides a fresh isolated temporary database."""
    fd, db_path = tempfile.mkstemp(suffix=".db", prefix="m1_adv_chal_")
    os.close(fd)
    asyncio.run(init_db(db_path))
    yield db_path
    if os.path.exists(db_path):
        try:
            os.remove(db_path)
        except OSError:
            pass


@pytest.fixture
def app_client(clean_db, monkeypatch):
    """Provides a TestClient with the isolated test database."""
    monkeypatch.setenv("DATABASE_PATH", clean_db)
    app = create_app()
    with TestClient(app, base_url="http://127.0.0.1:8080") as client:
        yield client, clean_db


class TestPathTraversalAndFileSystemSecurity:
    """Adversarially tests path traversal defense across static files and fs browser."""

    def test_static_audio_path_traversal_attempts(self, app_client):
        """
        Sends diverse path traversal attacks to the /audio static mount.
        Must return 404/400 (Not Found / Bad Request), NEVER leak outside files.
        """
        client, db_path = app_client

        traversal_payloads = [
            "/audio/../main.py",
            "/audio/../../galgame2voice/main.py",
            "/audio/..%2f..%2fgalgame2voice%2fmain.py",
            "/audio/..%5c..%5cgalgame2voice%5cmain.py",
            "/audio/%2e%2e%2f%2e%2e%2fmain.py",
            "/audio/....//....//galgame2voice/main.py",
            "/audio/C:/Windows/win.ini",
            "/audio/c:\\windows\\system32\\calc.exe",
            "/audio/cache/../../galgame2voice/routers/config.py",
            "/audio/cache/%2e%2e%2f%2e%2e%2fPROJECT.md",
            "/audio/NUL",
            "/audio/CON",
            "/audio/COM1",
        ]

        for payload in traversal_payloads:
            resp = client.get(payload)
            # Response must be 404 or 400, never 200 with source code
            assert resp.status_code in (400, 404, 405), f"Path traversal succeeded or unhandled on {payload}: {resp.status_code}"
            assert "def create_app" not in resp.text
            assert "M1_SECURITY" not in resp.text

    def test_legitimate_audio_file_serving_works(self, app_client):
        """Verifies that legitimate files inside audio_dir are accessible."""
        client, db_path = app_client
        settings = get_settings()

        test_wav = settings.audio_dir / "valid_test_audio.wav"
        test_wav.write_bytes(b"RIFF\x24\x00\x00\x00WAVEfmt \x10\x00\x00\x00")
        try:
            resp = client.get("/audio/valid_test_audio.wav")
            assert resp.status_code == 200
            assert resp.content.startswith(b"RIFF")
        finally:
            if test_wav.exists():
                test_wav.unlink()

    def test_browse_file_api_handles_arbitrary_inputs(self, app_client):
        """Tests /api/voice/browse-file with invalid file types and non-existent initial_dir."""
        pytest.importorskip("tkinter")
        client, db_path = app_client

        payload = {
            "file_type": "invalid_type_123",
            "initial_dir": "C:\\InvalidNonExistentPath9876",
        }
        with patch("tkinter.filedialog.askopenfilename", side_effect=Exception("Headless mode")):
            resp = client.post("/api/voice/browse-file", json=payload)
            assert resp.status_code == 200
            assert resp.json() == {"selected_path": ""}
