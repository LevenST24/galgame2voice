import assert from 'node:assert/strict';
import { test, beforeEach, afterEach } from 'node:test';
import { StreamAudioController } from '../frontend/src/audio_player.js';
import { clearMemAudioCache, getCachedAudioBlob, loadAudioBlob, putCachedAudioBlob, pruneAudioCacheStorage } from '../frontend/src/cache.js';

const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};
const tick = () => new Promise(resolve => setTimeout(resolve, 0));
const audio = text => new Blob([text], { type: 'audio/wav' });
const bad = () => new DOMException('synthetic invalid audio', 'EncodingError');
class AudioContextMock {
  state = 'running'; currentTime = 0; destination = {}; sources = []; decoded = [];
  createGain() { return { gain: { value: 1, setValueAtTime() {}, cancelScheduledValues() {}, linearRampToValueAtTime() {} }, connect() {}, disconnect() {} }; }
  createBufferSource() {
    const source = { connect() {}, disconnect() {}, start() {}, stop() {}, finish() { this.onended?.(); } };
    this.sources.push(source); this.onSource?.(); return source;
  }
  async decodeAudioData(buffer) {
    const text = new TextDecoder().decode(buffer);
    this.decoded.push(text);
    if (!text.startsWith('valid')) throw bad();
    return { duration: 1, text };
  }
  async suspend() { this.state = 'suspended'; }
  async resume() { this.state = 'running'; }
  async close() { this.state = 'closed'; }
}

let storage, fetched;
const players = [];
beforeEach(() => {
  clearMemAudioCache();
  const entries = new Map();
  storage = {
    entries,
    async match(req) { return entries.get(req.url)?.clone(); },
    async put(req, response) { entries.set(req.url, response.clone()); },
    async delete(req) { return entries.delete(req.url); },
    async keys() { return [...entries.keys()].map(key => new Request(key)); },
  };
  globalThis.caches = { open: async () => storage };
  globalThis.window = { AudioContext: AudioContextMock, caches: globalThis.caches };
  fetched = [];
  globalThis.fetch = async (url, options) => {
    fetched.push({ url, options }); return new Response(audio('valid fresh audio'));
  };
});
afterEach(async () => {
  for (const player of players.splice(0)) await player.close();
  clearMemAudioCache(); delete globalThis.caches; delete globalThis.window;
});
function player(options = {}) {
  const result = new StreamAudioController({ fetchTimeoutMs: 500, ...options });
  result.startSession('cache-recovery'); players.push(result); return result;
}
async function prepareLine(p, item) {
  const source = deferred(); p.ctx.onSource = source.resolve;
  const result = p.playLine(0, [item]);
  await Promise.race([result, source.promise]);
  return { result };
}

for (const mode of ['line', 'stream']) {
  for (const tier of ['memory', 'disk', 'provided-blob', 'legacy-disk']) {
    test(`${mode} recovers one corrupt ${tier} copy without deleting a recording`, async () => {
      const url = '/audio/cache/recover.wav';
      const broken = audio('broken cached audio');
      await putCachedAudioBlob(url, broken);
      await putCachedAudioBlob('user_recording', audio('original microphone recording'));
      if (tier === 'legacy-disk') {
        const [request] = await storage.keys();
        await storage.put(request, new Response(broken, { headers: { 'Content-Type': 'audio/wav', 'X-Cached-At': '1700000000000' } }));
      }
      if (tier === 'disk' || tier === 'legacy-disk') clearMemAudioCache();
      const p = player();
      const item = { url, ...(tier === 'provided-blob' ? { blob: broken } : {}) };
      let pending;
      if (mode === 'line') pending = (await prepareLine(p, item)).result;
      else await p.enqueueChunk({ ...item, index: 0 });
      assert.equal(p.ctx.sources.length, 1, 'a stale cached Blob prevented a fresh download from playing');
      assert.deepEqual(p.ctx.decoded, ['broken cached audio', 'valid fresh audio']);
      assert.equal(fetched.length, 1);
      assert.equal(fetched[0].url, url);
      assert.equal(fetched[0].options.cache, 'no-store');
      assert.equal(fetched[0].options.method ?? 'GET', 'GET');
      p.ctx.sources.forEach(source => source.finish());
      if (pending) assert.equal(await pending, 'ended');
      await tick(); clearMemAudioCache();
      assert.equal(await (await getCachedAudioBlob(url)).text(), 'valid fresh audio');
      assert.equal(await (await getCachedAudioBlob('user_recording')).text(), 'original microphone recording');
    });
  }
}

test('a second decode failure stops recovery and discards only the broken URL', async () => {
  const url = '/audio/cache/still-broken.wav';
  await putCachedAudioBlob(url, audio('old corrupt audio'));
  await putCachedAudioBlob('user_recording', audio('keep recording'));
  globalThis.fetch = async (requested, options) => {
    fetched.push({ url: requested, options }); return new Response(audio('new corrupt audio'));
  };
  const errors = []; const p = player({ onError: error => errors.push(error) });
  assert.equal(await p.playLine(0, [url]), 'failed');
  assert.equal(fetched.length, 1, 'a decode failure caused repeated downloads');
  assert.equal(p.ctx.sources.length, 0);
  assert.equal(errors.length, 1);
  assert.equal(errors[0].code, 'AUDIO_DECODE_FAILED');
  assert.match(errors[0].message, /语音.*无法解码.*文字聊天/);
  assert.equal(await getCachedAudioBlob(url), null);
  assert.equal(await (await getCachedAudioBlob('user_recording')).text(), 'keep recording');
});

test('stopping while the fresh download waits prevents late playback and recaching', async () => {
  const url = '/audio/cache/cancel-recovery.wav';
  await putCachedAudioBlob(url, audio('broken cached audio'));
  const requested = deferred(), response = deferred();
  globalThis.fetch = async (value, options) => {
    fetched.push({ url: value, options }); requested.resolve(); return response.promise;
  };
  const p = player(); const pending = p.playLine(0, [url]);
  await Promise.race([requested.promise, pending]);
  assert.equal(fetched.length, 1);
  p.stopLine({ fadeMs: 0 });
  assert.equal(await pending, 'interrupted');
  assert.equal(fetched[0].options.signal.aborted, true);
  response.resolve(new Response(audio('valid late audio')));
  await tick();
  assert.equal(p.ctx.sources.length, 0);
  assert.equal(await getCachedAudioBlob(url), null);
});

test('a late failed decode uses a newer valid copy without deleting or rewriting it', async () => {
  const url = '/audio/cache/newer-valid.wav';
  await putCachedAudioBlob(url, audio('broken old audio'));
  const p = player(), decoding = deferred(), finishOld = deferred();
  const decode = p.ctx.decodeAudioData.bind(p.ctx);
  p.ctx.decodeAudioData = async buffer => {
    if (new TextDecoder().decode(buffer).startsWith('broken')) {
      decoding.resolve(); await finishOld.promise;
    }
    return decode(buffer);
  };
  const pending = p.playLine(0, [url]); await decoding.promise;
  await putCachedAudioBlob(url, audio('valid newer audio'));
  const ready = deferred(); p.ctx.onSource = ready.resolve;
  finishOld.reject(bad());
  await Promise.race([pending, ready.promise]);
  assert.equal(p.ctx.sources.length, 1);
  assert.equal(p.ctx.sources[0].buffer.text, 'valid newer audio');
  assert.equal(fetched.length, 0);
  p.ctx.sources[0].finish(); assert.equal(await pending, 'ended');
  await tick(); clearMemAudioCache();
  assert.equal(await (await getCachedAudioBlob(url)).text(), 'valid newer audio');
});

test('a delayed invalid storage write cannot replace the repaired copy', async () => {
  const url = '/audio/cache/late-write.wav';
  const writeStarted = deferred(), releaseWrite = deferred();
  const put = storage.put.bind(storage);
  let first = true;
  storage.put = async (req, response) => {
    if (first) { first = false; writeStarted.resolve(); await releaseWrite.promise; }
    await put(req, response);
  };
  const oldWrite = putCachedAudioBlob(url, audio('broken cached audio'));
  await writeStarted.promise;
  const p = player(); const { result } = await prepareLine(p, url);
  assert.equal(p.ctx.sources.length, 1, 'playback waited for the stalled cache writer');
  p.ctx.sources[0].finish(); assert.equal(await result, 'ended');
  releaseWrite.resolve(); await oldWrite; await tick(); clearMemAudioCache();
  assert.equal(await (await getCachedAudioBlob(url)).text(), 'valid fresh audio');
});

test('a late storage read cannot restore a copy replaced during its body read', async () => {
  const url = '/audio/cache/late-read.wav';
  await putCachedAudioBlob(url, audio('broken old audio')); clearMemAudioCache();
  const reading = deferred(), releaseRead = deferred();
  const match = storage.match.bind(storage);
  storage.match = async req => {
    const response = await match(req);
    return { ok: true, headers: response.headers, blob: async () => {
      reading.resolve(); await releaseRead.promise; return response.blob();
    } };
  };
  const old = getCachedAudioBlob(url); await reading.promise;
  await putCachedAudioBlob(url, audio('valid new audio'));
  releaseRead.resolve();
  assert.equal(await (await old).text(), 'valid new audio');
  assert.equal(await (await getCachedAudioBlob(url)).text(), 'valid new audio');
});

test('a late prefetch response cannot overwrite audio repaired by a newer playback', async () => {
  const url = '/audio/cache/late-prefetch.wav';
  const prefetchStarted = deferred(), oldResponse = deferred();
  globalThis.fetch = async (value, options) => {
    fetched.push({ url: value, options });
    if (fetched.length === 1) { prefetchStarted.resolve(); return oldResponse.promise; }
    return new Response(audio('valid fresh audio'));
  };
  const prefetch = loadAudioBlob(url); await prefetchStarted.promise;
  await putCachedAudioBlob(url, audio('broken cached audio'));
  const p = player(); const { result } = await prepareLine(p, url);
  assert.equal(p.ctx.sources.length, 1);
  p.ctx.sources[0].finish(); assert.equal(await result, 'ended');
  oldResponse.resolve(new Response(audio('broken prefetched audio')));
  await prefetch; await tick(); clearMemAudioCache();
  assert.equal(await (await getCachedAudioBlob(url)).text(), 'valid fresh audio');
  assert.equal(fetched.length, 2);
});

test('cancelling before a decode failure arrives preserves the cached recording', async () => {
  const key = 'user_recording';
  const original = audio('original recording'); await putCachedAudioBlob(key, original);
  const p = player(), decoding = deferred(), outcome = deferred();
  p.ctx.decodeAudioData = async () => { decoding.resolve(); return outcome.promise; };
  const pending = p.playLine(0, [{ url: key, blob: original }]); await decoding.promise;
  p.stopLine({ fadeMs: 0 }); assert.equal(await pending, 'interrupted');
  outcome.reject(bad()); await tick();
  assert.equal(await (await getCachedAudioBlob(key)).text(), 'original recording');
  assert.equal(fetched.length, 0);
});

test('the original deadline includes the failed decode and the recovery decode', async t => {
  const url = '/audio/cache/one-deadline.wav';
  await putCachedAudioBlob(url, audio('broken cached audio'));
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const p = player(), firstStarted = deferred(), first = deferred(), secondStarted = deferred(), second = deferred();
  let attempts = 0;
  p.ctx.decodeAudioData = async () => {
    if (++attempts === 1) { firstStarted.resolve(); return first.promise; }
    secondStarted.resolve(); return second.promise;
  };
  const pending = p.playLine(0, [url]); await firstStarted.promise;
  t.mock.timers.tick(400); first.reject(bad()); await secondStarted.promise;
  t.mock.timers.tick(100);
  assert.equal(await pending, 'timeout');
  assert.equal(fetched.length, 1);
  assert.equal(p._linePlayState, null, 'the recovery must finish within the original 500ms budget');
  second.resolve({ duration: 1 });
  await Promise.resolve(); await Promise.resolve();
  assert.equal(p.ctx.sources.length, 0);
  assert.equal(await getCachedAudioBlob(url), null);
});

test('a healthy burst persists every recording instead of silently dropping busy keys', async () => {
  const keys = Array.from({ length: 40 }, (_, i) => `user_recording_${i}`);
  await Promise.all(keys.map(key => putCachedAudioBlob(key, audio(`recording ${key}`))));
  clearMemAudioCache();
  for (const key of keys) assert.equal(await (await getCachedAudioBlob(key))?.text(), `recording ${key}`);
  assert.equal(fetched.length, 0);
});

test('automatic cache pruning preserves user recordings and unknown keys', async () => {
  await putCachedAudioBlob('user_recording_old', audio('irreplaceable user recording'));
  await putCachedAudioBlob('legacy_unknown_key', audio('unknown ownership'));
  await putCachedAudioBlob('/audio/cache/old.wav', audio('old generated audio'));
  await putCachedAudioBlob('msg_generated', audio('generated audio'));
  await putCachedAudioBlob('/audio/cache/new.wav', audio('new generated audio'));
  await pruneAudioCacheStorage(2);
  clearMemAudioCache();
  assert.equal(await (await getCachedAudioBlob('user_recording_old'))?.text(), 'irreplaceable user recording');
  assert.equal(await (await getCachedAudioBlob('legacy_unknown_key'))?.text(), 'unknown ownership');
  assert.equal(await getCachedAudioBlob('/audio/cache/old.wav'), null);
  assert.equal(await (await getCachedAudioBlob('msg_generated'))?.text(), 'generated audio');
  assert.equal(await (await getCachedAudioBlob('/audio/cache/new.wav'))?.text(), 'new generated audio');
});

test('a valid cached playback does not fetch or rewrite storage', async () => {
  const url = '/audio/cache/healthy.wav';
  await putCachedAudioBlob(url, audio('valid cached audio'));
  let writes = 0; const put = storage.put.bind(storage);
  storage.put = async (...args) => { writes++; return put(...args); };
  const p = player(); const { result } = await prepareLine(p, url);
  assert.equal(p.ctx.sources.length, 1);
  p.ctx.sources[0].finish(); assert.equal(await result, 'ended');
  await tick(); assert.equal(fetched.length, 0); assert.equal(writes, 0);
});
