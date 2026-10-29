"""Audio reuse follows the effective model, source files and database owner."""

import asyncio
from contextlib import aclosing
import io
import os
from pathlib import Path
from types import SimpleNamespace
import wave

import httpx
import pytest

from galgame2voice.config import get_settings
from galgame2voice.database import crud
from galgame2voice.database.models import VoiceProfileCreate, VoiceProfileUpdate
from galgame2voice.database.session import init_db, get_db
from galgame2voice.services.gpt_sovits_client import GptSovitsClient
from galgame2voice.services.tts_cache_manager import TtsCacheManager, get_tts_cache_manager
from galgame2voice.services.tts_scheduler import TtsScheduler
from galgame2voice.services.tts_service import TtsService


def wav(amplitude):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(amplitude.to_bytes(2, "little", signed=True) * 8000 * 4)
    return buffer.getvalue()


@pytest.fixture
async def stack(tmp_path, monkeypatch):
    monkeypatch.setenv("GALGAME2VOICE_PROJECT_ROOT", str(tmp_path))
    get_settings.cache_clear()
    reference = tmp_path / "reference.wav"
    reference.write_bytes(wav(200))
    weights = [tmp_path / name for name in ("first.ckpt", "second.ckpt")]
    for path in weights:
        path.write_bytes(b"synthetic model marker")
    sovits = tmp_path / "voice.pth"
    sovits.write_bytes(b"synthetic sovits model marker")
    db = tmp_path / "voices.db"
    await init_db(db)
    async with get_db(db) as conn:
        profile = await crud.create_voice_profile(conn, VoiceProfileCreate(
            name="Voice", gpt_weights_path=str(weights[0]), sovits_weights_path=str(sovits),
            ref_audio_path=str(reference), prompt_text="Same transcript", prompt_lang="ja",
        ))
    scheduler = TtsScheduler()
    monkeypatch.setattr("galgame2voice.services.tts_service.get_tts_scheduler", lambda: scheduler)
    cache = TtsCacheManager(cache_dir=tmp_path / "audio/cache", db_path=str(db))
    client = SimpleNamespace(calls=0, revision=0, current_refer_audio=None,
                             current_gpt_weights=str(weights[0]), current_sovits_weights=str(sovits))
    payloads = [wav(200), wav(400)]

    async def synthesize(*args, **kwargs):
        client.calls += 1
        return payloads[client.revision]

    async def stream(*args, **kwargs):
        client.calls += 1
        yield payloads[client.revision]

    client.synthesize, client.stream_tts = synthesize, stream
    service = TtsService(client=client, cache_manager=cache, db_path=str(db), audio_dir=tmp_path / "audio")
    yield SimpleNamespace(root=tmp_path, reference=reference, weights=weights, db=db, profile=profile,
                          client=client, service=service, cache=cache, payloads=payloads, scheduler=scheduler)
    await scheduler.aclose()
    await cache.aclose()


async def render(service, mode, options):
    if mode == "bytes":
        return await service.synthesize("同じ台詞", options=options)
    if mode == "file":
        return (await service.synthesize_to_file("同じ台詞", options=options))[1].read_bytes()
    return b"".join([part async for part in service.stream_tts("同じ台詞", options=options)])


@pytest.mark.parametrize("leader_mode", ["bytes", "file"])
@pytest.mark.parametrize("cache_tier", ["memory", "disk"])
@pytest.mark.parametrize("use_cache, follower_text, interrupt", [
    (True, "同じ台詞", False), (False, "同じ台詞", False),
    (True, "別の台詞", False), (True, "同じ台詞", True),
])
async def test_queued_stream_reuses_only_matching_completed_audio(stack, monkeypatch, leader_mode, cache_tier, use_cache, follower_text, interrupt):
    """A preceding job can fill the cache after the stream's initial miss."""
    if cache_tier == "disk":
        # Keep the synthetic clip above the RAM budget, exercising real disk
        # reads and their cleanup rather than replacing the cache with a mock.
        stack.cache.max_mem_bytes = 1024
    started, release = asyncio.Event(), asyncio.Event()
    synthesize, put = stack.client.synthesize, stack.cache.put
    writes = []

    async def gated_synthesis(*args, **kwargs):
        started.set()
        await release.wait()
        return await synthesize(*args, **kwargs)

    async def record_write(*args, **kwargs):
        result = await put(*args, **kwargs)
        writes.append(result)
        return result

    monkeypatch.setattr(stack.client, "synthesize", gated_synthesis)
    monkeypatch.setattr(stack.cache, "put", record_write)
    options = {"voice_profile_id": stack.profile.id}
    leader = asyncio.create_task(render(stack.service, leader_mode, options))
    follower = None

    async def read_stream():
        chunks = []
        async with aclosing(stack.service.stream_tts(
            follower_text, options, chunk_size=1024, use_cache=use_cache,
        )) as stream:
            async for part in stream:
                chunks.append(part)
                if interrupt:
                    break
        return b"".join(chunks)

    async def next_job():
        return "worker available"

    async def wait_for_queue():
        while stack.scheduler.get_queue_depth() != 1:
            await asyncio.sleep(0)

    try:
        await asyncio.wait_for(started.wait(), 5)
        follower = asyncio.create_task(read_stream())
        await asyncio.wait_for(wait_for_queue(), 5)
        release.set()
        results = await asyncio.wait_for(asyncio.gather(leader, follower), 5)
        expected_audio = stack.payloads[0][:1024] if interrupt else stack.payloads[0]
        assert results == [stack.payloads[0], expected_audio]
        assert await asyncio.wait_for(stack.scheduler.schedule(next_job), 5) == "worker available"
        expected_calls = 1 if use_cache and follower_text == "同じ台詞" else 2
        assert stack.client.calls == expected_calls, "queued playback repeated an already completed inference"
        assert len(writes) == (expected_calls if use_cache else 1), "a cache replay must not rewrite the audio"
        stats = await stack.cache.get_stats()
        assert stats["total_hits"] == (1 if expected_calls == 1 else 0)
    finally:
        release.set()
        for task in (leader, follower):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in (leader, follower) if task is not None), return_exceptions=True)


@pytest.mark.parametrize("mode", ["bytes", "file", "stream"])
@pytest.mark.parametrize("full_reference", [False, True])
async def test_model_change_does_not_reuse_old_audio(stack, mode, full_reference):
    options = {"voice_profile_id": stack.profile.id}
    if full_reference:
        options.update(ref_audio_path=str(stack.reference), prompt_text="Same transcript", prompt_lang="ja")
    assert await render(stack.service, mode, options) == stack.payloads[0]
    assert await render(stack.service, mode, options) == stack.payloads[0]
    assert stack.client.calls == 1
    async with get_db(stack.db) as conn:
        await crud.update_voice_profile(conn, stack.profile.id, VoiceProfileUpdate(gpt_weights_path=str(stack.weights[1])))
    stack.client.current_gpt_weights = str(stack.weights[1])
    stack.client.revision = 1
    assert await render(stack.service, mode, options) == stack.payloads[1]
    assert stack.client.calls == 2


async def test_pre_resolved_same_path_model_replacement_changes_key(stack):
    options = {"_pre_resolved": True, "voice_profile_id": stack.profile.id,
               "gpt_weights_path": str(stack.weights[0]), "sovits_weights_path": stack.client.current_sovits_weights,
               "ref_audio_path": str(stack.reference), "prompt_text": "Same transcript", "prompt_lang": "ja"}
    assert await render(stack.service, "bytes", options) == stack.payloads[0]
    stack.weights[0].write_bytes(b"replaced synthetic model with a different revision")
    stack.client.revision = 1
    assert await render(stack.service, "bytes", options) == stack.payloads[1]
    assert stack.client.calls == 2


async def test_equal_reference_basenames_and_metadata_are_distinct(stack):
    other = stack.root / "other" / stack.reference.name
    other.parent.mkdir()
    other.write_bytes(wav(400))
    stamp = stack.reference.stat()
    os.utime(other, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    opts = {"ref_audio_path": str(stack.reference), "prompt_text": "same"}
    first = stack.cache.compute_cache_key("same", opts)[0]
    assert stack.cache.compute_cache_key("same", {**opts, "ref_audio_path": str(other)})[0] != first


async def test_reference_resolution_prefers_project_over_working_directory(stack, monkeypatch):
    (stack.root / "audio").mkdir(exist_ok=True)
    target = stack.root / "audio/ref.wav"
    target.write_bytes(wav(200))
    shortcut = stack.root / "shortcut"
    (shortcut / "audio").mkdir(parents=True)
    collision = shortcut / "audio/ref.wav"
    collision.write_bytes(wav(400))
    monkeypatch.chdir(shortcut)
    def key(path):
        return stack.cache.compute_cache_key("same", {"ref_audio_path": str(path)})[0]
    assert key("audio/ref.wav") == key(target)
    assert key("audio/ref.wav") != key(collision)


async def test_cache_keys_include_database_owner(stack):
    other = TtsCacheManager(cache_dir=stack.root / "other-cache", db_path=stack.root / "other.db")
    try:
        assert stack.cache.compute_cache_key("same")[0] != other.compute_cache_key("same")[0]
    finally:
        await other.aclose()


async def test_cache_factory_reuses_only_identical_database_and_directory(stack):
    first = get_tts_cache_manager(cache_dir=stack.root / "factory-a", db_path=stack.db)
    other = get_tts_cache_manager(cache_dir=stack.root / "factory-b", db_path=stack.root / "other.db")
    assert other is not first
    assert get_tts_cache_manager(cache_dir=stack.root / "factory-b", db_path=stack.root / "other.db") is other


class Engine:
    def __init__(self, audio):
        self.audio, self.posts = audio, 0

    async def handle_request(self, method, path, **kwargs):
        self.posts += method == "POST"
        return httpx.Response(200, content=self.audio)


@pytest.mark.parametrize("mode", ["bytes", "stream"])
async def test_known_engine_model_mismatch_never_synthesizes(stack, mode):
    engine = Engine(stack.payloads[1])
    client = GptSovitsClient(server=engine)
    client.current_gpt_weights = str(stack.weights[1])
    opts = {"gpt_weights_path": str(stack.weights[0]), "ref_audio_path": str(stack.reference),
            "prompt_text": "Same transcript", "prompt_lang": "ja"}
    with pytest.raises(ValueError, match="音色|模型"):
        if mode == "bytes":
            await client.synthesize("hello", options=opts)
        else:
            _ = [part async for part in client.stream_tts("hello", options=opts)]
    assert engine.posts == 0


async def test_reference_changed_after_resolution_never_reaches_engine(stack):
    options = await stack.service._populate_voice_profile_opts({"voice_profile_id": stack.profile.id})
    engine = Engine(stack.payloads[1])
    client = GptSovitsClient(server=engine)
    client.current_gpt_weights = str(stack.weights[0])
    stack.reference.write_bytes(wav(600) + b"changed revision")
    with pytest.raises(ValueError, match="文件|音色"):
        await client.synthesize("hello", options=options)
    assert engine.posts == 0


@pytest.mark.parametrize("mode", ["bytes", "stream"])
async def test_replaced_model_must_be_reloaded_before_synthesis(stack, mode):
    engine = Engine(stack.payloads[0])
    client = GptSovitsClient(server=engine)
    await client.set_gpt_weights(str(stack.weights[0]))
    stack.weights[0].write_bytes(b"model replaced after engine loaded it")
    opts = {"gpt_weights_path": str(stack.weights[0]), "ref_audio_path": str(stack.reference),
            "prompt_text": "Same transcript", "prompt_lang": "ja"}
    with pytest.raises(ValueError, match="模型.*更新|重新加载"):
        if mode == "bytes":
            await client.synthesize("hello", opts)
        else:
            _ = [part async for part in client.stream_tts("hello", opts)]
    assert engine.posts == 0


async def test_switch_reloads_same_path_when_model_file_changed(stack):
    class RecordingEngine(Engine):
        def __init__(self):
            super().__init__(stack.payloads[0])
            self.loads = []

        async def handle_request(self, method, path, **kwargs):
            if path in {"/set_gpt_weights", "/set_sovits_weights"}:
                self.loads.append(path)
            return await super().handle_request(method, path, **kwargs)

    engine = RecordingEngine()
    client = GptSovitsClient(server=engine)
    profile = {"id": 1, "name": "Voice", "gpt_weights_path": str(stack.weights[0]),
               "sovits_weights_path": str(stack.weights[1]), "ref_audio_path": str(stack.reference),
               "prompt_text": "Same transcript", "prompt_lang": "ja"}
    assert await client.switch_voice_profile(profile)
    assert await client.switch_voice_profile(profile)
    assert engine.loads.count("/set_gpt_weights") == 1
    stack.weights[0].write_bytes(b"replacement model revision")
    assert await client.switch_voice_profile(profile)
    assert engine.loads.count("/set_gpt_weights") == 2
    assert engine.loads.count("/set_sovits_weights") == 1


@pytest.mark.parametrize("mode", ["bytes", "stream"])
async def test_model_changed_while_waiting_for_inference_lock_is_rejected(stack, mode):
    engine = Engine(stack.payloads[0])
    client = GptSovitsClient(server=engine)
    client.current_gpt_weights = str(stack.weights[0])
    options = await stack.service._populate_voice_profile_opts({"voice_profile_id": stack.profile.id})
    # Exercise identity rejection for a caller that cannot prepare models.
    options.pop("_voice_preparation", None)
    async def request():
        if mode == "bytes":
            return await client.synthesize("hello", options)
        return [part async for part in client.stream_tts("hello", options)]

    await client.lock.acquire()
    task = asyncio.create_task(request())
    try:
        await asyncio.sleep(0)
        assert not task.done()
        client.current_gpt_weights = str(stack.weights[1])
    finally:
        client.lock.release()
    with pytest.raises(ValueError, match="模型"):
        await asyncio.wait_for(task, timeout=3)
    assert engine.posts == 0


async def test_http_stream_checks_model_before_post(stack):
    posts = []
    def handler(request):
        posts.append(request)
        return httpx.Response(200, content=stack.payloads[0])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as transport:
        client = GptSovitsClient(client=transport)
        client.current_gpt_weights = str(stack.weights[1])
        opts = {"gpt_weights_path": str(stack.weights[0]), "ref_audio_path": str(stack.reference),
                "prompt_text": "Same transcript", "prompt_lang": "ja"}
        with pytest.raises(ValueError, match="模型"):
            _ = [part async for part in client.stream_tts("hello", opts)]
        assert posts == []
        assert not client.lock.locked()
        assert client._inflight_requests == 0


async def test_engine_change_does_not_reuse_another_engines_audio(stack):
    stack.client.base_url = "http://engine-one.test"
    assert await render(stack.service, "bytes", {"_pre_resolved": True}) == stack.payloads[0]
    stack.client.base_url = "http://engine-two.test"
    stack.client.revision = 1
    assert await render(stack.service, "bytes", {"_pre_resolved": True}) == stack.payloads[1]
    assert stack.client.calls == 2


async def test_voice_manager_stream_uses_the_same_model_identity(stack):
    from galgame2voice.services.voice_manager import VoiceManager
    manager = VoiceManager(db_path=str(stack.db))
    manager.client, manager.tts_service = stack.client, stack.service
    options = {"voice_profile_id": stack.profile.id}
    assert b"".join([part async for part in manager.stream_tts("同じ台詞", options)]) == stack.payloads[0]
    async with get_db(stack.db) as conn:
        await crud.update_voice_profile(conn, stack.profile.id, VoiceProfileUpdate(gpt_weights_path=str(stack.weights[1])))
    stack.client.current_gpt_weights = str(stack.weights[1])
    stack.client.revision = 1
    assert b"".join([part async for part in manager.stream_tts("同じ台詞", options)]) == stack.payloads[1]
    assert stack.client.calls == 2


async def test_active_profile_is_not_reused_after_local_model_replacement(stack):
    from galgame2voice.services.voice_manager import VoiceManager
    client = GptSovitsClient(server=Engine(stack.payloads[0]))
    assert await client.switch_voice_profile(stack.profile)
    manager = VoiceManager(client, db_path=str(stack.db))
    assert manager.is_active_profile(stack.profile)
    stack.weights[0].write_bytes(b"new weights at same path")
    assert not manager.is_active_profile(stack.profile)


async def test_explicit_cache_scope_cannot_become_application_default(stack):
    from galgame2voice.database.session import get_database_path
    explicit = get_tts_cache_manager(cache_dir=stack.root / "custom", db_path=stack.db)
    default = get_tts_cache_manager()
    assert default is not explicit
    assert Path(default.db_path).resolve() == Path(get_database_path()).resolve()


async def test_failed_model_rollback_requires_reselection_before_synthesis(stack):
    class BrokenRollback(Engine):
        reject_old = False

        async def handle_request(self, method, path, **kwargs):
            weight = (kwargs.get("params") or {}).get("weights_path")
            if (path == "/set_sovits_weights" and weight == str(stack.weights[1])
                    or path == "/set_gpt_weights" and weight == str(stack.weights[0]) and self.reject_old):
                return httpx.Response(500, text="synthetic load failure")
            return await super().handle_request(method, path, **kwargs)

    engine = BrokenRollback(stack.payloads[0])
    client = GptSovitsClient(server=engine)
    original = {"id": 1, "name": "Voice", "gpt_weights_path": str(stack.weights[0]),
                "sovits_weights_path": str(stack.weights[0]), "ref_audio_path": str(stack.reference),
                "prompt_text": "Same transcript", "prompt_lang": "ja"}
    assert await client.switch_voice_profile(original)
    engine.reject_old = True
    assert not await client.switch_voice_profile({**original, "gpt_weights_path": str(stack.weights[1]),
                                                  "sovits_weights_path": str(stack.weights[1])})
    with pytest.raises(ValueError, match="模型|音色"):
        await client.synthesize("hello", {"ref_audio_path": str(stack.reference), "prompt_text": "same"})
    assert engine.posts == 0
    engine.reject_old = False
    assert await client.switch_voice_profile(original)
    assert await client.synthesize("hello", {"ref_audio_path": str(stack.reference)}) == stack.payloads[0]


@pytest.mark.parametrize("operation", ["switch", "setter"])
async def test_cancelled_model_load_cannot_synthesize_until_reselected(stack, operation):
    started, release = asyncio.Event(), asyncio.Event()
    class SlowLoad(Engine):
        block = False

        async def handle_request(self, method, path, **kwargs):
            if self.block and path == "/set_gpt_weights":
                started.set()
                await release.wait()
            return await super().handle_request(method, path, **kwargs)

    engine = SlowLoad(stack.payloads[0])
    client = GptSovitsClient(server=engine)
    original = {"id": 1, "name": "Voice", "gpt_weights_path": str(stack.weights[0]),
                "sovits_weights_path": str(stack.weights[0]), "ref_audio_path": str(stack.reference),
                "prompt_text": "Same transcript", "prompt_lang": "ja"}
    assert await client.switch_voice_profile(original)
    engine.block = True
    load = (client.switch_voice_profile({**original, "gpt_weights_path": str(stack.weights[1])})
            if operation == "switch" else client.set_gpt_weights(str(stack.weights[1])))
    task = asyncio.create_task(load)
    await asyncio.wait_for(started.wait(), 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(ValueError, match="模型|音色"):
        await client.synthesize("hello", {"ref_audio_path": str(stack.reference)})
    assert engine.posts == 0
    assert not client.lock.locked()
    engine.block = False
    assert await client.switch_voice_profile(original)
    assert await client.synthesize("hello", {"ref_audio_path": str(stack.reference)}) == stack.payloads[0]


async def test_chat_service_constructs_tts_for_its_database(stack):
    from galgame2voice.services.chat_service import ChatService
    chat = ChatService(db_path=str(stack.db))
    assert chat.tts_service.db_path == str(stack.db)
    assert Path(chat.tts_service.cache_manager.db_path).resolve() == stack.db.resolve()


async def test_individual_model_load_invalidates_active_profile_match(stack):
    from galgame2voice.services.voice_manager import VoiceManager
    client = GptSovitsClient(server=Engine(stack.payloads[0]))
    assert await client.switch_voice_profile(stack.profile)
    manager = VoiceManager(client, db_path=str(stack.db))
    assert manager.is_active_profile(stack.profile)
    assert await client.set_gpt_weights(str(stack.weights[1]))
    assert not manager.is_active_profile(stack.profile)


@pytest.mark.parametrize("operation", ["switch", "setter"])
async def test_old_engine_acknowledgement_cannot_mark_new_endpoint_loaded(stack, operation):
    started, release = asyncio.Event(), asyncio.Event()
    class SlowEngine(Engine):
        block = False

        async def handle_request(self, method, path, **kwargs):
            if self.block and path == "/set_gpt_weights":
                started.set()
                await release.wait()
            return await super().handle_request(method, path, **kwargs)

    engine = SlowEngine(stack.payloads[0])
    client = GptSovitsClient(server=engine)
    original = {"id": 1, "name": "Voice", "gpt_weights_path": str(stack.weights[0]),
                "sovits_weights_path": str(stack.weights[0]), "ref_audio_path": str(stack.reference),
                "prompt_text": "Same transcript", "prompt_lang": "ja"}
    assert await client.switch_voice_profile(original)
    engine.block = True
    load = (client.switch_voice_profile({**original, "gpt_weights_path": str(stack.weights[1])})
            if operation == "switch" else client.set_gpt_weights(str(stack.weights[1])))
    task = asyncio.create_task(load)
    await asyncio.wait_for(started.wait(), 3)
    await client.set_base_url("http://different-engine.test:9880")
    release.set()
    result = await asyncio.gather(task, return_exceptions=True)
    assert result[0] is False or isinstance(result[0], ValueError)
    assert client.current_gpt_weights is None
    with pytest.raises(ValueError, match="模型|音色"):
        await client.synthesize("hello", {"ref_audio_path": str(stack.reference)})
    assert engine.posts == 0
    engine.block = False
    assert await client.switch_voice_profile(original)
    assert await client.synthesize("hello", {"ref_audio_path": str(stack.reference)}) == stack.payloads[0]
