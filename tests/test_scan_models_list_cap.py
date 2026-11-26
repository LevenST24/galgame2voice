"""The 50-item scan list shows the user's own data/ files first and says how many were cut."""

from types import SimpleNamespace

from galgame2voice.routers import voice


def test_data_files_come_before_a_big_engine_folder_and_the_cut_is_reported(tmp_path, monkeypatch):
    project = tmp_path / "project"
    data_dir = project / "data"
    audio_dir = project / "audio"
    engine = tmp_path / "GPT-SoVITS"
    (data_dir / "refs").mkdir(parents=True)
    audio_dir.mkdir(parents=True)
    (engine / "dataset").mkdir(parents=True)
    for i in range(80):
        (engine / "dataset" / f"train_{i:03}.wav").write_bytes(b"x")
    (data_dir / "refs" / "my_ref.wav").write_bytes(b"x")

    monkeypatch.setattr(
        voice, "get_settings", lambda: SimpleNamespace(project_root=project, data_dir=data_dir, audio_dir=audio_dir)
    )
    monkeypatch.setattr(voice, "_discover_gpt_sovits_roots", lambda: [str(engine)])

    result = voice._scan_models_sync()

    assert len(result["audio_files"]) == 50
    assert result["audio_files"][0]["name"] == "my_ref.wav"
    assert result["hidden"] == {"audio_files": 31}
