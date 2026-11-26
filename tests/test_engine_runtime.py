"""Real subprocess coverage for legacy engine temporary-directory adaptation."""

import os
from pathlib import Path
import runpy
import subprocess
import sys

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from galgame2voice.services.engine_runtime import prepare_engine_entrypoint, write_engine_guard
from galgame2voice.utils.precision import write_sovits_yaml_config


@pytest.mark.requires_piped_subprocess
@pytest.mark.parametrize("quote", ['"', "'"])
def test_legacy_api_starts_when_engine_temp_cannot_be_a_directory(tmp_path, quote):
    engine = tmp_path / "engine"
    engine.mkdir()
    (engine / "TEMP").write_text("cannot replace an engine-owned path")
    (engine / "weights.bin").write_text("synthetic model fixture")
    source = (
        "import os, tempfile\nnow_dir = os.getcwd()\n"
        f"_temp_dir = os.path.join(now_dir, {quote}TEMP{quote})\n"
        "os.makedirs(_temp_dir, exist_ok=True)\ntempfile.tempdir = _temp_dir\n"
        "with tempfile.TemporaryFile() as handle: handle.write(b'ok')\n"
        "assert open('weights.bin').read() == 'synthetic model fixture'\n"
        "print('engine-ready')\n"
    )
    original = engine / "api_v2.py"
    original.write_text(source)
    writable = tmp_path / "writable-temp"
    environment = {**os.environ, "TMPDIR": str(writable)}
    before = subprocess.run([sys.executable, "-I", str(original)], cwd=engine,
                            env=environment, capture_output=True, timeout=10)
    assert before.returncode != 0
    entry = prepare_engine_entrypoint(engine, tmp_path / "cache")
    after = subprocess.run([sys.executable, "-I", str(entry)], cwd=engine,
                           env=environment, capture_output=True, timeout=10)
    assert after.returncode == 0, after.stderr
    assert b"engine-ready" in after.stdout
    assert original.read_text() == source
    assert (engine / "TEMP").is_file()


def test_modern_entrypoint_is_not_rewritten(tmp_path):
    original = tmp_path / "api_v2.py"
    original.write_text("print('modern engine')\n")
    assert prepare_engine_entrypoint(tmp_path, tmp_path / "cache") == original
    assert not (tmp_path / "cache").exists()


def test_engine_config_copy_preserves_original_and_weight_paths(tmp_path):
    engine = tmp_path / "engine"
    source = engine / "GPT_SoVITS/configs/tts_infer.yaml"
    source.parent.mkdir(parents=True)
    content = "custom:\n  device: cuda\n  is_half: true\n  t2s_weights_path: GPT_SoVITS/model.ckpt\n"
    source.write_text(content)
    target = tmp_path / "cache/config.yaml"
    assert write_sovits_yaml_config(engine, False, device="cpu", output_path=target) == target
    assert source.read_text() == content
    assert "device: cpu" in target.read_text()
    assert "is_half: false" in target.read_text()
    assert "t2s_weights_path: GPT_SoVITS/model.ckpt" in target.read_text()


def test_unwritable_config_copy_does_not_silently_use_old_precision(tmp_path):
    source = tmp_path / "GPT_SoVITS/configs/tts_infer.yaml"
    source.parent.mkdir(parents=True)
    source.write_text("custom:\n  device: cuda\n  is_half: true\n")
    target = tmp_path / "occupied.yaml"
    target.mkdir()
    with pytest.raises(RuntimeError, match="配置副本"):
        write_sovits_yaml_config(tmp_path, False, device="cpu", output_path=target)


def test_engine_guard_middleware_blocks_cross_site_and_rebinding(tmp_path):
    guard_cls = runpy.run_path(str(write_engine_guard(tmp_path)))["EngineGuardMiddleware"]
    app = FastAPI()
    app.add_middleware(guard_cls)
    app.get("/control")(lambda: {"ok": True})
    client = TestClient(app, base_url="http://127.0.0.1:9880")
    assert client.get("/control").status_code == 200
    bad_host = client.get("/control", headers={"Host": "evil.example:9880"})
    assert bad_host.status_code == 403 and "语音引擎只接受本程序自己的请求" in bad_host.text
    assert client.get("/control", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert client.get("/control", headers={"Sec-Fetch-Site": "same-site"}).status_code == 403
    assert client.get("/control", headers={"Sec-Fetch-Site": "none"}).status_code == 200


@pytest.mark.requires_piped_subprocess
def test_engine_guard_wraps_fastapi_entrypoint_in_subprocess(tmp_path):
    guard = write_engine_guard(tmp_path / "bootstrap")
    fake_entry = tmp_path / "fake_api.py"
    fake_entry.write_text(
        "import sys\nfrom fastapi import FastAPI\nAPP = FastAPI()\n"
        "if __name__ == '__main__':\n    print(sys.argv[0])\n    print(len(APP.user_middleware))\n",
        encoding="utf-8",
    )
    res = subprocess.run([sys.executable, "-I", str(guard), str(fake_entry), "-a", "127.0.0.1"], capture_output=True, text=True, timeout=10)
    assert res.returncode == 0 and res.stdout.splitlines() == [str(fake_entry), "1"], res.stderr


def test_spawn_sovits_places_guard_before_entrypoint(tmp_path, monkeypatch):
    import scripts.run_server as rs
    (tmp_path / "api_v2.py").write_text("print('ok')\n", encoding="utf-8")
    seen = []
    monkeypatch.setattr(rs, "find_engine_python", lambda _: Path(sys.executable))
    monkeypatch.setattr(rs, "write_sovits_yaml_config", lambda *a, **kw: None)
    monkeypatch.setattr(rs, "build_gpt_sovits_env", lambda *a, **kw: {})
    monkeypatch.setattr(rs, "setup_windows_job_object", lambda: None)
    monkeypatch.setattr(rs, "assign_process_to_job", lambda _: True)
    monkeypatch.setattr(rs.subprocess, "Popen", lambda cmd, **kw: seen.append(cmd) or type("P", (), {"pid": 1, "poll": lambda s: None})())
    try:
        rs._spawn_sovits_process(tmp_path, "127.0.0.1", 9880, is_half=False, device="cpu", project_root=tmp_path)
        assert seen[0][3:6] == [str(tmp_path / "data/engine_cache/bootstrap/engine_guard.py"), str(tmp_path / "api_v2.py"), "-a"]
    finally:
        rs.get_engine_supervisor().clear_owned_process(project_root=tmp_path)
