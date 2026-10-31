"""
Evidence-based precision calibration tests:
1. precision store read/write and resolve_initial_is_half priority (env > cache > default).
2. calibrate_engine_precision decision table (audible / silent / inconclusive), with a
   fake probe + fake restart — no real engine or GPU needed.
"""

import asyncio
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
            seen["status"] = asyncio.run(health.engine_status_endpoint())
            return False

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
        monkeypatch.setattr(run_server, "is_port_in_use", lambda *_a, **_k: False)
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


class TestReadinessMonitorCalibration:
    def test_port_opening_after_120s_triggers_calibration(self, tmp_path, monkeypatch):
        monkeypatch.setattr(run_server, "PROJECT_ROOT", tmp_path)
        monkeypatch.setattr(run_server.time, "sleep", lambda *_args, **_kwargs: None)
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
        proc = MagicMock()
        proc.poll.return_value = None
        monkeypatch.setattr(run_server, "_spawn_sovits_process", lambda *_a, **_k: proc)

        # First call before spawn is tick 0 (returns False).
        # Ticks 1..240 return False; tick 241+ (i.e. > 240 iterations = 120s) returns True.
        ticks = 0

        def fake_is_port_in_use(port, host):
            nonlocal ticks
            if ticks == 0:
                ticks += 1
                return False
            current = ticks
            ticks += 1
            return current >= 250

        monkeypatch.setattr(run_server, "is_port_in_use", fake_is_port_in_use)

        calibrated = []
        monkeypatch.setattr(
            run_server,
            "_calibrate_precision_after_ready",
            lambda *args, **kwargs: calibrated.append((args, kwargs)),
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
        assert len(calibrated) == 1, "readiness watcher gave up without calibrating"
        assert calibrated[0][0][0] == engine
        assert calibrated[0][0][1] == "127.0.0.1"
        assert calibrated[0][0][2] == 19231
        assert calibrated[0][0][3] is True
        assert calibrated[0][0][4] == "default"

    def test_child_process_exit_stops_monitor_without_calibration(self, tmp_path, monkeypatch):
        monkeypatch.setattr(run_server, "PROJECT_ROOT", tmp_path)
        monkeypatch.setattr(run_server.time, "sleep", lambda *_args, **_kwargs: None)
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

        ticks = 0
        proc = MagicMock()
        proc.returncode = 1

        def fake_poll():
            nonlocal ticks
            return 1 if ticks >= 10 else None

        proc.poll.side_effect = fake_poll
        monkeypatch.setattr(run_server, "_spawn_sovits_process", lambda *_a, **_k: proc)

        def fake_is_port_in_use(port, host):
            nonlocal ticks
            ticks += 1
            return False

        monkeypatch.setattr(run_server, "is_port_in_use", fake_is_port_in_use)

        calibrated = []
        monkeypatch.setattr(
            run_server,
            "_calibrate_precision_after_ready",
            lambda *args, **kwargs: calibrated.append((args, kwargs)),
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
        assert calibrated == [], "calibration should not trigger if child process exited"

    def test_child_process_exit_with_port_busy_does_not_calibrate(self, tmp_path, monkeypatch):
        monkeypatch.setattr(run_server, "PROJECT_ROOT", tmp_path)
        monkeypatch.setattr(run_server.time, "sleep", lambda *_args, **_kwargs: None)
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

        step = 0

        def fake_sleep(*args, **kwargs):
            nonlocal step
            step += 1

        monkeypatch.setattr(run_server.time, "sleep", fake_sleep)

        proc = MagicMock()
        proc.returncode = 1

        def fake_poll():
            return 1 if step >= 5 else None

        proc.poll.side_effect = fake_poll
        monkeypatch.setattr(run_server, "_spawn_sovits_process", lambda *_a, **_k: proc)

        # Port is occupied at step >= 5 (when child process has already crashed).
        # Readiness monitor must check proc.poll() first and abort without calibrating.
        def fake_is_port_in_use(port, host):
            return step >= 5

        monkeypatch.setattr(run_server, "is_port_in_use", fake_is_port_in_use)

        calibrated = []
        monkeypatch.setattr(
            run_server,
            "_calibrate_precision_after_ready",
            lambda *args, **kwargs: calibrated.append((args, kwargs)),
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
        assert calibrated == [], "calibration must not trigger if child process died even if port is busy"

    def test_slow_loading_hint_printed_once_past_120s(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(run_server, "PROJECT_ROOT", tmp_path)
        monkeypatch.setattr(run_server.time, "sleep", lambda *_args, **_kwargs: None)
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
        proc = MagicMock()
        proc.poll.return_value = None
        monkeypatch.setattr(run_server, "_spawn_sovits_process", lambda *_a, **_k: proc)

        ticks = 0

        def fake_is_port_in_use(port, host):
            nonlocal ticks
            if ticks == 0:
                ticks += 1
                return False
            current = ticks
            ticks += 1
            return current >= 260

        monkeypatch.setattr(run_server, "is_port_in_use", fake_is_port_in_use)
        monkeypatch.setattr(
            run_server,
            "_calibrate_precision_after_ready",
            lambda *args, **kwargs: None,
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

        captured = capsys.readouterr()
        assert captured.out.count("GPT-SoVITS 模型仍在后台加载中") == 1


