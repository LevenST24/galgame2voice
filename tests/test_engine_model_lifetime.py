"""A reachable engine address does not preserve models across a restart."""

import asyncio

import httpx
import pytest

from galgame2voice.services.gpt_sovits_client import GptSovitsClient
from galgame2voice.routers import health


class Engine:
    def __init__(self):
        self.gpt = self.sovits = "engine-default"
        self.health_error = None
        self.health_status = 200
        self.calls = []
        self.probing = asyncio.Event()
        self.finish_probe = asyncio.Event()
        self.delay_probe = False

    async def request(self, request):
        path = request.url.path
        self.calls.append((str(request.url), request.method))
        if request.method == "GET" and path in {"/control", "/"}:
            error = self.health_error
            if self.delay_probe:
                self.probing.set()
                await self.finish_probe.wait()
            if error:
                raise error("Synthetic health failure", request=request)
            return httpx.Response(self.health_status, json={"status": "running"})
        if path == "/set_gpt_weights":
            self.gpt = request.url.params["weights_path"]
        elif path == "/set_sovits_weights":
            self.sovits = request.url.params["weights_path"]
        elif request.method == "POST" and path == "/control":
            self.gpt = self.sovits = "engine-default"
        return httpx.Response(200, json={"code": 0})

    def loads(self):
        return [url for url, _ in self.calls if "/set_" in url]


@pytest.fixture
async def loaded_engine(configured_voice):
    engine = Engine()
    transport = httpx.MockTransport(engine.request)
    pool = httpx.AsyncClient(transport=transport)
    client = GptSovitsClient(base_url="http://engine.test:9880", client=pool)
    assert await client.switch_voice_profile(configured_voice)
    engine.calls.clear()
    try:
        yield client, engine, configured_voice
    finally:
        engine.finish_probe.set()
        await client.aclose()
        if client._close_task:
            await client._close_task


@pytest.mark.asyncio
@pytest.mark.parametrize('error', [httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadError,
                                  httpx.WriteError, httpx.RemoteProtocolError])
async def test_observed_connection_loss_reloads_same_voice_at_same_address(loaded_engine, error):
    client, engine, profile = loaded_engine
    expected = (engine.gpt, engine.sovits)
    engine.health_error = error
    assert not (await client.check_health())["connected"]
    engine.health_error = None
    engine.gpt = engine.sovits = "engine-default"
    assert (await client.check_health())["connected"]
    assert await client.switch_voice_profile(profile)
    assert (engine.gpt, engine.sovits) == expected
    assert len(engine.loads()) == 3


@pytest.mark.asyncio
async def test_connection_refused_then_healthy_fallback_still_requires_model_reload(loaded_engine):
    client, engine, profile = loaded_engine
    original = engine.request

    async def recovering(request):
        if request.url.path == "/control":
            raise httpx.ConnectError("Engine restarting", request=request)
        return await original(request)

    # Keep ownership of both test-only pools explicit.
    old_pool = client._client
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(recovering))
    try:
        assert (await client.check_health())["connected"]
        assert not client.models_unchanged()
        assert await client.switch_voice_profile(profile)
        assert len(engine.loads()) == 3
    finally:
        await old_pool.aclose()


def _install_health_transport(client, handler):
    """Point health probes at a one-off transport. Caller closes the previous pool."""
    old_pool = client._client
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return old_pool


@pytest.mark.asyncio
async def test_one_control_protocol_error_keeps_loaded_weights(loaded_engine):
    """A single stale connection is not an engine restart."""
    client, engine, profile = loaded_engine
    gpt, sovits = client.current_gpt_weights, client.current_sovits_weights
    assert gpt and sovits and client.models_unchanged()
    attempts = 0
    original = engine.request

    async def flaky_control(request):
        nonlocal attempts
        if request.method == "GET" and request.url.path == "/control":
            attempts += 1
            if attempts == 1:
                raise httpx.RemoteProtocolError("Synthetic stale connection", request=request)
            return httpx.Response(400, json={"status": "running"})
        return await original(request)

    old_pool = _install_health_transport(client, flaky_control)
    try:
        result = await client.check_health()
        assert result["connected"] is True
        assert client.models_unchanged()
        assert client.current_gpt_weights == gpt
        assert client.current_sovits_weights == sovits
        assert client.active_profile is profile
        assert result["http_status"] == 400
        assert result["current_gpt_weights"] == gpt
        assert result["current_sovits_weights"] == sovits
        assert attempts == 2
    finally:
        await old_pool.aclose()


@pytest.mark.asyncio
async def test_two_consecutive_connection_errors_still_clear_loaded_weights(loaded_engine):
    """Two connection failures in a row still mean the engine restarted."""
    client, engine, profile = loaded_engine
    assert client.active_profile is profile
    assert client.current_gpt_weights and client.models_unchanged()
    original = engine.request

    async def engine_gone(request):
        if request.method == "GET" and request.url.path in {"/control", "/"}:
            raise httpx.RemoteProtocolError("Synthetic engine restart", request=request)
        return await original(request)

    old_pool = _install_health_transport(client, engine_gone)
    try:
        result = await client.check_health()
        assert result["connected"] is False
        assert result["status"] == "unreachable"
        assert not client.models_unchanged()
        assert client.current_gpt_weights is None
        assert client.current_sovits_weights is None
        assert client.active_profile is None
    finally:
        await old_pool.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [httpx.ReadTimeout, httpx.PoolTimeout, None])
async def test_busy_or_slow_health_check_does_not_reload_large_models(loaded_engine, error):
    client, engine, profile = loaded_engine
    engine.health_error = error
    engine.health_status = 503
    assert not (await client.check_health())["connected"]
    engine.health_error = None
    engine.health_status = 200
    assert client.models_unchanged()
    assert await client.switch_voice_profile(profile)
    assert engine.loads() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("force", [False, True])
async def test_delayed_failure_requires_a_more_recent_remote_confirmation(loaded_engine, force):
    client, engine, profile = loaded_engine
    engine.health_error = httpx.ConnectError
    engine.delay_probe = True
    probe = asyncio.create_task(client.check_health())
    try:
        await engine.probing.wait()
        assert await client.switch_voice_profile(profile, force=force)
        engine.finish_probe.set()
        assert not (await probe)["connected"]
        if force:
            assert client.models_unchanged()
            assert client.active_profile is profile
        else:
            # A local no-op did not speak to the engine and cannot refute the
            # connection failure. Only actual model acknowledgements can.
            assert not client.models_unchanged()
            assert client.active_profile is None
            engine.delay_probe = False
            engine.health_error = None
            assert await client.switch_voice_profile(profile)
            assert len(engine.loads()) == 3
    finally:
        engine.finish_probe.set()
        await probe


@pytest.mark.asyncio
@pytest.mark.parametrize('telemetry', [False, True])
async def test_health_probe_keeps_its_original_address_during_hot_swap(loaded_engine, monkeypatch, telemetry):
    client, engine, profile = loaded_engine
    old_url = client.base_url
    engine.delay_probe = True
    monkeypatch.setattr('galgame2voice.services.gpt_sovits_client.get_gpt_sovits_client', lambda: client)
    probe = asyncio.create_task(health._probe_gpt_sovits(old_url) if telemetry else client.check_health())
    try:
        await engine.probing.wait()
        await client.set_base_url("http://new-engine.test:9999")
        replacement = Engine()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(replacement.request))
        assert await client.switch_voice_profile(profile)
        engine.finish_probe.set()
        result = await probe
        assert (result.base_url if telemetry else result["url"]) == old_url
        assert all(url.startswith(old_url) for url, _ in engine.calls)
        if not telemetry:
            assert result['current_gpt_weights'] is None
            assert result['current_sovits_weights'] is None
        assert client.active_profile is profile and client.models_unchanged()
    finally:
        engine.finish_probe.set()
        await probe


@pytest.mark.asyncio
@pytest.mark.parametrize('part', ['gpt', 'sovits', 'reference'])
async def test_delayed_health_failure_cannot_erase_a_confirmed_individual_update(loaded_engine, part):
    client, engine, _ = loaded_engine
    engine.health_error = httpx.ConnectError
    engine.delay_probe = True
    probe = asyncio.create_task(client.check_health())
    try:
        await engine.probing.wait()
        if part == 'gpt':
            assert await client.set_gpt_weights(client.current_gpt_weights)
        elif part == 'sovits':
            assert await client.set_sovits_weights(client.current_sovits_weights)
        else:
            assert await client.set_refer_audio(client.current_refer_audio)
        engine.finish_probe.set()
        assert not (await probe)['connected']
        assert client.models_unchanged()
    finally:
        engine.finish_probe.set()
        await probe


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["restart", "exit"])
async def test_control_forgets_weights_before_stopping_the_engine(loaded_engine, command):
    client, engine, profile = loaded_engine
    expected = (engine.gpt, engine.sovits)
    await client.control(command)
    assert await client.switch_voice_profile(profile)
    assert (engine.gpt, engine.sovits) == expected
    assert len(engine.loads()) == 3


@pytest.mark.asyncio
async def test_disconnected_control_response_cannot_restore_old_models(loaded_engine, monkeypatch):
    client, _, _ = loaded_engine

    async def disconnected(*args, **kwargs):
        raise httpx.ReadError('Restart took effect before its response arrived')

    monkeypatch.setattr(client, '_request', disconnected)
    with pytest.raises(httpx.ReadError):
        await client.control('restart')
    assert not client.models_unchanged()
    assert client.active_profile is None


@pytest.mark.asyncio
async def test_restart_during_a_weight_load_rejects_the_old_acknowledgement(loaded_engine):
    client, engine, profile = loaded_engine
    entered, finish = asyncio.Event(), asyncio.Event()
    original = engine.request

    async def blocked(request):
        if request.url.path == '/set_gpt_weights':
            entered.set()
            await finish.wait()
        return await original(request)

    old_pool = client._client
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(blocked))
    switching = asyncio.create_task(client.switch_voice_profile(profile, force=True))
    try:
        await entered.wait()
        await client.control('restart')
        finish.set()
        assert not await switching
        assert client.active_profile is None
        assert not client.models_unchanged()
        assert len(engine.loads()) == 1, 'Old engine cannot finish loading or roll back after restart'
    finally:
        finish.set()
        await switching
        await old_pool.aclose()
