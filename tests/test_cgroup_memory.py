"""Tests for Linux container / cgroups memory quota detection and voice switch guard."""

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from galgame2voice.main import create_app
from galgame2voice.services.voice_manager import VoiceManager, set_voice_manager
from galgame2voice.utils.hardware import get_cgroup_memory_available_gb, get_system_memory_status


@pytest.fixture
async def temp_db_path(isolate_test_database):
    """REST routes and their manager must use the same initialized database."""
    from galgame2voice.database.session import init_db

    await init_db(isolate_test_database)
    return isolate_test_database


def test_cgroup_v2_memory_detection(tmp_path, monkeypatch):
    """Verifies cgroups v2 memory limit and usage detection."""
    # Mock host available = 16 GB
    monkeypatch.setattr("galgame2voice.utils.hardware._detect_host_memory_status", lambda: (16.0, 16.0))
    monkeypatch.setenv("GALGAME2VOICE_CGROUP_ROOT", str(tmp_path))

    # 1. cgroup v2 with 2GB limit and 1.2GB usage -> 0.8GB available
    (tmp_path / "memory.max").write_text(str(int(2.0 * (1024 ** 3))), encoding="utf-8")
    (tmp_path / "memory.current").write_text(str(int(1.2 * (1024 ** 3))), encoding="utf-8")

    cg_avail = get_cgroup_memory_available_gb(cgroup_root=str(tmp_path))
    assert cg_avail is not None
    assert abs(cg_avail - 0.8) < 0.01

    free_gb = get_system_memory_status()[1]
    assert abs(free_gb - 0.8) < 0.01  # min(16.0, 0.8) = 0.8

    # 2. cgroup v2 with 'max' (unlimited) -> returns host available (16.0 GB)
    (tmp_path / "memory.max").write_text("max\n", encoding="utf-8")
    cg_avail_unlimited = get_cgroup_memory_available_gb(cgroup_root=str(tmp_path))
    assert cg_avail_unlimited is None
    free_gb_unlimited = get_system_memory_status()[1]
    assert abs(free_gb_unlimited - 16.0) < 0.01


def test_cgroup_v1_memory_detection(tmp_path, monkeypatch):
    """Verifies cgroups v1 memory limit and usage detection."""
    # Mock host available = 32 GB
    monkeypatch.setattr("galgame2voice.utils.hardware._detect_host_memory_status", lambda: (32.0, 32.0))
    monkeypatch.setenv("GALGAME2VOICE_CGROUP_ROOT", str(tmp_path))

    mem_dir = tmp_path / "memory"
    mem_dir.mkdir()

    # 1. cgroup v1 with 3GB limit and 2.5GB usage -> 0.5GB available
    (mem_dir / "memory.limit_in_bytes").write_text(str(int(3.0 * (1024 ** 3))), encoding="utf-8")
    (mem_dir / "memory.usage_in_bytes").write_text(str(int(2.5 * (1024 ** 3))), encoding="utf-8")

    cg_avail = get_cgroup_memory_available_gb(cgroup_root=str(tmp_path))
    assert cg_avail is not None
    assert abs(cg_avail - 0.5) < 0.01

    free_gb = get_system_memory_status()[1]
    assert abs(free_gb - 0.5) < 0.01  # min(32.0, 0.5) = 0.5

    # 2. cgroup v1 with unlimited sentinel (>= 1 << 60) -> returns host available (32.0 GB)
    (mem_dir / "memory.limit_in_bytes").write_text("9223372036854771712\n", encoding="utf-8")
    cg_avail_unlimited = get_cgroup_memory_available_gb(cgroup_root=str(tmp_path))
    assert cg_avail_unlimited is None
    free_gb_unlimited = get_system_memory_status()[1]
    assert abs(free_gb_unlimited - 32.0) < 0.01


@pytest.mark.asyncio
async def test_cgroup_quota_triggers_503_on_voice_switch(tmp_path, temp_db_path, mock_gpt_sovits, sample_voice_profile, monkeypatch):
    """In containerized environment, cgroup quota exhaustion blocks voice switch with 503 unless forced."""
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("GALGAME2VOICE_SKIP_MEM_CHECK", raising=False)
    monkeypatch.setenv("GALGAME2VOICE_MIN_FREE_MEM_GB", "1.5")

    # cgroup limit: 2GB max, 1.2GB used -> 0.8GB free (< 1.5GB threshold)
    (tmp_path / "memory.max").write_text(str(int(2.0 * (1024 ** 3))), encoding="utf-8")
    (tmp_path / "memory.current").write_text(str(int(1.2 * (1024 ** 3))), encoding="utf-8")
    monkeypatch.setenv("GALGAME2VOICE_CGROUP_ROOT", str(tmp_path))

    # Host has plenty of memory (64 GB)
    monkeypatch.setattr("psutil.virtual_memory", lambda: type("VM", (), {"available": 64.0 * (1024 ** 3)})())

    manager = VoiceManager(gpt_sovits_client_or_server=mock_gpt_sovits, db_path=temp_db_path)
    set_voice_manager(manager)

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as client:
        post_resp = await client.post("/api/voice/profiles", json=sample_voice_profile)
        assert post_resp.status_code == 201
        profile_id = post_resp.json()["id"]

        # Without force: rejected with 503 due to container cgroup quota
        resp_503 = await client.post("/api/voice/switch", json={"profile_id": profile_id})
        assert resp_503.status_code == 503
        assert "系统空闲内存不足" in resp_503.json()["detail"]

        # With force: bypasses quota check and succeeds
        resp_force = await client.post("/api/voice/switch", json={"profile_id": profile_id, "force": True})
        assert resp_force.status_code == 200
        assert resp_force.json()["status"] == "switched"


def test_cgroup_proc_self_subpath_detection(tmp_path, monkeypatch):
    """
    Verifies that in Kubernetes / Docker environments where /sys/fs/cgroup/memory.max is 'max',
    _get_cgroup_memory_available_gb inspects /proc/self/cgroup to discover the container slice.
    """
    # Root has 'max' (unlimited)
    (tmp_path / "memory.max").write_text("max\n", encoding="utf-8")

    # Container subpath has 2GB limit, 1.5GB current
    container_cgroup = tmp_path / "kubepods.slice" / "pod123"
    container_cgroup.mkdir(parents=True, exist_ok=True)
    (container_cgroup / "memory.max").write_text(str(int(2.0 * (1024 ** 3))), encoding="utf-8")
    (container_cgroup / "memory.current").write_text(str(int(1.5 * (1024 ** 3))), encoding="utf-8")

    # Mock /proc/self/cgroup to point to container subpath
    mock_proc = tmp_path / "proc_self_cgroup"
    mock_proc.write_text("0::/kubepods.slice/pod123\n", encoding="utf-8")

    orig_path_cls = Path
    def patched_path(p, *args, **kwargs):
        if str(p) == "/proc/self/cgroup":
            return mock_proc
        return orig_path_cls(p, *args, **kwargs)

    monkeypatch.setattr("galgame2voice.utils.hardware.Path", patched_path)

    avail = get_cgroup_memory_available_gb(cgroup_root=str(tmp_path))
    assert avail is not None
    assert abs(avail - 0.5) < 0.01  # 2.0 - 1.5 = 0.5 GB
