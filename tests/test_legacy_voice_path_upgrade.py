"""Old installs stored absolute voice paths; after copying data/ and characters/
into a new folder, startup must point them at the copied files."""

import pytest

from galgame2voice.config import get_settings
from galgame2voice.database.migrations import auto_heal_voice_profiles
from galgame2voice.database.session import get_db

TAILS = ("characters/custom/v.ckpt", "characters/custom/v.pth", "audio/refs/v.wav")


@pytest.mark.asyncio
async def test_voice_paths_from_old_install_move_to_copied_files(tmp_path, monkeypatch):
    new_root, kept_root, gone_root = (tmp_path / name for name in ("new", "kept", "gone"))
    external = tmp_path / "game" / "audio" / "line.wav"
    for file in [external, *(root / tail for root in (new_root, kept_root) for tail in TAILS)]:
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(b"x")
    monkeypatch.setenv("GALGAME2VOICE_PROJECT_ROOT", str(new_root))
    get_settings.cache_clear()
    rows = {
        "gone": [str(gone_root / tail) for tail in TAILS],  # old folder deleted
        "kept": [str(kept_root / tail) for tail in TAILS],  # old folder still on disk
        "mixed": [str(new_root / TAILS[0]), TAILS[1], str(external)],
    }
    async with get_db() as conn:
        for name, (gpt, sovits, ref) in rows.items():
            await conn.execute(
                "INSERT INTO voice_profiles (name, gpt_weights_path, sovits_weights_path, ref_audio_path, prompt_text)"
                " VALUES (?, ?, ?, ?, '');",
                (name, gpt, sovits, ref),
            )
        await auto_heal_voice_profiles(conn)
        cursor = await conn.execute("SELECT name, gpt_weights_path, sovits_weights_path, ref_audio_path FROM voice_profiles;")
        healed = {row[0]: tuple(row[1:]) for row in await cursor.fetchall()}
    assert healed["gone"] == healed["kept"] == TAILS
    assert healed["mixed"] == (TAILS[0], TAILS[1], str(external))
