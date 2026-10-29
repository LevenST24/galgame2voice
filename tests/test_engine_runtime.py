"""Real subprocess coverage for legacy engine temporary-directory adaptation."""

import os
import subprocess
import sys

import pytest

from galgame2voice.services.engine_runtime import prepare_engine_entrypoint
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
