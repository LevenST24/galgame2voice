"""A bound voice must be prepared before inference, under the same engine lock."""
import asyncio
import io
import json
from pathlib import Path
from types import SimpleNamespace
import wave

import httpx
import pytest

from galgame2voice.config import get_settings
from galgame2voice.database import crud
from galgame2voice.database.models import VoiceProfileCreate, VoiceProfileUpdate
from galgame2voice.database.session import get_db, init_db
from galgame2voice.services.gpt_sovits_client import GptSovitsClient
from galgame2voice.services.tts_cache_manager import TtsCacheManager
from galgame2voice.services.tts_scheduler import TtsScheduler
from galgame2voice.services.tts_service import TtsService
from galgame2voice.services.voice_resources import InsufficientMemoryError, VoiceResourceGuard


def wav(amplitude):
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(amplitude.to_bytes(2, 'little', signed=True) * 8000 * 4)
    return buffer.getvalue()


class Engine:
    def __init__(self):
        self.gpt = ''
        self.sovits = ''
        self.calls = []

    async def handle_request(self, method, path, **kwargs):
        self.calls.append((method, path))
        params = kwargs.get('params', {})
        if path == '/set_gpt_weights':
            self.gpt = params['weights_path']
        elif path == '/set_sovits_weights':
            self.sovits = params['weights_path']
        elif path == '/tts':
            # Different audio makes a wrong model observable rather than merely
            # asserting that a configuration endpoint happened to be called.
            selected_b = 'voice_b' in self.gpt and 'voice_b' in self.sovits
            return httpx.Response(200, content=wav(600 if selected_b else 200))
        return httpx.Response(200, json={'code': 0})


@pytest.fixture
async def stack(tmp_path, monkeypatch):
    monkeypatch.setenv('GALGAME2VOICE_PROJECT_ROOT', str(tmp_path))
    monkeypatch.setenv('GALGAME2VOICE_SKIP_MEM_CHECK', '1')
    get_settings.cache_clear()
    database = tmp_path/'voices.db'
    await init_db(database)
    profiles = []
    async with get_db(database) as conn:
        for name in ['voice_a', 'voice_b']:
            gpt, sovits, reference = [tmp_path/f'{name}{suffix}' for suffix in ['.ckpt', '.pth', '.wav']]
            gpt.write_bytes(b'synthetic gpt model marker')
            sovits.write_bytes(b'synthetic sovits model marker')
            reference.write_bytes(wav(200))
            profiles.append(await crud.create_voice_profile(conn, VoiceProfileCreate(
                name=name, gpt_weights_path=str(gpt), sovits_weights_path=str(sovits),
                ref_audio_path=str(reference), prompt_text='こんにちは', prompt_lang='ja',
            )))
        await crud.set_active_voice_profile(conn, profiles[0].id)
    scheduler = TtsScheduler()
    monkeypatch.setattr('galgame2voice.services.tts_service.get_tts_scheduler', lambda: scheduler)
    engine = Engine()
    client = GptSovitsClient(server=engine)
    assert await client.switch_voice_profile(profiles[0])
    engine.calls.clear()
    cache = TtsCacheManager(cache_dir=tmp_path/'audio/cache', db_path=str(database))
    service = TtsService(client=client, cache_manager=cache, db_path=str(database), audio_dir=tmp_path/'audio')
    yield SimpleNamespace(root=tmp_path, db=database, profiles=profiles, client=client,
                          engine=engine, service=service, scheduler=scheduler, cache=cache)
    await scheduler.aclose()
    await cache.aclose()
    await client.aclose()


async def render(stack, mode):
    options = {'voice_profile_id': stack.profiles[1].id}
    if mode == 'bytes':
        return await stack.service.synthesize('こんにちは', options=options, use_cache=False)
    if mode == 'file':
        return (await stack.service.synthesize_to_file('こんにちは', options=options, use_cache=False))[1].read_bytes()
    return b''.join([chunk async for chunk in stack.service.stream_tts('こんにちは', options=options, use_cache=False)])


async def test_deleted_selected_voice_reports_missing_instead_of_unavailable(stack):
    async with get_db(stack.db) as conn:
        assert await crud.delete_voice_profile(conn, stack.profiles[1].id)

    with pytest.raises(ValueError, match='所选音色已不存在.*角色与会话'):
        await stack.service.synthesize('こんにちは', options={'voice_profile_id': stack.profiles[1].id}, use_cache=False)
    assert stack.engine.calls == []


@pytest.mark.parametrize('mode', ['bytes', 'file', 'stream'])
@pytest.mark.parametrize('implicit', [False, True])
async def test_unavailable_profile_database_never_reuses_engine_voice(stack, monkeypatch, mode, implicit):
    from galgame2voice.services.voice_resolver import get_voice_resolver

    target = stack.profiles[1]
    async with get_db(stack.db) as conn:
        await crud.set_active_voice_profile(conn, target.id)
    resolver = get_voice_resolver()
    fetch = resolver._fetch_raw_profile

    async def unavailable(*args, **kwargs):
        raise OSError('synthetic profile database failure')

    monkeypatch.setattr(resolver, '_fetch_raw_profile', unavailable)
    options = {} if implicit else {'voice_profile_id': target.id}

    async def synthesize():
        if mode == 'bytes':
            return await stack.service.synthesize('こんにちは', options=options, use_cache=False)
        if mode == 'file':
            return await stack.service.synthesize_to_file('こんにちは', options=options, use_cache=False)
        return b''.join([part async for part in stack.service.stream_tts('こんにちは', options=options, use_cache=False)])

    with pytest.raises(ValueError, match='音色.*暂时无法读取'):
        await synthesize()
    assert stack.engine.calls == []

    monkeypatch.setattr(resolver, '_fetch_raw_profile', fetch)
    result = await synthesize()
    if mode == 'file':
        result = result[1].read_bytes()
    assert result == wav(600)


@pytest.mark.parametrize('mode', ['bytes', 'file', 'stream'])
async def test_same_address_recovery_cannot_synthesize_with_restarted_engine_defaults(stack, monkeypatch, mode):
    assert await render(stack, mode) == wav(600)
    online = False
    original = stack.engine.handle_request

    async def restarted_engine(method, path, **kwargs):
        if not online:
            raise httpx.ConnectError('Synthetic stopped engine')
        return await original(method, path, **kwargs)

    monkeypatch.setattr(stack.engine, 'handle_request', restarted_engine)
    assert not (await stack.client.check_health())['connected']
    stack.engine.gpt = stack.engine.sovits = 'engine-default'
    online = True
    assert (await stack.client.check_health())['connected']
    assert await render(stack, mode) == wav(600)
    async with get_db(stack.db) as conn:
        assert (await crud.get_active_voice_profile(conn)).id == stack.profiles[0].id


@pytest.mark.parametrize('mode', ['bytes', 'file', 'stream'])
@pytest.mark.parametrize('transport', ['server', 'http'])
@pytest.mark.parametrize('error', [httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadError,
                                  httpx.WriteError, httpx.RemoteProtocolError])
async def test_synthesis_connection_loss_recovers_without_health_polling(stack, monkeypatch, mode, transport, error):
    assert await render(stack, mode) == wav(600)
    fail_once = True
    original = stack.engine.handle_request

    async def restarted_engine(method, path, **kwargs):
        nonlocal fail_once
        if path == '/tts' and fail_once:
            fail_once = False
            stack.engine.gpt = stack.engine.sovits = 'engine-default'
            raise error('Synthetic restart during inference')
        return await original(method, path, **kwargs)

    if transport == 'server':
        monkeypatch.setattr(stack.engine, 'handle_request', restarted_engine)
    else:
        async def handle(request):
            return await restarted_engine(request.method, request.url.path,
                                          params=dict(request.url.params),
                                          json_data=json.loads(request.content) if request.content else None)

        stack.client.server = None
        stack.client._client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    if mode == 'stream':
        # A stream that has already started cannot safely be replayed. The next
        # explicit request must prepare the selected voice before sending audio.
        with pytest.raises(error):
            await render(stack, mode)
    assert await render(stack, mode) == wav(600)


@pytest.mark.parametrize('error', [httpx.ReadError, httpx.RemoteProtocolError])
async def test_partial_stream_failure_does_not_replay_audio_and_next_request_restores_voice(stack, error):
    opts = await stack.service._populate_voice_profile_opts({'voice_profile_id': stack.profiles[1].id})
    first_audio = wav(600)[:8192]
    attempts = 0

    class InterruptedAudio(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield first_audio
            stack.engine.gpt = stack.engine.sovits = 'engine-default'
            raise error('Synthetic connection lost after audio bytes')

    async def handle(request):
        nonlocal attempts
        if request.url.path == '/tts':
            attempts += 1
            if attempts == 1:
                return httpx.Response(200, stream=InterruptedAudio())
        return await stack.engine.handle_request(request.method, request.url.path,
                                                params=dict(request.url.params),
                                                json_data=json.loads(request.content) if request.content else None)

    stack.client.server = None
    stack.client._client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    heard = []
    with pytest.raises(error):
        async for chunk in stack.client.stream_tts('こんにちは', options=opts):
            heard.append(chunk)
    assert b''.join(heard) == first_audio
    assert attempts == 1, 'Already delivered audio must not be replayed automatically'
    assert await render(stack, 'stream') == wav(600)
    assert attempts == 2


@pytest.mark.parametrize('mode', ['bytes', 'file', 'stream'])
async def test_bound_voice_loads_before_its_first_inference(stack, mode):
    assert await render(stack, mode) == wav(600)
    assert 'voice_b' in stack.engine.gpt
    assert 'voice_b' in stack.engine.sovits
    assert [path for _, path in stack.engine.calls][-1] == '/tts'
    # Inference chooses its own models; it does not change the user's saved
    # global selection while another session/Telegram may also be speaking.
    async with get_db(stack.db) as conn:
        assert (await crud.get_active_voice_profile(conn)).id == stack.profiles[0].id


@pytest.mark.parametrize('resource', ['memory', 'vram'])
@pytest.mark.parametrize('uncertain', [False, True])
async def test_automatic_preparation_keeps_resource_guards(stack, monkeypatch, resource, uncertain):
    monkeypatch.delenv('GALGAME2VOICE_SKIP_MEM_CHECK', raising=False)
    monkeypatch.setattr('galgame2voice.services.voice_resources.release_system_memory', lambda: None)
    monkeypatch.setattr('galgame2voice.services.voice_resources.get_system_memory_status',
                        lambda: (16., .1 if resource == 'memory' else 8.))
    monkeypatch.setattr('galgame2voice.services.voice_resources.get_gpu_vram_status',
                        lambda: (8., .1 if resource == 'vram' else 8.))
    stack.client._model_state_uncertain = uncertain
    with pytest.raises(InsufficientMemoryError, match='内存|显存'):
        await render(stack, 'bytes')
    assert stack.engine.calls == []


async def test_queued_file_change_does_not_trigger_loading_or_inference(stack):
    opts = await stack.service._populate_voice_profile_opts({'voice_profile_id': stack.profiles[1].id})
    await asyncio.to_thread(Path(stack.profiles[1].gpt_weights_path).write_bytes, b'replaced while queued')
    with pytest.raises(ValueError, match='文件.*变化'):
        await stack.client.synthesize('こんにちは', options=opts)
    assert stack.engine.calls == []


async def test_plain_mapping_cannot_authorize_model_preparation(stack):
    target = stack.profiles[1]
    options = {'gpt_weights_path': target.gpt_weights_path, 'sovits_weights_path': target.sovits_weights_path,
               'ref_audio_path': target.ref_audio_path, 'prompt_text': target.prompt_text, 'prompt_lang': 'ja',
               '_voice_preparation': {'name': target.name}}
    with pytest.raises(ValueError, match='模型.*不一致'):
        await stack.client.synthesize('こんにちは', options=options)
    assert stack.engine.calls == []


async def test_partial_profile_cannot_borrow_another_voices_models_for_preparation(stack):
    async with get_db(stack.db) as conn:
        await crud.update_voice_profile(conn, stack.profiles[1].id, VoiceProfileUpdate(sovits_weights_path=''))
    with pytest.raises(ValueError, match='模型.*缺少'):
        await render(stack, 'bytes')
    assert stack.engine.calls == []


@pytest.mark.parametrize('mode', ['bytes', 'file', 'stream'])
@pytest.mark.parametrize('implicit', [False, True])
@pytest.mark.parametrize('missing', ['both', 'gpt', 'sovits', 'blank'])
async def test_incomplete_resolved_voice_never_uses_engine_models(stack, mode, implicit, missing):
    target = stack.profiles[1]
    current = stack.profiles[0]
    # The retained model is already loaded, so a mismatch check cannot detect
    # that the other half was borrowed from the previous voice.
    gpt = current.gpt_weights_path if missing == 'sovits' else ''
    sovits = current.sovits_weights_path if missing == 'gpt' else ''
    if missing == 'blank':
        gpt, sovits = '  ', current.sovits_weights_path
    async with get_db(stack.db) as conn:
        await crud.update_voice_profile(conn, target.id, VoiceProfileUpdate(
            gpt_weights_path=gpt, sovits_weights_path=sovits,
        ))
        if implicit:
            await crud.set_active_voice_profile(conn, target.id)

    options = {} if implicit else {'voice_profile_id': target.id}
    with pytest.raises(ValueError, match='模型.*缺少'):
        if mode == 'bytes':
            await stack.service.synthesize('こんにちは', options=options)
        elif mode == 'file':
            await stack.service.synthesize_to_file('こんにちは', options=options)
        else:
            _ = [chunk async for chunk in stack.service.stream_tts('こんにちは', options=options)]
    assert stack.engine.calls == []


async def test_complete_explicit_weights_override_incomplete_profile(stack):
    target = stack.profiles[1]
    async with get_db(stack.db) as conn:
        await crud.update_voice_profile(conn, target.id, VoiceProfileUpdate(
            gpt_weights_path='', sovits_weights_path='',
        ))
    result = await stack.service.synthesize('こんにちは', options={
        'voice_profile_id': target.id,
        'gpt_weights_path': target.gpt_weights_path,
        'sovits_weights_path': target.sovits_weights_path,
    }, use_cache=False)
    assert result == wav(600)


async def test_selected_pre_resolved_voice_cannot_borrow_missing_models(stack):
    target = stack.profiles[1]
    with pytest.raises(ValueError, match='模型.*缺少'):
        await stack.service.synthesize('こんにちは', options={
            '_pre_resolved': True, 'voice_profile_id': target.id,
            'gpt_weights_path': '', 'sovits_weights_path': target.sovits_weights_path,
            'ref_audio_path': target.ref_audio_path,
        })
    assert stack.engine.calls == []


async def test_remote_model_paths_do_not_require_local_files(stack):
    target = stack.profiles[1]
    async with get_db(stack.db) as conn:
        await crud.update_voice_profile(conn, target.id, VoiceProfileUpdate(
            gpt_weights_path='remote-only/gpt.ckpt', sovits_weights_path='remote-only/voice.pth',
        ))
    options = await stack.service._populate_voice_profile_opts({'voice_profile_id': target.id})
    assert options['gpt_weights_path'] == 'remote-only/gpt.ckpt'
    assert options['sovits_weights_path'] == 'remote-only/voice.pth'
    assert options['_voice_preparation'].name == target.name


async def test_no_selected_profile_keeps_engine_defaults(stack):
    options = await stack.service._populate_voice_profile_opts({'_pre_resolved': True})
    assert options['gpt_weights_path'] == stack.profiles[0].gpt_weights_path
    assert options['sovits_weights_path'] == stack.profiles[0].sovits_weights_path


async def test_endpoint_change_after_resolution_cannot_prepare_another_engine(stack):
    opts = await stack.service._populate_voice_profile_opts({'voice_profile_id': stack.profiles[1].id})
    stack.client.base_url = 'http://127.0.0.1:9999'
    with pytest.raises(ValueError, match='地址.*改变'):
        await stack.client.synthesize('こんにちは', options=opts)
    assert stack.engine.calls == []


async def test_endpoint_change_during_resource_probe_sends_no_model_load(stack, monkeypatch):
    def change_endpoint(_guard, force=False):
        stack.client.base_url = 'http://127.0.0.1:9999'
    monkeypatch.setattr(VoiceResourceGuard, 'check', change_endpoint)
    with pytest.raises(ValueError, match='地址.*改变'):
        await render(stack, 'bytes')
    assert stack.engine.calls == []


@pytest.mark.parametrize('mode', ['bytes', 'file', 'stream'])
async def test_cached_audio_does_not_load_models_again(stack, mode):
    opts = {'voice_profile_id': stack.profiles[1].id}
    async def cached_render():
        if mode == 'bytes':
            return await stack.service.synthesize('こんにちは', options=opts)
        if mode == 'file':
            return (await stack.service.synthesize_to_file('こんにちは', options=opts))[1].read_bytes()
        return b''.join([chunk async for chunk in stack.service.stream_tts('こんにちは', options=opts)])
    assert await cached_render() == wav(600)
    assert await stack.client.switch_voice_profile(stack.profiles[0])
    stack.engine.calls.clear()
    assert await cached_render() == wav(600)
    assert stack.engine.calls == []
    assert 'voice_a' in stack.client.current_gpt_weights


async def test_preparation_and_inference_do_not_allow_an_interleaving_switch(stack, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    original = stack.engine.handle_request
    async def paused(method, path, **kwargs):
        result = await original(method, path, **kwargs)
        if path == '/set_gpt_weights' and 'voice_b' in kwargs.get('params', {}).get('weights_path', ''):
            entered.set()
            await release.wait()
        return result
    monkeypatch.setattr(stack.engine, 'handle_request', paused)
    inference = asyncio.create_task(render(stack, 'bytes'))
    later = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        later = asyncio.create_task(stack.client.switch_voice_profile(stack.profiles[0]))
        await asyncio.sleep(0)
        assert not later.done()
        release.set()
        assert await inference == wav(600)
        assert await later
        paths = [path for _, path in stack.engine.calls]
        assert paths.index('/tts') < len(paths)-1
        assert paths[paths.index('/tts')+1] == '/set_gpt_weights'
    finally:
        release.set()
        for task in (inference, later):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*[task for task in (inference, later) if task is not None], return_exceptions=True)


async def test_cancelled_preparation_sends_no_tts_and_next_request_can_recover(stack, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    original = stack.engine.handle_request
    async def paused(method, path, **kwargs):
        result = await original(method, path, **kwargs)
        if path == '/set_gpt_weights' and 'voice_b' in kwargs.get('params', {}).get('weights_path', ''):
            entered.set()
            await release.wait()
        return result
    monkeypatch.setattr(stack.engine, 'handle_request', paused)
    opts = await stack.service._populate_voice_profile_opts({'voice_profile_id': stack.profiles[1].id})
    task = asyncio.create_task(stack.client.synthesize('こんにちは', options=opts))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stack.client._model_state_uncertain
        assert not any(path == '/tts' for _, path in stack.engine.calls)
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert await render(stack, 'bytes') == wav(600)
    assert not stack.client._model_state_uncertain


async def test_failed_preparation_preserves_selection_and_does_not_cache_wrong_audio(stack, monkeypatch):
    original = stack.engine.handle_request
    refuse = True
    async def refusal(method, path, **kwargs):
        if refuse and path == '/set_sovits_weights' and 'voice_b' in kwargs.get('params', {}).get('weights_path', ''):
            return httpx.Response(503, text='synthetic model load refused')
        return await original(method, path, **kwargs)
    monkeypatch.setattr(stack.engine, 'handle_request', refusal)
    with pytest.raises(ValueError, match='模型未能加载'):
        await render(stack, 'bytes')
    assert not any(path == '/tts' for _, path in stack.engine.calls)
    async with get_db(stack.db) as conn:
        assert (await crud.get_active_voice_profile(conn)).id == stack.profiles[0].id
    refuse = False
    assert await render(stack, 'bytes') == wav(600)
