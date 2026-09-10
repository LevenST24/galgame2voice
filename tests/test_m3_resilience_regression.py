"""
Regression tests for M3 Hardening:
1. SessionManager table name cache invalidation & arbitrary connection row_factory safety.
2. Voice switch REST semantics: 404 precedence for non-existent profiles.
3. Voice switch memory guard: 503 on low memory and force=True override bypass.
4. SSRF URL Guard: thread-safe DNS TTL expiration and explicit cache purge preventing DNS rebinding.
"""

import asyncio
import socket
import time
import pytest
import aiosqlite
from httpx import AsyncClient, ASGITransport
from galgame2voice.services.session_manager import SessionManager
from galgame2voice.main import create_app
from galgame2voice.services.voice_manager import VoiceManager, set_voice_manager
from galgame2voice.security.url_guard import validate_llm_base_url, clear_dns_cache


@pytest.mark.asyncio
async def test_session_manager_stale_cache_recovery():
    """Verify that SessionManager handles dynamic table re-initialization and arbitrary connection row_factories."""
    async with aiosqlite.connect(":memory:") as db:
        await db.execute("""
            CREATE TABLE session_messages (
                id INTEGER PRIMARY KEY,
                session_id TEXT,
                role TEXT,
                content_chinese TEXT,
                content_japanese TEXT,
                raw_content TEXT
            );
        """)
        await db.execute(
            "INSERT INTO session_messages VALUES (1, 's1', 'user', '你好', 'こんにちは', '你好');"
        )
        await db.commit()

        mgr = SessionManager(db_path=":memory:")
        mgr._table_name = "messages"  # Simulate stale cached table name

        # This should recover, detect session_messages, ensure row_factory, and return the row
        turns = await mgr._get_history_on_conn(db, "s1", 10, 8000)
        assert len(turns) == 1
        assert turns[0].content_chinese == "你好"


@pytest.mark.asyncio
async def test_voice_switch_nonexistent_profile_404_precedence(temp_db_path, mock_gpt_sovits, monkeypatch):
    """
    Non-existent voice profile switch should return 404 Not Found,
    NOT 503 Service Unavailable, even when memory is low.
    """
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("GALGAME2VOICE_SKIP_MEM_CHECK", raising=False)
    # Simulate low memory (e.g. 0.8 GB free < scaled threshold)
    monkeypatch.setattr("galgame2voice.services.voice_manager.get_system_memory_status", lambda: (16.0, 0.8))

    manager = VoiceManager(gpt_sovits_client_or_server=mock_gpt_sovits, db_path=temp_db_path)
    set_voice_manager(manager)

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/voice/switch", json={"profile_id": 99999})
        # REST semantics: Non-existent profile should be 404, not 503!
        assert resp.status_code == 404, f"Expected 404 for missing profile, got {resp.status_code}: {resp.text}"


@pytest.mark.asyncio
async def test_voice_switch_existing_profile_low_memory_503_and_force_override(
    temp_db_path, mock_gpt_sovits, sample_voice_profile, monkeypatch
):
    """
    When profile exists and free memory is < 1.5GB:
    - Without force: returns 503 Service Unavailable.
    - With force=True: bypasses memory check and switches successfully (200).
    """
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("GALGAME2VOICE_SKIP_MEM_CHECK", raising=False)
    # Simulate low memory (0.8 GB free)
    monkeypatch.setattr("galgame2voice.services.voice_manager.get_system_memory_status", lambda: (16.0, 0.8))

    manager = VoiceManager(gpt_sovits_client_or_server=mock_gpt_sovits, db_path=temp_db_path)
    set_voice_manager(manager)

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        post_resp = await client.post("/api/voice/profiles", json=sample_voice_profile)
        assert post_resp.status_code == 201
        profile_id = post_resp.json()["id"]

        # Switch without force -> 503
        resp_503 = await client.post("/api/voice/switch", json={"profile_id": profile_id})
        assert resp_503.status_code == 503
        assert "系统空闲内存不足" in resp_503.json()["detail"]

        # Switch with force=True -> 200
        resp_force = await client.post("/api/voice/switch", json={"profile_id": profile_id, "force": True})
        assert resp_force.status_code == 200
        assert resp_force.json()["status"] == "switched"


def test_dns_rebind_ssrf_cache_vulnerability(monkeypatch):
    """
    Simulates DNS rebinding attack where domain originally resolves to public IP
    and is later updated to private/loopback IP (127.0.0.1).
    A secure SSRF guard must detect the updated IP via TTL expiration and clear_dns_cache.
    """
    clear_dns_cache()
    current_time = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: current_time[0])

    # Step 1: Initial DNS resolves to public IP 93.184.216.34 (example.com)
    mock_addrs = [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
    ]
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: mock_addrs)
    ok, _ = validate_llm_base_url("https://rebind-test.com/v1")
    assert ok is True

    # Step 2: Attacker changes DNS record to point to local loopback 127.0.0.1
    mock_addrs = [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))
    ]
    # Advance time beyond 60s TTL
    current_time[0] += 65.0

    # With TTL expiration, the stale public IP cache has expired, so 127.0.0.1 is blocked!
    ok_after_ttl, reason = validate_llm_base_url("https://rebind-test.com/v1")
    assert ok_after_ttl is False
    assert "环回/私网" in reason

    # Step 3: Explicit clear_dns_cache immediately purges cache regardless of time
    clear_dns_cache()
    current_time[0] = 1000.0
    mock_addrs = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
    assert validate_llm_base_url("https://rebind-test.com/v1")[0] is True
    mock_addrs = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
    clear_dns_cache()
    assert validate_llm_base_url("https://rebind-test.com/v1")[0] is False


# ============================================================================
# 5. Linux Container / cgroups Memory Quota Tests
# ============================================================================

def test_cgroup_v2_memory_detection(tmp_path, monkeypatch):
    """Verifies cgroups v2 memory limit and usage detection."""
    from galgame2voice.utils.hardware import get_cgroup_memory_available_gb, get_system_memory_status

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
    from galgame2voice.utils.hardware import get_cgroup_memory_available_gb, get_system_memory_status

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
    async with AsyncClient(transport=transport, base_url="http://test") as client:
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


# ============================================================================
# 6. POSIX Signal Handling & Windows Process Tree Termination Tests
# ============================================================================

def test_posix_signal_registration_and_handling(monkeypatch):
    """Verifies setup_signal_handlers registers SIGTERM and SIGHUP on non-Windows and calls cleanup."""
    import signal
    import sys
    from unittest.mock import MagicMock
    import scripts.run_server as run_server

    registered_handlers = {}
    def mock_signal(sig, handler):
        registered_handlers[sig] = handler

    monkeypatch.setattr(signal, "signal", mock_signal)
    monkeypatch.setattr(sys, "platform", "linux")

    # Add mock SIGHUP if not present on Windows platform
    mock_sighup = getattr(signal, "SIGHUP", 1)
    monkeypatch.setattr(signal, "SIGHUP", mock_sighup, raising=False)

    run_server.setup_signal_handlers()

    assert signal.SIGTERM in registered_handlers
    assert mock_sighup in registered_handlers

    # Simulate receiving SIGTERM
    cleanup_called = [False]
    monkeypatch.setattr(run_server, "cleanup_subprocesses", lambda: cleanup_called.__setitem__(0, True))

    with pytest.raises(SystemExit) as exc_info:
        registered_handlers[signal.SIGTERM](signal.SIGTERM, None)

    assert cleanup_called[0] is True
    assert exc_info.value.code == 128 + signal.SIGTERM


def test_process_tree_termination_and_cleanup(tmp_path, monkeypatch):
    """Verifies terminate_process_tree terminates process tree with taskkill fallback and cleans files."""
    import scripts.run_server as run_server
    import sys
    from unittest.mock import MagicMock

    monkeypatch.setattr(run_server, "PROJECT_ROOT", tmp_path)
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)

    # 1. Test psutil process tree termination
    mock_parent = MagicMock()
    mock_child1 = MagicMock()
    mock_child2 = MagicMock()
    mock_parent.children.return_value = [mock_child1, mock_child2]

    mock_psutil = MagicMock()
    mock_psutil.Process.return_value = mock_parent
    # child1 alive after first wait, killed and gone after second wait
    mock_psutil.wait_procs.side_effect = [([], [mock_child1]), ([mock_child1], [])]

    taskkill_args = []
    def mock_subprocess_run(args, **kwargs):
        taskkill_args.append(args)
        return MagicMock(returncode=0)

    monkeypatch.setattr(run_server.subprocess, "run", mock_subprocess_run)
    monkeypatch.setitem(sys.modules, "psutil", mock_psutil)
    run_server.terminate_process_tree(12345)

    mock_parent.terminate.assert_called_once()
    mock_child1.terminate.assert_called_once()
    mock_child2.terminate.assert_called_once()
    mock_child1.kill.assert_called_once()

    # 2. Test taskkill fallback when psutil is missing or fails
    monkeypatch.setitem(sys.modules, "psutil", None)
    monkeypatch.setattr(run_server.sys, "platform", "win32")

    taskkill_args.clear()
    run_server.terminate_process_tree(54321)

    assert any("taskkill" in str(arg) and "54321" in str(arg) for arg in taskkill_args)

    # 3. Test cleanup_subprocesses removes files and kills pid
    (tmp_path / "data" / "active_port.txt").write_text("8080", encoding="utf-8")
    (tmp_path / "galgame2voice.pid").write_text("1001", encoding="utf-8")
    (tmp_path / "gptsovits.pid").write_text("54321", encoding="utf-8")

    run_server.cleanup_subprocesses()

    assert not (tmp_path / "data" / "active_port.txt").exists()
    assert not (tmp_path / "galgame2voice.pid").exists()
    assert not (tmp_path / "gptsovits.pid").exists()


# ============================================================================
# 7. Concurrent Voice Switch Concurrency & State Guard Tests
# ============================================================================

@pytest.mark.asyncio
async def test_concurrent_voice_switch_serialization_and_toctou_prevention(temp_db_path, mock_gpt_sovits, monkeypatch):
    """
    Simulates concurrent voice switch requests where memory is sufficient for the first
    switch but drops below threshold before the second switch.
    Without switch_lock, both would race through the memory check.
    Under switch_lock, the second switch detects depleted memory under the lock and gets 503.
    """
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("GALGAME2VOICE_SKIP_MEM_CHECK", raising=False)

    # Memory starts at 2.0GB, drops to 0.8GB after first check
    mem_readings = [2.0, 0.8, 0.8, 0.8]
    def mock_free_mem(*args, **kwargs):
        return (16.0, mem_readings.pop(0) if mem_readings else 0.8)

    monkeypatch.setattr("galgame2voice.services.voice_manager.get_system_memory_status", mock_free_mem)

    manager = VoiceManager(gpt_sovits_client_or_server=mock_gpt_sovits, db_path=temp_db_path)
    set_voice_manager(manager)

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Create Profile A
        p_a = await client.post("/api/voice/profiles", json={
            "name": "Character A",
            "gpt_weights_path": "a.ckpt",
            "sovits_weights_path": "a.pth",
            "refer_audio_path": "a.wav",
            "refer_text": "こんにちは",
        })
        id_a = p_a.json()["id"]

        # Create Profile B
        p_b = await client.post("/api/voice/profiles", json={
            "name": "Character B",
            "gpt_weights_path": "b.ckpt",
            "sovits_weights_path": "b.pth",
            "refer_audio_path": "b.wav",
            "refer_text": "さようなら",
        })
        id_b = p_b.json()["id"]

        # Concurrently send switch requests
        res_a, res_b = await asyncio.gather(
            client.post("/api/voice/switch", json={"profile_id": id_a}),
            client.post("/api/voice/switch", json={"profile_id": id_b}),
            return_exceptions=True,
        )

        status_codes = {res_a.status_code, res_b.status_code}
        # One succeeded (200), one safely rejected due to serialized low memory check (503)
        assert 200 in status_codes, f"Expected at least one 200, got {status_codes}"
        assert 503 in status_codes, f"Expected one 503 from TOCTOU guard, got {status_codes}"


@pytest.mark.asyncio
async def test_concurrent_voice_switch_same_profile_optimization(temp_db_path, mock_gpt_sovits, sample_voice_profile, monkeypatch):
    """
    Multiple concurrent switch requests for the same already active profile
    return 200 OK without redundant reload or race conditions.
    """
    manager = VoiceManager(gpt_sovits_client_or_server=mock_gpt_sovits, db_path=temp_db_path)
    set_voice_manager(manager)

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        p = await client.post("/api/voice/profiles", json=sample_voice_profile)
        p_id = p.json()["id"]

        # Initial switch
        init_resp = await client.post("/api/voice/switch", json={"profile_id": p_id})
        assert init_resp.status_code == 200

        # Rapid concurrent switches to the same profile
        tasks = [client.post("/api/voice/switch", json={"profile_id": p_id}) for _ in range(5)]
        responses = await asyncio.gather(*tasks)

        for r in responses:
            assert r.status_code == 200
            assert r.json()["status"] == "switched"
            assert r.json()["profile_id"] == p_id


# ============================================================================
# 8. Container Subpath, Signal Safety & Deep State Guard Tests
# ============================================================================

def test_cgroup_proc_self_subpath_detection(tmp_path, monkeypatch):
    """
    Verifies that in Kubernetes / Docker environments where /sys/fs/cgroup/memory.max is 'max',
    _get_cgroup_memory_available_gb inspects /proc/self/cgroup to discover the container slice.
    """
    from galgame2voice.utils.hardware import get_cgroup_memory_available_gb
    from pathlib import Path

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


def test_terminate_process_tree_safety_and_access_denied_fallback(monkeypatch):
    """
    Verifies:
    1. Safety guard protects PID 0, PID 1, and self (os.getpid()).
    2. psutil AccessDenied does NOT falsely mark success and triggers OS-level fallback.
    """
    import os
    import sys
    import scripts.run_server as run_server
    from unittest.mock import MagicMock

    # 1. Safety guard checks
    mock_psutil = MagicMock()
    monkeypatch.setitem(sys.modules, "psutil", mock_psutil)

    run_server.terminate_process_tree(0)
    run_server.terminate_process_tree(1)
    run_server.terminate_process_tree(os.getpid())
    mock_psutil.Process.assert_not_called()

    # 2. AccessDenied triggers fallback
    mock_psutil.Process.side_effect = mock_psutil.AccessDenied()

    called_commands = []
    def mock_run(cmd, **kwargs):
        called_commands.append(cmd)
        return MagicMock(returncode=0)

    monkeypatch.setattr(run_server.subprocess, "run", mock_run)
    run_server.terminate_process_tree(99999)

    # Must have invoked fallback command
    assert len(called_commands) > 0
    cmd_str = " ".join(str(c) for c in called_commands[0])
    assert "99999" in cmd_str


@pytest.mark.asyncio
async def test_switch_active_profile_alias_and_crud_by_name(temp_db_path, mock_gpt_sovits, sample_voice_profile):
    """Verifies crud.get_voice_profile_by_name and VoiceManager.switch_active_profile alias."""
    from galgame2voice.database.session import get_db
    from galgame2voice.database import crud
    from galgame2voice.database.models import VoiceProfileCreate

    async with get_db(temp_db_path) as conn:
        created = await crud.create_voice_profile(conn, VoiceProfileCreate(**sample_voice_profile))
        found = await crud.get_voice_profile_by_name(conn, sample_voice_profile["name"])
        assert found is not None
        assert found.id == created.id

    manager = VoiceManager(gpt_sovits_client_or_server=mock_gpt_sovits, db_path=temp_db_path)
    success = await manager.switch_active_profile(sample_voice_profile["name"], persist=True)
    assert success is True
    assert manager.active_profile.name == sample_voice_profile["name"]


@pytest.mark.asyncio
async def test_voice_switch_same_profile_with_different_refer_audio_triggers_switch(
    temp_db_path, mock_gpt_sovits, sample_voice_profile, monkeypatch
):
    """
    If a profile's refer audio or prompt text changes, switch_voice must NOT skip switching,
    ensuring updated reference audio is dispatched to GPT-SoVITS.
    """
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("GALGAME2VOICE_SKIP_MEM_CHECK", raising=False)
    # Mock memory check to return safe value — this test validates refer audio
    # change detection, not memory gating.
    monkeypatch.setattr("galgame2voice.services.voice_manager.get_system_memory_status", lambda: (16.0, 10.0))

    manager = VoiceManager(gpt_sovits_client_or_server=mock_gpt_sovits, db_path=temp_db_path)
    set_voice_manager(manager)

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Create initial profile
        p_resp = await client.post("/api/voice/profiles", json=sample_voice_profile)
        p_id = p_resp.json()["id"]

        # Initial switch
        sw1 = await client.post("/api/voice/switch", json={"profile_id": p_id})
        assert sw1.status_code == 200

        # Update profile's refer audio
        new_audio = "updated_refer.wav"
        up_resp = await client.put(f"/api/voice/profiles/{p_id}", json={"refer_audio_path": new_audio})
        assert up_resp.status_code == 200

        # Track set_refer_audio calls on mock server
        refer_calls = []
        orig_req = manager.client._request
        async def mock_req(method, endpoint, **kwargs):
            if endpoint == "/set_refer_audio":
                refer_calls.append(kwargs.get("params", {}).get("refer_audio_path"))
            return await orig_req(method, endpoint, **kwargs)

        monkeypatch.setattr(manager.client, "_request", mock_req)

        # Switch without force -> must NOT skip because refer audio is updated
        sw2 = await client.post("/api/voice/switch", json={"profile_id": p_id})
        assert sw2.status_code == 200
        assert new_audio in refer_calls


@pytest.mark.asyncio
async def test_voice_switch_concurrent_deletion_returns_404_under_lock(
    temp_db_path, mock_gpt_sovits, sample_voice_profile
):
    """
    If a profile is deleted while a switch request waits for switch_lock,
    the request detects deletion under lock and returns 404.
    """
    from galgame2voice.database.session import get_db
    from galgame2voice.database import crud

    manager = VoiceManager(gpt_sovits_client_or_server=mock_gpt_sovits, db_path=temp_db_path)
    set_voice_manager(manager)

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        p_resp = await client.post("/api/voice/profiles", json=sample_voice_profile)
        p_id = p_resp.json()["id"]

        # Hold switch lock and delete profile concurrently
        async with manager.switch_lock:
            # Delete profile from database while switch is queued
            async with get_db() as conn:
                await crud.delete_voice_profile(conn, p_id)

            # Start switch task in background while lock is held
            switch_task = asyncio.create_task(
                client.post("/api/voice/switch", json={"profile_id": p_id})
            )
            await asyncio.sleep(0.05)

        # Lock is now released, switch_task proceeds and detects deletion
        res = await switch_task
        assert res.status_code == 404
        assert "not found" in res.json()["detail"].lower()

