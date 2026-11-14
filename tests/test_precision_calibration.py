"""
Evidence-based precision calibration tests:
1. precision store read/write and resolve_initial_is_half priority (env > cache > default).
2. calibrate_engine_precision decision table (audible / silent / inconclusive), with a
   fake probe + fake restart — no real engine or GPU needed.
"""

import asyncio
from contextlib import closing, contextmanager
import json
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest

from galgame2voice.config import get_settings
from galgame2voice.routers import health
from galgame2voice.services.engine_runtime import EngineLaunch
from galgame2voice.services.gpt_sovits_client import GptSovitsClient
from galgame2voice.services.sovits_endpoint import parse_sovits_endpoint
from galgame2voice.utils.precision import (
    read_precision_cache,
    resolve_initial_is_half,
    write_precision_cache,
)
from scripts import run_server


class TestPrecisionStore:
    def test_write_and_read_roundtrip(self, tmp_path):
        write_precision_cache(tmp_path, str(tmp_path / "engine"), False)
        data = read_precision_cache(tmp_path)
        assert data is not None
        assert data["is_half"] is False
        assert data["sovits_dir"] == str(tmp_path / "engine")

    def test_missing_cache_returns_none(self, tmp_path):
        assert read_precision_cache(tmp_path) is None

    def test_corrupt_cache_returns_none(self, tmp_path):
        (tmp_path / "data").mkdir()
        (tmp_path / "data" / "precision.json").write_text("{not json", encoding="utf-8")
        assert read_precision_cache(tmp_path) is None

    def test_utf8_bom_tolerated(self, tmp_path):
        (tmp_path / "data").mkdir()
        payload = json.dumps({"is_half": True, "sovits_dir": "x", "verified_at": 0})
        (tmp_path / "data" / "precision.json").write_bytes(b"\xef\xbb\xbf" + payload.encode("utf-8"))
        assert read_precision_cache(tmp_path)["is_half"] is True


class TestResolveInitialIsHalf:
    def test_env_override_fp32_wins(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GPT_SOVITS_PRECISION", "fp32")
        is_half, source = resolve_initial_is_half(tmp_path, tmp_path / "engine")
        assert is_half is False and source == "env"

    def test_env_override_fp16_wins(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GPT_SOVITS_PRECISION", "FP16")
        is_half, source = resolve_initial_is_half(tmp_path, tmp_path / "engine")
        assert is_half is True and source == "env"

    def test_cache_used_when_engine_dir_matches(self, tmp_path, monkeypatch):
        monkeypatch.delenv("GPT_SOVITS_PRECISION", raising=False)
        engine = tmp_path / "engine"
        write_precision_cache(tmp_path, str(engine), False)
        is_half, source = resolve_initial_is_half(tmp_path, engine)
        assert is_half is False and source == "cache"

    def test_cache_ignored_when_engine_dir_changed(self, tmp_path, monkeypatch):
        monkeypatch.delenv("GPT_SOVITS_PRECISION", raising=False)
        monkeypatch.setattr(
            "galgame2voice.utils.hardware.detect_gpu_capability",
            lambda: (True, "Mock GPU", 0),
        )
        write_precision_cache(tmp_path, str(tmp_path / "old_engine"), False)
        is_half, source = resolve_initial_is_half(tmp_path, tmp_path / "new_engine")
        assert is_half is True and source == "default"

    def test_default_is_fp16(self, tmp_path, monkeypatch):
        monkeypatch.delenv("GPT_SOVITS_PRECISION", raising=False)
        monkeypatch.setattr(
            "galgame2voice.utils.hardware.detect_gpu_capability",
            lambda: (True, "Mock GPU", 0),
        )
        is_half, source = resolve_initial_is_half(tmp_path, tmp_path / "engine")
        assert is_half is True and source == "default"


class TestCalibrateEnginePrecision:
    """Exercises scripts.run_server.calibrate_engine_precision with fake probe/restart."""

    @pytest.fixture
    def calibrate(self):
        import scripts.run_server as rs
        return rs.calibrate_engine_precision

    def test_fp16_audible_caches_fp16(self, calibrate, tmp_path, monkeypatch):
        monkeypatch.setattr("scripts.run_server.PROJECT_ROOT", tmp_path)
        probes = iter([0.5])
        is_half, calibrated = calibrate(
            tmp_path / "engine", True, lambda: next(probes), lambda fh: (_ for _ in ()).throw(AssertionError("no restart expected")),
        )
        assert is_half is True and calibrated is True
        assert read_precision_cache(tmp_path)["is_half"] is True

    def test_fp16_silent_restarts_fp32_and_caches(self, calibrate, tmp_path, monkeypatch):
        monkeypatch.setattr("scripts.run_server.PROJECT_ROOT", tmp_path)
        # First probe (FP16): silent. Second probe (after FP32 restart): audible.
        probes = iter([0.0, 0.42])
        restarts = []

        def fake_restart(is_half):
            restarts.append(is_half)
            return object()

        is_half, calibrated = calibrate(tmp_path / "engine", True, lambda: next(probes), fake_restart)
        assert restarts == [False]
        assert is_half is False and calibrated is True
        assert read_precision_cache(tmp_path)["is_half"] is False

    def test_fp32_still_silent_no_cache(self, calibrate, tmp_path, monkeypatch):
        monkeypatch.setattr("scripts.run_server.PROJECT_ROOT", tmp_path)
        probes = iter([0.0, 0.0])
        is_half, calibrated = calibrate(tmp_path / "engine", True, lambda: next(probes), lambda fh: object())
        assert is_half is False and calibrated is False
        assert read_precision_cache(tmp_path) is None

    def test_inconclusive_probe_no_cache(self, calibrate, tmp_path, monkeypatch):
        monkeypatch.setattr("scripts.run_server.PROJECT_ROOT", tmp_path)
        is_half, calibrated = calibrate(tmp_path / "engine", True, lambda: None, lambda fh: object())
        assert is_half is True and calibrated is False
        assert read_precision_cache(tmp_path) is None

    def test_fp32_silent_from_start_no_restart(self, calibrate, tmp_path, monkeypatch):
        monkeypatch.setattr("scripts.run_server.PROJECT_ROOT", tmp_path)
        probes = iter([0.0])
        restarts = []

        is_half, calibrated = calibrate(tmp_path / "engine", False, lambda: next(probes), lambda fh: restarts.append(fh))
        assert restarts == []  # already FP32 — restarting changes nothing
        assert is_half is False and calibrated is False


class TestResolveWeightFilePath:
    """Weights can live inside the character package (project-relative) or the
    engine dir (engine-relative); the switch flow absolutizes either form."""

    def test_package_relative_weight_absolutized(self, tmp_path, monkeypatch):
        import galgame2voice.utils.path_guard as pg
        pkg = tmp_path / "characters" / "kanna" / "gpt.ckpt"
        pkg.parent.mkdir(parents=True)
        pkg.write_bytes(b"w")
        monkeypatch.setattr(pg, "get_settings", lambda: SimpleNamespace(project_root=tmp_path, audio_dir=tmp_path / "audio"))
        monkeypatch.setattr(pg, "get_authorized_roots", lambda include_sovits=True: [])
        assert pg.resolve_weight_file_path("characters/kanna/gpt.ckpt") == str(pkg)

    def test_engine_relative_weight_absolutized(self, tmp_path, monkeypatch):
        import galgame2voice.utils.path_guard as pg
        engine = tmp_path / "engine"
        w = engine / "GPT_weights_v2ProPlus" / "kanna-e50.ckpt"
        w.parent.mkdir(parents=True)
        w.write_bytes(b"w")
        monkeypatch.setattr(pg, "get_settings", lambda: SimpleNamespace(project_root=tmp_path, audio_dir=tmp_path / "audio"))
        monkeypatch.setattr(pg, "get_authorized_roots", lambda include_sovits=True: [engine])
        assert pg.resolve_weight_file_path("GPT_weights_v2ProPlus/kanna-e50.ckpt") == str(w)

    def test_absolute_untouched_and_missing_passthrough(self, tmp_path, monkeypatch):
        import galgame2voice.utils.path_guard as pg
        monkeypatch.setattr(pg, "get_settings", lambda: SimpleNamespace(project_root=tmp_path, audio_dir=tmp_path / "audio"))
        monkeypatch.setattr(pg, "get_authorized_roots", lambda include_sovits=True: [])
        assert pg.resolve_weight_file_path("GPT_weights_v2ProPlus/missing.ckpt") == "GPT_weights_v2ProPlus/missing.ckpt"
        assert pg.resolve_weight_file_path("") == ""


class TestYamlPrecisionSync:
    def test_yaml_read_write_roundtrip(self, tmp_path):
        from galgame2voice.utils.precision import (
            read_sovits_yaml_is_half,
            write_sovits_yaml_is_half,
            find_sovits_yaml_path,
        )
        configs_dir = tmp_path / "GPT_SoVITS" / "configs"
        configs_dir.mkdir(parents=True)
        yaml_file = configs_dir / "tts_infer.yaml"
        yaml_file.write_text(
            "custom:\n  device: cuda\n  is_half: true\n  version: v2\nv1:\n  is_half: false\n",
            encoding="utf-8",
        )

        assert find_sovits_yaml_path(tmp_path) == yaml_file
        assert read_sovits_yaml_is_half(tmp_path) is True

        # Switch to FP32
        out_path = write_sovits_yaml_is_half(tmp_path, False)
        assert out_path == yaml_file
        assert read_sovits_yaml_is_half(tmp_path) is False
        content = yaml_file.read_text(encoding="utf-8")
        assert "is_half: false" in content
        assert "is_half: true" not in content.split("v1:")[0]

        # Switch back to FP16
        write_sovits_yaml_is_half(tmp_path, True)
        assert read_sovits_yaml_is_half(tmp_path) is True

    def test_resolve_initial_is_half_picks_up_yaml_fp32(self, tmp_path, monkeypatch):
        monkeypatch.delenv("GPT_SOVITS_PRECISION", raising=False)
        configs_dir = tmp_path / "GPT_SoVITS" / "configs"
        configs_dir.mkdir(parents=True)
        yaml_file = configs_dir / "tts_infer.yaml"
        yaml_file.write_text(
            "custom:\n  device: cuda\n  is_half: false\n  version: v2\n",
            encoding="utf-8",
        )

        is_half, source = resolve_initial_is_half(tmp_path, tmp_path)
        assert is_half is False
        assert source == "yaml"

    def test_resolve_initial_is_half_picks_up_db_setting(self, tmp_path, monkeypatch):
        import sqlite3
        from galgame2voice.utils.precision import read_db_precision, resolve_initial_is_half
        monkeypatch.delenv("GPT_SOVITS_PRECISION", raising=False)

        data_dir = tmp_path / "data"
        data_dir.mkdir(parents=True)
        db_file = data_dir / "galgame2voice.db"
        with sqlite3.connect(str(db_file)) as conn:
            conn.execute("CREATE TABLE settings (id INTEGER PRIMARY KEY, inference_precision TEXT);")
            conn.execute("INSERT INTO settings (id, inference_precision) VALUES (1, 'fp32');")

        assert read_db_precision(tmp_path) == "fp32"
        is_half, source = resolve_initial_is_half(tmp_path, tmp_path)
        assert is_half is False
        assert source == "db"

        # Update DB to fp16
        with sqlite3.connect(str(db_file)) as conn:
            conn.execute("UPDATE settings SET inference_precision = 'fp16' WHERE id = 1;")
        assert read_db_precision(tmp_path) == "fp16"
        is_half2, source2 = resolve_initial_is_half(tmp_path, tmp_path)
        assert is_half2 is True
        assert source2 == "db"

    def test_cpu_mode_yaml_and_resolution(self, tmp_path, monkeypatch):
        import sqlite3
        from galgame2voice.utils.precision import (
            read_sovits_yaml_device,
            write_sovits_yaml_config,
            resolve_initial_device_and_half,
            write_precision_cache,
        )
        monkeypatch.delenv("GPT_SOVITS_PRECISION", raising=False)
        monkeypatch.delenv("GPT_SOVITS_DEVICE", raising=False)

        configs_dir = tmp_path / "GPT_SoVITS" / "configs"
        configs_dir.mkdir(parents=True)
        yaml_file = configs_dir / "tts_infer.yaml"
        yaml_file.write_text(
            "custom:\n  device: cuda\n  is_half: true\n",
            encoding="utf-8",
        )

        assert read_sovits_yaml_device(tmp_path) == "cuda"

        # 1. Switch to CPU via write_sovits_yaml_config
        write_sovits_yaml_config(tmp_path, is_half=False, device="cpu")
        assert read_sovits_yaml_device(tmp_path) == "cpu"
        content = yaml_file.read_text(encoding="utf-8")
        assert "device: cpu" in content
        assert "is_half: false" in content

        # 2. YAML source detection
        dev, half, src = resolve_initial_device_and_half(tmp_path, tmp_path)
        assert dev == "cpu"
        assert half is False
        assert src == "yaml"

        # 3. DB source detection for CPU
        data_dir = tmp_path / "data"
        data_dir.mkdir(parents=True)
        db_file = data_dir / "galgame2voice.db"
        with sqlite3.connect(str(db_file)) as conn:
            conn.execute("CREATE TABLE settings (id INTEGER PRIMARY KEY, inference_precision TEXT);")
            conn.execute("INSERT INTO settings (id, inference_precision) VALUES (1, 'cpu');")

        dev2, half2, src2 = resolve_initial_device_and_half(tmp_path, tmp_path)
        assert dev2 == "cpu"
        assert half2 is False
        assert src2 == "db"

        # 4. Cache source detection for CPU
        write_precision_cache(tmp_path, str(tmp_path), is_half=False, device="cpu")
        with sqlite3.connect(str(db_file)) as conn:
            conn.execute("UPDATE settings SET inference_precision = 'auto' WHERE id = 1;")

        dev3, half3, src3 = resolve_initial_device_and_half(tmp_path, tmp_path)
        assert dev3 == "cpu"
        assert half3 is False
        assert src3 == "cache"


class TestSilenceCalibrationRestart:
    HOST = "127.0.0.1"
    PORT = 19231
    GPT = r"C:\voice\gpt.ckpt"
    SOVITS = r"C:\voice\sovits.pth"
    REFER = r"C:\voice\ref.wav"

    @classmethod
    def _install_owned_engine(cls, tmp_path, monkeypatch):
        import threading

        monkeypatch.setattr(health, "_engine_restart_lock", threading.Lock())
        monkeypatch.setattr(health, "_engine_restart_target", None)
        monkeypatch.setattr(get_settings(), "project_root", tmp_path)
        monkeypatch.setattr(run_server, "PROJECT_ROOT", tmp_path)
        (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
        (tmp_path / "logs" / "gpt_sovits.log").write_text(
            "=== Galgame2Voice engine start ===\nengine ready\n", encoding="utf-8"
        )
        engine = tmp_path / "engine"
        engine.mkdir(parents=True, exist_ok=True)
        (engine / "api_v2.py").write_text("# synthetic entrypoint, never executed\n", encoding="utf-8")
        proc = MagicMock(pid=4242)
        proc.poll.return_value = None
        monkeypatch.setattr(run_server, "_SPAWNED_SOVITS_PROC", proc)
        monkeypatch.setattr(
            run_server,
            "_SPAWNED_SOVITS_LAUNCH",
            EngineLaunch(proc, cls.HOST, cls.PORT, "cuda", True, started_at=1.0),
        )
        monkeypatch.setattr(run_server, "_SPAWNED_SOVITS_STARTED_AT", 1.0)
        monkeypatch.setattr(run_server.time, "sleep", lambda *_args, **_kwargs: None)
        return proc, engine

    @classmethod
    def _remember_replacement(cls, is_half):
        replacement = MagicMock(pid=4243)
        replacement.poll.return_value = None

        def spawn(*_args, **_kwargs):
            run_server._SPAWNED_SOVITS_PROC = replacement
            run_server._SPAWNED_SOVITS_LAUNCH = EngineLaunch(
                replacement, cls.HOST, cls.PORT, "cuda", is_half, started_at=2.0
            )
            return replacement

        return spawn

    @classmethod
    def _loaded_client(cls):
        client = GptSovitsClient(base_url=f"http://{cls.HOST}:{cls.PORT}")
        client.current_gpt_weights = cls.GPT
        client.current_sovits_weights = cls.SOVITS
        client.current_refer_audio = cls.REFER
        client.current_refer_text = "テスト"
        client.current_refer_language = "ja"
        client._model_state_uncertain = False
        client._loaded_gpt_source = None
        client._loaded_sovits_source = None
        return client

    def test_silence_calibration_gap_stays_starting(self, tmp_path, monkeypatch):
        proc, engine = self._install_owned_engine(tmp_path, monkeypatch)
        endpoint = parse_sovits_endpoint(f"http://{self.HOST}:{self.PORT}", source="db")
        seen = {}

        async def resolve():
            return endpoint

        async def probe(*_args, **_kwargs):
            return SimpleNamespace(status="unreachable")

        def terminate(pid, timeout=2.0):
            assert pid == proc.pid
            proc.poll.return_value = 1

        def port_in_use(*_args, **_kwargs):
            if run_server._SPAWNED_SOVITS_PROC is proc:
                seen["status"] = asyncio.run(health.engine_status_endpoint())
                return False
            return True

        monkeypatch.setattr(health, "resolve_effective_sovits_endpoint", resolve)
        monkeypatch.setattr(health, "_probe_gpt_sovits", probe)
        monkeypatch.setattr(run_server, "terminate_process_tree", terminate)
        monkeypatch.setattr(run_server, "is_port_in_use", port_in_use)
        monkeypatch.setattr(run_server, "_probe_synth_peak", lambda *_a, **_k: 0.0)
        monkeypatch.setattr(run_server, "_spawn_sovits_process", self._remember_replacement(False))
        run_server._calibrate_precision_after_ready(
            engine, self.HOST, self.PORT, True, "default", device="cuda"
        )
        assert seen.get("status") is not None
        assert seen["status"]["state"] == "starting", seen["status"]
        assert seen["status"]["state"] != "failed", seen["status"]

    def test_silence_calibration_reloads_weights_on_the_replacement(self, tmp_path, monkeypatch):
        proc, engine = self._install_owned_engine(tmp_path, monkeypatch)
        client = self._loaded_client()
        monkeypatch.setattr(
            "galgame2voice.services.gpt_sovits_client._global_gpt_sovits_client", client
        )
        monkeypatch.setattr(
            "galgame2voice.services.gpt_sovits_client.get_gpt_sovits_client", lambda: client
        )
        peaks = iter((0.0, 0.5))

        def terminate(pid, timeout=2.0):
            assert pid == proc.pid
            proc.poll.return_value = 1

        monkeypatch.setattr(run_server, "terminate_process_tree", terminate)
        monkeypatch.setattr(
            run_server,
            "is_port_in_use",
            lambda *_a, **_k: run_server._SPAWNED_SOVITS_PROC is not proc,
        )
        monkeypatch.setattr(run_server, "_probe_synth_peak", lambda *_a, **_k: next(peaks))
        monkeypatch.setattr(run_server, "_spawn_sovits_process", self._remember_replacement(False))
        run_server._calibrate_precision_after_ready(
            engine, self.HOST, self.PORT, True, "default", device="cuda"
        )
        calls = []

        class _Server:
            async def handle_request(self, method, path, json_data=None, params=None):
                calls.append(path)
                return httpx.Response(200, text='{"ok": true}')

        client.server = _Server()
        switched = asyncio.run(
            client.switch_voice_profile({
                "name": "heroine",
                "gpt_weights_path": self.GPT,
                "sovits_weights_path": self.SOVITS,
                "refer_audio_path": self.REFER,
                "refer_text": "テスト",
                "refer_language": "ja",
            })
        )
        assert "/set_gpt_weights" in calls, {
            "switched": switched,
            "requests": calls,
            "current_gpt_weights": client.current_gpt_weights,
            "uncertain": client._model_state_uncertain,
        }
        assert "/set_sovits_weights" in calls, {
            "switched": switched,
            "requests": calls,
            "current_sovits_weights": client.current_sovits_weights,
            "uncertain": client._model_state_uncertain,
        }

    def test_silence_calibration_does_not_spawn_while_the_port_is_busy(self, tmp_path, monkeypatch):
        proc, engine = self._install_owned_engine(tmp_path, monkeypatch)
        pid_file = tmp_path / "gptsovits.pid"
        pid_file.write_text(str(proc.pid), encoding="utf-8")
        endpoint = parse_sovits_endpoint(f"http://{self.HOST}:{self.PORT}", source="db")

        async def resolve():
            return endpoint

        async def probe(*_args, **_kwargs):
            return SimpleNamespace(status="unreachable")

        monkeypatch.setattr(health, "resolve_effective_sovits_endpoint", resolve)
        monkeypatch.setattr(health, "_probe_gpt_sovits", probe)

        spawned = []

        def terminate(pid, timeout=2.0):
            assert pid == proc.pid
            proc.poll.return_value = 1

        def spawn(*args, **kwargs):
            spawned.append((args, kwargs))
            return self._remember_replacement(False)()

        monkeypatch.setattr(run_server, "terminate_process_tree", terminate)
        monkeypatch.setattr(run_server, "is_port_in_use", lambda *_a, **_k: True)
        monkeypatch.setattr(run_server, "_probe_synth_peak", lambda *_a, **_k: 0.0)
        monkeypatch.setattr(run_server, "_spawn_sovits_process", spawn)
        run_server._calibrate_precision_after_ready(
            engine, self.HOST, self.PORT, True, "default", device="cuda"
        )
        assert spawned == [], spawned
        assert run_server._SPAWNED_SOVITS_PROC is None
        assert run_server._SPAWNED_SOVITS_LAUNCH is None
        assert run_server._SPAWNED_SOVITS_STARTED_AT is None
        assert not pid_file.exists()

        status = asyncio.run(health.engine_status_endpoint())
        assert status["state"] == "unavailable", status
        assert "退出" not in status["message"]

    def test_silence_calibration_clears_managed_state_when_replacement_spawn_raises(
        self, tmp_path, monkeypatch
    ):
        proc, engine = self._install_owned_engine(tmp_path, monkeypatch)
        pid_file = tmp_path / "gptsovits.pid"
        pid_file.write_text(str(proc.pid), encoding="utf-8")
        endpoint = parse_sovits_endpoint(f"http://{self.HOST}:{self.PORT}", source="db")

        async def resolve():
            return endpoint

        async def probe(*_args, **_kwargs):
            return SimpleNamespace(status="unreachable")

        monkeypatch.setattr(health, "resolve_effective_sovits_endpoint", resolve)
        monkeypatch.setattr(health, "_probe_gpt_sovits", probe)

        def terminate(pid, timeout=2.0):
            assert pid == proc.pid
            proc.poll.return_value = 1

        def failing_spawn(*_args, **_kwargs):
            raise OSError("spawn failed")

        monkeypatch.setattr(run_server, "terminate_process_tree", terminate)
        monkeypatch.setattr(run_server, "is_port_in_use", lambda *_a, **_k: False)
        monkeypatch.setattr(run_server, "_probe_synth_peak", lambda *_a, **_k: 0.0)
        monkeypatch.setattr(run_server, "_spawn_sovits_process", failing_spawn)
        run_server._calibrate_precision_after_ready(
            engine, self.HOST, self.PORT, True, "default", device="cuda"
        )
        assert run_server._SPAWNED_SOVITS_PROC is None
        assert run_server._SPAWNED_SOVITS_LAUNCH is None
        assert run_server._SPAWNED_SOVITS_STARTED_AT is None
        assert not pid_file.exists()

        status = asyncio.run(health.engine_status_endpoint())
        assert status["state"] == "unavailable", status
        assert "退出" not in status["message"]

    def _run_fp32_restart_harness(self, tmp_path, monkeypatch, *, on_port_check, probe_peak):
        monkeypatch.setattr(health, "_engine_restart_lock", threading.Lock())
        monkeypatch.setattr(health, "_engine_restart_target", None)
        monkeypatch.setattr(get_settings(), "project_root", tmp_path)
        monkeypatch.setattr(run_server, "PROJECT_ROOT", tmp_path)
        monkeypatch.setattr(run_server, "_SPAWNED_SOVITS_PROC", None)
        monkeypatch.setattr(run_server, "_SPAWNED_SOVITS_LAUNCH", None)
        monkeypatch.setattr(run_server, "_SPAWNED_SOVITS_STARTED_AT", None)

        endpoint = parse_sovits_endpoint(f"http://{self.HOST}:{self.PORT}", source="db")
        monkeypatch.setattr(
            "galgame2voice.services.sovits_endpoint.resolve_effective_sovits_endpoint_sync",
            lambda: endpoint,
        )
        engine = tmp_path / "engine"
        engine.mkdir()
        monkeypatch.setattr(run_server, "find_gpt_sovits_directory", lambda: engine)
        monkeypatch.setattr(
            run_server,
            "resolve_initial_device_and_half",
            lambda *_a, **_k: ("cuda", True, "default"),
        )

        active_state = None
        states_by_pid = {}
        probe_snapshots = []
        calibration_results = []
        real_calibrate = run_server.calibrate_engine_precision

        def spy_calibrate(*args, **kwargs):
            res = real_calibrate(*args, **kwargs)
            calibration_results.append(res)
            return res

        def fake_spawn(sovits_dir, host, port, is_half, device="cuda"):
            nonlocal active_state
            pid = 5001 if is_half else 5002
            proc = MagicMock(pid=pid)
            state = {
                "proc": proc,
                "is_half": is_half,
                "alive": True,
                "port_checks": 0,
                "ready_threshold": 2 if is_half else 4,
                "listening": False,
                "sleeps_after_listening": [],
            }
            proc.returncode = None
            proc.poll.side_effect = lambda: None if state["alive"] else 1
            states_by_pid[pid] = state
            active_state = state
            run_server._SPAWNED_SOVITS_PROC = proc
            run_server._SPAWNED_SOVITS_LAUNCH = EngineLaunch(
                proc, host, port, device, is_half, started_at=float(len(states_by_pid))
            )
            return proc

        def fake_terminate(pid, timeout=2.0):
            state = states_by_pid[pid]
            state["alive"] = False
            state["listening"] = False
            state["proc"].returncode = 1

        def fake_is_port_in_use(port, host):
            if active_state is None or not active_state["alive"]:
                return False
            active_state["port_checks"] += 1
            return on_port_check(active_state)

        def fake_sleep(seconds):
            if active_state is not None and active_state["listening"]:
                active_state["sleeps_after_listening"].append(seconds)

        def fake_probe(host, port, timeout=90.0):
            probe_snapshots.append({
                "is_half": active_state["is_half"],
                "port_checks": active_state["port_checks"],
                "listening": active_state["listening"],
                "sleeps_after_listening": list(active_state["sleeps_after_listening"]),
            })
            if active_state is None or not active_state["alive"] or not active_state["listening"]:
                return None
            return probe_peak(active_state)

        created = []
        real_thread = threading.Thread

        def capture_thread(*args, **kwargs):
            t = real_thread(*args, **kwargs)
            created.append(t)
            return t

        monkeypatch.setattr(run_server, "calibrate_engine_precision", spy_calibrate)
        monkeypatch.setattr(run_server, "_spawn_sovits_process", fake_spawn)
        monkeypatch.setattr(run_server, "terminate_process_tree", fake_terminate)
        monkeypatch.setattr(run_server, "is_port_in_use", fake_is_port_in_use)
        monkeypatch.setattr(run_server.time, "sleep", fake_sleep)
        monkeypatch.setattr(run_server, "_probe_synth_peak", fake_probe)
        monkeypatch.setattr(run_server.threading, "Thread", capture_thread)

        run_server.ensure_gpt_sovits_running()
        assert len(created) == 1
        created[0].join(timeout=5)
        assert not created[0].is_alive()
        return probe_snapshots, calibration_results

    def test_fp32_restart_waits_for_port_and_settle_before_probing(self, tmp_path, monkeypatch):
        def on_port_check(state):
            if state["port_checks"] >= state["ready_threshold"]:
                state["listening"] = True
            return state["listening"]

        probe_snapshots, _ = self._run_fp32_restart_harness(
            tmp_path,
            monkeypatch,
            on_port_check=on_port_check,
            probe_peak=lambda state: 0.0 if state["is_half"] else 0.42,
        )

        assert len(probe_snapshots) == 2
        assert probe_snapshots[0]["is_half"] is True
        assert probe_snapshots[0]["listening"] is True
        assert probe_snapshots[0]["sleeps_after_listening"] == [2.0]

        assert probe_snapshots[1]["is_half"] is False
        assert probe_snapshots[1]["listening"] is True
        assert probe_snapshots[1]["port_checks"] == 4
        assert probe_snapshots[1]["sleeps_after_listening"] == [2.0]

        cached = read_precision_cache(tmp_path)
        assert cached is not None
        assert cached["is_half"] is False

    def test_fp32_restart_exiting_before_port_ready_reports_restart_failure(
        self, tmp_path, monkeypatch, capsys
    ):
        def on_port_check(state):
            if state["is_half"]:
                state["listening"] = True
                return True
            if state["port_checks"] >= 2:
                state["alive"] = False
                state["proc"].returncode = 1
            return False

        probe_snapshots, calibration_results = self._run_fp32_restart_harness(
            tmp_path,
            monkeypatch,
            on_port_check=on_port_check,
            probe_peak=lambda _state: 0.0,
        )

        captured = capsys.readouterr()
        assert calibration_results == [(False, False)]
        assert [s["is_half"] for s in probe_snapshots] == [True]
        assert "FP32 引擎重启失败" in captured.out
        assert "FP32 探针仍为静音或未完成" not in captured.out
        assert read_precision_cache(tmp_path) is None


class TestReadinessMonitorCalibration:
    @staticmethod
    def _run_readiness_monitor(tmp_path, monkeypatch, proc, is_port_in_use_fn, sleep_fn=None):
        monkeypatch.setattr(run_server, "PROJECT_ROOT", tmp_path)
        monkeypatch.setattr(run_server, "_SPAWNED_SOVITS_PROC", None)
        monkeypatch.setattr(run_server.time, "sleep", sleep_fn or (lambda *_args, **_kwargs: None))
        endpoint = parse_sovits_endpoint("http://127.0.0.1:19231", source="db")
        monkeypatch.setattr(
            "galgame2voice.services.sovits_endpoint.resolve_effective_sovits_endpoint_sync",
            lambda: endpoint,
        )
        engine = tmp_path / "engine"
        engine.mkdir()
        monkeypatch.setattr(run_server, "find_gpt_sovits_directory", lambda: engine)
        monkeypatch.setattr(
            run_server,
            "resolve_initial_device_and_half",
            lambda *_a, **_k: ("cuda", True, "default"),
        )

        def fake_spawn(*_a, **_k):
            run_server._SPAWNED_SOVITS_PROC = proc
            return proc

        monkeypatch.setattr(run_server, "_spawn_sovits_process", fake_spawn)
        monkeypatch.setattr(run_server, "is_port_in_use", is_port_in_use_fn)

        calibrate_calls = []
        monkeypatch.setattr(
            run_server,
            "_calibrate_precision_after_ready",
            lambda *args, **kwargs: calibrate_calls.append((args, kwargs)),
        )

        created = []
        real_thread = threading.Thread

        def capture(*args, **kwargs):
            thread = real_thread(*args, **kwargs)
            created.append(thread)
            return thread

        monkeypatch.setattr(run_server.threading, "Thread", capture)

        run_server.ensure_gpt_sovits_running()
        assert len(created) == 1
        created[0].join(timeout=5)
        assert not created[0].is_alive()
        return engine, calibrate_calls

    def test_port_opening_after_120s_triggers_calibration(self, tmp_path, monkeypatch, capsys):
        proc = MagicMock()
        proc.poll.return_value = None
        ticks = 0

        def fake_is_port_in_use(port, host):
            nonlocal ticks
            if ticks == 0:
                ticks += 1
                return False
            current = ticks
            ticks += 1
            return current >= 260

        engine, calibrate_calls = self._run_readiness_monitor(
            tmp_path, monkeypatch, proc, fake_is_port_in_use
        )
        assert len(calibrate_calls) == 1, "readiness watcher gave up without calibrating"
        assert calibrate_calls[0][0] == (engine, "127.0.0.1", 19231, True, "default")
        assert run_server._SPAWNED_SOVITS_PROC is proc
        assert capsys.readouterr().out.count("GPT-SoVITS 模型仍在后台加载中") == 1

    @pytest.mark.parametrize("port_busy_at_exit", [False, True])
    def test_child_process_exit_stops_monitor_without_calibration(
        self, tmp_path, monkeypatch, port_busy_at_exit
    ):
        step = 0

        def fake_sleep(*_args, **_kwargs):
            nonlocal step
            step += 1

        proc = MagicMock()
        proc.returncode = 1
        proc.poll.side_effect = lambda: 1 if step >= 5 else None

        _, calibrate_calls = self._run_readiness_monitor(
            tmp_path,
            monkeypatch,
            proc,
            lambda _port, _host: port_busy_at_exit and step >= 5,
            sleep_fn=fake_sleep,
        )
        assert calibrate_calls == []


class TestProbeSynthCustomVoiceFallback:
    """
    Regression tests for custom voice profile fallback in _probe_synth_peak.
    When characters/ is empty but a custom voice profile is configured in SQLite,
    the probe must fall back to the custom voice reference audio and trigger FP32
    precision calibration when FP16 produces silent audio.
    """

    @staticmethod
    def _build_test_wav(samples: bytes, bits: int = 16, sample_rate: int = 32000, channels: int = 1) -> bytes:
        import struct
        block_align = channels * (bits // 8)
        fmt = struct.pack(
            "<HHIIHH", 1, channels, sample_rate,
            sample_rate * block_align, block_align, bits,
        )
        chunk = b"fmt " + struct.pack("<I", len(fmt)) + fmt
        chunk += b"data" + struct.pack("<I", len(samples)) + samples
        if len(samples) % 2:
            chunk += b"\x00"
        return b"RIFF" + struct.pack("<I", 4 + len(chunk)) + b"WAVE" + chunk

    @classmethod
    def _write_ref_wav(cls, path, seconds: int = 1):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(cls._build_test_wav(b"\x00\x10" * 32000 * seconds))
        return path

    @staticmethod
    def _setup_probe_env(tmp_path, monkeypatch, profiles=(), active_id=None):
        import sqlite3
        from galgame2voice.services.character_manager import get_character_manager

        char_dir = tmp_path / "characters"
        char_dir.mkdir(parents=True, exist_ok=True)
        mgr = get_character_manager(char_dir)
        monkeypatch.setattr("galgame2voice.services.character_manager.get_character_manager", lambda: mgr)
        monkeypatch.setattr(run_server, "PROJECT_ROOT", tmp_path)
        settings = get_settings()
        monkeypatch.setattr(settings, "project_root", tmp_path)

        db_file = settings.db_path
        db_file.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(str(db_file))) as conn, conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS settings ("
                "id INTEGER PRIMARY KEY CHECK (id = 1), "
                "active_voice_profile_id INTEGER DEFAULT NULL);"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS voice_profiles ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, "
                "gpt_weights_path TEXT NOT NULL DEFAULT '', sovits_weights_path TEXT NOT NULL DEFAULT '', "
                "ref_audio_path TEXT NOT NULL, prompt_text TEXT NOT NULL, "
                "prompt_lang TEXT NOT NULL DEFAULT 'ja', is_default INTEGER NOT NULL DEFAULT 0);"
            )
            conn.execute("DELETE FROM voice_profiles;")
            for row in profiles:
                conn.execute(
                    "INSERT INTO voice_profiles "
                    "(id, name, gpt_weights_path, sovits_weights_path, ref_audio_path, prompt_text, prompt_lang, is_default) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?);",
                    row,
                )
            conn.execute("INSERT OR REPLACE INTO settings (id, active_voice_profile_id) VALUES (1, ?);", (active_id,))
        return db_file

    @staticmethod
    @contextmanager
    def _serve_tts(responses):
        import http.server
        import urllib.parse

        requests_received = []
        queue = list(responses)

        class MockTtsHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path == "/tts":
                    requests_received.append(urllib.parse.parse_qs(parsed.query))
                    payload = queue.pop(0) if queue else b""
                    self.send_response(200)
                    self.send_header("Content-Type", "audio/wav")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                else:
                    self.send_response(404)
                    self.end_headers()

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), MockTtsHandler)
        host, port = server.server_address
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            yield host, port, requests_received
        finally:
            server.shutdown()
            server.server_close()

    def test_custom_voice_fallback_probes_and_calibrates_fp32(self, tmp_path, monkeypatch):
        ref_file = self._write_ref_wav(tmp_path / "custom_refs" / "user_sample.wav")
        self._setup_probe_env(
            tmp_path,
            monkeypatch,
            profiles=[(1, "custom_char", "gpt.ckpt", "sovits.pth", str(ref_file), "カスタムボイスです。", "ja", 1)],
            active_id=1,
        )
        responses = [
            self._build_test_wav(b"\x00\x00" * 1600),
            self._build_test_wav(b"\x00\x00" * 1600),
            self._build_test_wav(b"\x00\x7f" * 1600),
        ]
        with self._serve_tts(responses) as (host, port, requests_received):
            first_peak = run_server._probe_synth_peak(host, port)
            assert first_peak == 0.0, f"Expected 0.0 (silent), got {first_peak}"
            assert len(requests_received) == 1
            assert requests_received[0]["ref_audio_path"] == [str(ref_file)]
            assert requests_received[0]["prompt_text"] == ["カスタムボイスです。"]
            assert requests_received[0]["prompt_lang"] == ["ja"]

            restarts = []
            engine_dir = tmp_path / "engine"
            engine_dir.mkdir(parents=True, exist_ok=True)
            final_is_half, calibrated = run_server.calibrate_engine_precision(
                engine_dir,
                is_half=True,
                probe_fn=lambda: run_server._probe_synth_peak(host, port),
                restart_fn=lambda is_half: restarts.append(is_half) or object(),
            )

            assert restarts == [False]
            assert final_is_half is False
            assert calibrated is True
            cached = read_precision_cache(tmp_path)
            assert cached is not None
            assert cached["is_half"] is False
            assert len(requests_received) == 3

    def test_custom_voice_fallback_no_active_uses_is_default_order(self, tmp_path, monkeypatch):
        ref1 = self._write_ref_wav(tmp_path / "custom_refs" / "profile1.wav")
        ref2 = self._write_ref_wav(tmp_path / "custom_refs" / "profile2.wav")
        self._setup_probe_env(
            tmp_path,
            monkeypatch,
            profiles=[
                (1, "p1", "gpt1.ckpt", "sovits1.pth", str(ref1), "テキスト1", "ja", 0),
                (2, "p2", "gpt2.ckpt", "sovits2.pth", str(ref2), "テキスト2", "zh", 1),
            ],
            active_id=None,
        )
        with self._serve_tts([self._build_test_wav(b"\x00\x7f" * 1600)]) as (host, port, requests_received):
            peak = run_server._probe_synth_peak(host, port)
            assert peak is not None and peak > 0.0
            assert len(requests_received) == 1
            assert requests_received[0]["ref_audio_path"] == [str(ref2)]
            assert requests_received[0]["prompt_text"] == ["テキスト2"]
            assert requests_received[0]["prompt_lang"] == ["zh"]

    def test_custom_voice_fallback_non_default_character_preferred_over_db(self, tmp_path, monkeypatch):
        alice_dir = tmp_path / "characters" / "alice"
        char_audio = self._write_ref_wav(alice_dir / "alice_voice.wav", seconds=4)
        manifest = {
            "id": "alice",
            "name": "Alice",
            "emotions": {"gentle": {"audio": "alice_voice.wav", "text": "アリスです。", "lang": "ja"}},
        }
        (alice_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

        ref_file = self._write_ref_wav(tmp_path / "custom_refs" / "db_sample.wav")
        self._setup_probe_env(
            tmp_path,
            monkeypatch,
            profiles=[(1, "custom_char", "gpt.ckpt", "sovits.pth", str(ref_file), "DB音色です。", "ja", 1)],
            active_id=1,
        )
        with self._serve_tts([self._build_test_wav(b"\x00\x7f" * 1600)]) as (host, port, requests_received):
            peak = run_server._probe_synth_peak(host, port)
            assert peak is not None and peak > 0.0
            assert len(requests_received) == 1
            assert requests_received[0]["ref_audio_path"] == [str(char_audio.resolve())]
            assert requests_received[0]["prompt_text"] == ["アリスです。"]
            assert requests_received[0]["prompt_lang"] == ["ja"]

    def test_custom_voice_fallback_nonexistent_audio_returns_none(self, tmp_path, monkeypatch):
        self._setup_probe_env(
            tmp_path,
            monkeypatch,
            profiles=[(1, "p1", "gpt.ckpt", "sovits.pth", "nonexistent/path/sample.wav", "テキスト1", "ja", 1)],
            active_id=1,
        )
        assert run_server._probe_synth_peak("127.0.0.1", 9999) is None

    def test_custom_voice_fallback_locked_db_returns_none(self, tmp_path, monkeypatch):
        import sqlite3

        db_file = self._setup_probe_env(tmp_path, monkeypatch)
        conn = sqlite3.connect(str(db_file))
        try:
            conn.execute("BEGIN EXCLUSIVE;")
            assert run_server._probe_synth_peak("127.0.0.1", 9999) is None
        finally:
            conn.close()

