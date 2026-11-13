"""Creating and updating a voice profile must reject blank name or weight paths."""

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
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as client:
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


@pytest.mark.parametrize(
    ("field_overrides", "detail"),
    [
        ({"name": "   "}, "音色名称不能为空"),
        ({"gpt_weights_path": "   "}, "GPT 权重路径不能为空"),
        ({"gpt_weights_path": " \t\n "}, "GPT 权重路径不能为空"),
        ({"gpt_weights_path": ""}, None),
        ({"sovits_weights_path": "   "}, "SoVITS 权重路径不能为空"),
        ({"sovits_weights_path": " \t\n "}, "SoVITS 权重路径不能为空"),
        ({"sovits_weights_path": ""}, None),
    ],
)
async def test_blank_profile_create_is_rejected_and_leaves_no_row(field_overrides, detail):
    """POST /api/voice/profiles rejects blank or empty name/weights without creating a row or changing active profile."""
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as client:
        # Create an initial valid profile so we can verify active profile preservation
        initial = await client.post("/api/voice/profiles", json={
            "name": "初始基准音色",
            "gpt_weights_path": "weights/base.ckpt",
            "sovits_weights_path": "weights/base.pth",
        })
        assert initial.status_code == 201, initial.text
        initial_id = initial.json()["id"]

        async with get_db() as conn:
            profiles_before = await crud.list_voice_profiles(conn)
            active_before = await crud.get_active_voice_profile(conn)

        base_payload = {
            "name": "待测试音色",
            "description": "测试空白路径校验",
            "gpt_weights_path": "weights/valid.ckpt",
            "sovits_weights_path": "weights/valid.pth",
            "ref_audio_path": "audio/valid.wav",
            "prompt_text": "こんにちは",
        }
        base_payload.update(field_overrides)

        response = await client.post("/api/voice/profiles", json=base_payload)

        assert response.status_code == 422, response.text
        if detail is not None:
            assert response.json()["detail"] == detail

        async with get_db() as conn:
            profiles_after = await crud.list_voice_profiles(conn)
            active_after = await crud.get_active_voice_profile(conn)

        assert [p.id for p in profiles_after] == [p.id for p in profiles_before]
        before_active_id = active_before.id if active_before else initial_id
        after_active_id = active_after.id if active_after else None
        assert after_active_id == before_active_id


async def test_valid_profile_create_strips_paths_and_allows_nonexistent_local_files():
    """POST /api/voice/profiles strips leading/trailing spaces and does not require model files to exist locally.

    Validates that compliant relative paths (e.g. for remote engine host filesystems)
    are accepted without requiring local file presence on disk.
    """
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as client:
        # Test path trimming
        resp_trimmed = await client.post("/api/voice/profiles", json={
            "name": "  去空白音色  ",
            "description": "两侧带空白的合法路径",
            "gpt_weights_path": "  weights/trimmed.ckpt  ",
            "sovits_weights_path": "  weights/trimmed.pth  ",
            "ref_audio_path": "  audio/trimmed.wav  ",
        })
        assert resp_trimmed.status_code == 201, resp_trimmed.text
        trimmed_id = resp_trimmed.json()["id"]
        stored_trimmed = await _stored_profile(trimmed_id)
        assert stored_trimmed.name == "去空白音色"
        assert stored_trimmed.gpt_weights_path == "weights/trimmed.ckpt"
        assert stored_trimmed.sovits_weights_path == "weights/trimmed.pth"
        assert stored_trimmed.ref_audio_path == "audio/trimmed.wav"

        # Test model paths complying with path guard but not existing on local disk
        resp_nonexistent = await client.post("/api/voice/profiles", json={
            "name": "远端引擎文件系统音色",
            "gpt_weights_path": "weights/remote_engine_host_gpt.ckpt",
            "sovits_weights_path": "weights/remote_engine_host_sovits.pth",
        })
        assert resp_nonexistent.status_code == 201, resp_nonexistent.text
        remote_id = resp_nonexistent.json()["id"]
        stored_remote = await _stored_profile(remote_id)
        assert stored_remote.name == "远端引擎文件系统音色"
        assert stored_remote.gpt_weights_path == "weights/remote_engine_host_gpt.ckpt"
        assert stored_remote.sovits_weights_path == "weights/remote_engine_host_sovits.pth"
