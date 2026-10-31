"""Updating a voice profile must not persist a blank name or weight path."""

import pytest
from httpx import ASGITransport, AsyncClient

from galgame2voice.database import crud
from galgame2voice.database.session import get_db
from galgame2voice.main import create_app


def _profile_snapshot(profile) -> tuple:
    return (
        profile.name,
        profile.description,
        profile.gpt_weights_path,
        profile.sovits_weights_path,
        profile.ref_audio_path,
        profile.prompt_text,
        profile.updated_at,
    )


async def _stored_profile(profile_id: int):
    async with get_db() as conn:
        profile = await crud.get_voice_profile(conn, profile_id)
    assert profile is not None
    return profile


@pytest.mark.parametrize(
    ("payload", "detail"),
    [
        ({"name": "   "}, "音色名称不能为空"),
        ({"gpt_weights_path": "   "}, "GPT 权重路径不能为空"),
        ({"sovits_weights_path": "   "}, "SoVITS 权重路径不能为空"),
        ({"gpt_weights_path": ""}, "GPT 权重路径不能为空"),
        ({"sovits_weights_path": " \t\n "}, "SoVITS 权重路径不能为空"),
        (
            {"name": "新名字", "description": "被拒绝的说明", "gpt_weights_path": "   "},
            "GPT 权重路径不能为空",
        ),
    ],
)
async def test_blank_profile_update_is_rejected_and_keeps_the_saved_row(payload, detail):
    """PUT /api/voice/profiles/{id} rejects a blank name or weight path and leaves the row unchanged."""
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post("/api/voice/profiles", json={
            "name": "原音色",
            "description": "保持不变",
            "gpt_weights_path": "weights/original.ckpt",
            "sovits_weights_path": "weights/original.pth",
            "ref_audio_path": "audio/original.wav",
            "prompt_text": "こんにちは",
        })
        assert created.status_code == 201, created.text
        profile_id = created.json()["id"]
        before = _profile_snapshot(await _stored_profile(profile_id))

        updated = await client.put(f"/api/voice/profiles/{profile_id}", json=payload)

        assert updated.status_code == 422, updated.text
        assert updated.json()["detail"] == detail
        assert _profile_snapshot(await _stored_profile(profile_id)) == before
