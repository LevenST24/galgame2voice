"""scan-models must not walk data/ and audio/ again after project_root covered them."""

import os
from collections import Counter
from types import SimpleNamespace

from galgame2voice.routers import voice


def test_nested_app_dirs_are_walked_once(tmp_path, monkeypatch):
    project = tmp_path / "project"
    data_dir = project / "data"
    audio_dir = project / "audio"
    (data_dir / "models").mkdir(parents=True)
    (audio_dir / "refs").mkdir(parents=True)
    (data_dir / "models" / "a.ckpt").write_bytes(b"x")
    (data_dir / "models" / "a.pth").write_bytes(b"x")
    (audio_dir / "refs" / "ref.wav").write_bytes(b"x")

    monkeypatch.setattr(
        voice,
        "get_settings",
        lambda: SimpleNamespace(project_root=project, data_dir=data_dir, audio_dir=audio_dir),
    )
    monkeypatch.setattr(voice, "_discover_gpt_sovits_roots", lambda: [])

    visits: Counter[str] = Counter()
    real_walk = os.walk

    def counting_walk(top, *args, **kwargs):
        for root, dirs, files in real_walk(top, *args, **kwargs):
            visits[os.path.normcase(os.path.realpath(root))] += 1
            yield root, dirs, files

    monkeypatch.setattr(voice.os, "walk", counting_walk)

    result = voice._scan_models_sync()

    assert [w["name"] for w in result["gpt_weights"]] == ["a.ckpt"]
    assert [w["name"] for w in result["sovits_weights"]] == ["a.pth"]
    assert [a["name"] for a in result["audio_files"]] == ["ref.wav"]
    # Each yielded directory may be listed by os.walk, but the scan must
    # only process (and descend into) each one once.
    assert visits[os.path.normcase(os.path.realpath(audio_dir / "refs"))] == 1
    assert visits[os.path.normcase(os.path.realpath(data_dir / "models"))] == 1


def test_engine_training_logs_are_not_offered(tmp_path, monkeypatch):
    """A trained engine's logs/ held D_/G_ checkpoints and slices that crowded out real refs."""
    project = tmp_path / "project"
    engine = tmp_path / "GPT-SoVITS-v2pro"
    (project / "data").mkdir(parents=True)
    (engine / "SoVITS_weights_v2Pro").mkdir(parents=True)
    (engine / "SoVITS_weights_v2Pro" / "mine_e8_s200.pth").write_bytes(b"x")
    checkpoints = engine / "logs" / "mine" / "logs_s2_v2Pro"
    checkpoints.mkdir(parents=True)
    (checkpoints / "D_233333333333.pth").write_bytes(b"x")
    (checkpoints / "G_233333333333.pth").write_bytes(b"x")
    slices = engine / "logs" / "mine" / "5-wav32k"
    slices.mkdir()
    for index in range(80):
        (slices / f"vocal_{index:04d}.wav").write_bytes(b"x")
    (engine / "output" / "slicer_opt").mkdir(parents=True)
    (engine / "output" / "slicer_opt" / "best_ref.wav").write_bytes(b"x")

    monkeypatch.setattr(
        voice,
        "get_settings",
        lambda: SimpleNamespace(project_root=project, data_dir=project / "data", audio_dir=project / "audio"),
    )
    monkeypatch.setattr(voice, "_discover_gpt_sovits_roots", lambda: [engine])
    real_walk = os.walk

    def name_ordered_walk(top, *args, **kwargs):
        # NTFS lists entries by name, so logs/ came before output/ and SoVITS_weights*/.
        for root, dirs, files in real_walk(top, *args, **kwargs):
            dirs.sort(key=str.lower)
            files.sort(key=str.lower)
            yield root, dirs, files

    monkeypatch.setattr(voice.os, "walk", name_ordered_walk)

    result = voice._scan_models_sync()

    assert [w["name"] for w in result["sovits_weights"]] == ["mine_e8_s200.pth"]
    assert [a["name"] for a in result["audio_files"]] == ["best_ref.wav"]
