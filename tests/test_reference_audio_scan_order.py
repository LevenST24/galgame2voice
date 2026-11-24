"""The 参考音频 list for a custom voice comes from the model scan, which keeps 50 clips.

The scan used to walk GPT-SoVITS first, so after training (hundreds of slices in
output/slicer_opt) a clip copied into the app's audio/ folder was never offered.
"""
from galgame2voice.config import get_settings


def test_reference_copied_into_audio_dir_is_offered_even_after_training(tmp_path, monkeypatch):
    from galgame2voice.routers import voice

    project_root = tmp_path / "app"
    sovits_root = tmp_path / "GPT-SoVITS"
    slices = sovits_root / "output" / "slicer_opt"
    slices.mkdir(parents=True)
    for i in range(80):
        (slices / f"vocal.wav_{i:04d}.wav").write_bytes(b"RIFF")
    monkeypatch.setenv("GALGAME2VOICE_PROJECT_ROOT", str(project_root))
    get_settings.cache_clear()
    audio_dir = get_settings().audio_dir
    audio_dir.mkdir(parents=True, exist_ok=True)
    my_ref = audio_dir / "我的 参考.wav"
    my_ref.write_bytes(b"RIFF")
    monkeypatch.setattr(voice, "_discover_gpt_sovits_roots", lambda: [str(sovits_root)])
    voice._scan_cache.clear()

    result = voice._scan_models_sync()

    paths = [item["path"] for item in result["audio_files"]]
    assert str(my_ref) in paths, f"only {len(paths)} clips offered, all from GPT-SoVITS output"
