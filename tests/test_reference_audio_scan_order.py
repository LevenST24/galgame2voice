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


def test_sovits_reference_is_offered_even_with_many_character_packs(tmp_path, monkeypatch):
    from galgame2voice.routers import voice

    project_root = tmp_path / "app"
    emotions = ("gentle", "happy", "shy", "tsundere", "cool", "sad", "angry")
    for idx in range(8):
        refs_dir = project_root / "characters" / f"角色{idx}" / "refs"
        refs_dir.mkdir(parents=True)
        for emotion in emotions:
            (refs_dir / f"{emotion}.ogg").write_bytes(b"OggS")
    sovits_root = tmp_path / "GPT-SoVITS"
    sovits_root.mkdir(parents=True)
    engine_ref = sovits_root / "reference.wav"
    engine_ref.write_bytes(b"RIFF")
    monkeypatch.setenv("GALGAME2VOICE_PROJECT_ROOT", str(project_root))
    get_settings.cache_clear()
    monkeypatch.setattr(voice, "_discover_gpt_sovits_roots", lambda: [str(sovits_root)])
    voice._scan_cache.clear()

    result = voice._scan_models_sync()

    paths = [item["path"] for item in result["audio_files"]]
    assert str(engine_ref) in paths, f"only {len(paths)} clips offered, crowded out by characters/"
