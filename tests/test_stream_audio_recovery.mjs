import assert from 'node:assert/strict';
import { test, afterEach } from 'node:test';
import { StreamAudioController } from '../frontend/src/audio_player.js';
import { clearMemAudioCache, loadAudioBlob, putCachedAudioBlob } from '../frontend/src/cache.js';
import { streamChat } from '../frontend/src/ai.js';

const tick = (ms = 0) => new Promise(resolve => setTimeout(resolve, ms));
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return { promise, resolve }; };
const blob = () => new Blob([new Uint8Array([1, 2, 3])], { type: 'audio/wav' });
class AudioContextMock {
  state = 'running'; currentTime = 0; destination = {}; sources = []; resumes = 0;
  createGain() { return { gain: { value: 1, setValueAtTime() {}, cancelScheduledValues() {}, linearRampToValueAtTime() {} }, connect() {}, disconnect() {} }; }
  createBufferSource() {
    const source = { connect() {}, disconnect() {}, start(time) { this.time = time; }, stop() {}, finish() { this.onended?.(); } };
    this.sources.push(source); return source;
  }
  async decodeAudioData() { return { duration: 1 }; }
  async suspend() { this.state = 'suspended'; }
  async resume() { this.resumes++; this.state = 'running'; }
  async close() { this.state = 'closed'; }
}
const players = [];
function player(options = {}) {
  globalThis.window = { AudioContext: AudioContextMock };
  globalThis.fetch = async () => new Response(blob());
  const p = new StreamAudioController({ fetchTimeoutMs: 40, ...options });
  players.push(p); p.startSession('message'); return p;
}
afterEach(async () => {
  for (const p of players.splice(0)) await p.close();
  clearMemAudioCache(); delete globalThis.caches;
});

for (const stage of ['headers', 'body', 'decode']) {
  test(`a stalled ${stage} skips the failed head and plays the next chunk`, async () => {
    const errors = [];
    const p = player({ onError: error => errors.push(error) });
    let signal;
    if (stage === 'decode') {
      let calls = 0; p.ctx.decodeAudioData = async () => ++calls === 1 ? new Promise(() => {}) : { duration: 1 };
    } else {
      globalThis.fetch = async (url, options) => {
        if (url === '/bad.wav') {
          signal = options.signal;
          return stage === 'headers' ? new Promise(() => {}) : { ok: true, blob: () => new Promise(() => {}) };
        }
        return new Response(blob());
      };
    }
    const failed = p.enqueueChunk({ url: '/bad.wav', index: 0 });
    await tick();
    await p.enqueueChunk({ url: '/good.wav', index: 1 });
    assert.equal(p.activeSources.length, 0);
    await failed;
    assert.equal(p.activeSources.length, 1);
    assert.equal(p.activeSources[0].item.index, 1);
    assert.equal(errors.length, 1);
    if (signal) assert.equal(signal.aborted, true);
  });
}
test('repeated audio URLs play at each sentence position; duplicate events do not replay', async () => {
  const p = player();
  await p.enqueueChunk({ url: '/repeat.wav', index: 0 });
  await p.enqueueChunk({ url: '/repeat.wav', index: 1 });
  assert.equal(p.activeSources.length, 2);
  await p.enqueueChunk({ url: '/repeat.wav', index: 1 });
  p.ctx.sources.forEach(source => source.finish());
  await p.enqueueChunk({ url: '/repeat.wav', index: 0 });
  assert.equal(p.ctx.sources.length, 2);
});
test('interrupt cancels body reads and releases callers even if fetch ignores abort', async () => {
  const p = player(); let signal;
  globalThis.fetch = async (_url, options) => { signal = options.signal; return { ok: true, blob: () => new Promise(() => {}) }; };
  const pending = p.enqueueChunk({ url: '/slow.wav', index: 0 });
  await tick(); p.interrupt(0); await pending;
  assert.equal(signal.aborted, true); assert.equal(p.queue.length, 0);
});
test('a late cache read cannot start a fetch in the next session', async () => {
  const p = player(); const opened = deferred(); let requests = 0;
  window.caches = globalThis.caches = { open: () => opened.promise };
  globalThis.fetch = async () => { requests++; return new Response(blob()); };
  const pending = p.enqueueChunk({ url: '/old.wav', index: 0 });
  await tick(); p.startSession('new'); await pending;
  opened.resolve({ match: async () => null }); await tick();
  assert.equal(requests, 0); assert.equal(p.currentMsgId, 'new');
});
test('new chunks do not resume a paused context; portraits follow audio time', async () => {
  const starts = [], speaking = [];
  const p = player({ onChunkStart: item => starts.push(item.index), onStatusChange: value => speaking.push(value) });
  await p.enqueueChunk({ url: '/one.wav', index: 0 });
  await p.enqueueChunk({ url: '/two.wav', index: 1 });
  p.ctx.currentTime = 0.1; await tick(55);
  assert.deepEqual(starts, [0]);
  p.pause();
  await p.enqueueChunk({ url: '/three.wav', index: 2 });
  assert.equal(p.ctx.resumes, 0); assert.equal(p.paused, true);
  p.ctx.currentTime = 1.1; await tick(55);
  assert.deepEqual(starts, [0]); assert.equal(speaking.at(-1), false);
  p.resume(); await tick(55);
  assert.deepEqual(starts, [0, 1]); assert.equal(speaking.at(-1), true);
});
test('late onended from an interrupted source cannot finish the next session', async () => {
  let endings = 0; const p = player({ onQueueEmpty: () => endings++ });
  await p.enqueueChunk({ url: '/old.wav', index: 0 }); const old = p.ctx.sources[0];
  p.startSession('new'); old.finish();
  assert.equal(endings, 0); assert.equal(p.isPlaying, true);
});
test('stream gaps retain pause and playback ownership until input ends', async () => {
  let endings = 0; const p = player({ onQueueEmpty: () => endings++ });
  p.startSession('stream', null, { streaming: true });
  await p.enqueueChunk({ url: '/first.wav', index: 0 });
  p.ctx.sources[0].finish(); p.pause();
  assert.equal(p.isPlaying, true); assert.equal(endings, 0);
  await p.enqueueChunk({ url: '/next.wav', index: 1 });
  p.finishInput(); assert.equal(p.paused, true);
  p.resume(); await tick(); p.ctx.sources[1].finish();
  assert.equal(endings, 1); assert.equal(p.isPlaying, false);
});
test('cache storage failures cannot hang playback or caching', async () => {
  player(); const original = globalThis.setTimeout;
  globalThis.setTimeout = (fn, ms, ...args) => original(fn, ms === 1500 ? 10 : ms, ...args);
  try {
    window.caches = globalThis.caches = { open: () => new Promise(() => {}) };
    assert.ok((await loadAudioBlob('/fallback.wav', { timeoutMs: 100 })).size > 0);
    await putCachedAudioBlob('recording', blob());
  } finally { globalThis.setTimeout = original; }
});
test('blocked autoplay becomes an actionable paused state and can resume', async () => {
  const errors = []; const p = player({ onError: error => errors.push(error) });
  p.ctx.state = 'suspended'; p.ctx.resume = async () => { throw new Error('gesture required'); };
  await p.enqueueChunk({ url: '/autoplay.wav', index: 0 }); await tick();
  assert.equal(p.paused, true); assert.match(errors[0].message, /LOG 里再点一次「语音」/);
  p.ctx.resume = AudioContextMock.prototype.resume;
  p.resume(); await tick(); assert.equal(p.activeSources.length, 1);
});
test('stream HTTP errors cannot hang forever while reading their error body', async () => {
  let signal;
  globalThis.fetch = async (_url, options) => {
    signal = options.signal;
    return { ok: false, status: 500, json: () => new Promise(() => {}) };
  };
  const result = await new Promise(resolve => streamChat({ prompt: 'test', idleTimeoutMs: 10,
    onChunk() {}, onEnd: (_text, meta) => resolve(meta) }));
  assert.match(result.error, /超时/); assert.equal(signal.aborted, true);
});
test('final stream metadata retains repeated sentence URLs in order', async () => {
  const events = 'event: audio_chunk\ndata: {"audio_url":"/repeat.wav","sentence":"はい"}\n\n'
    + 'event: done\ndata: {"chinese":"好的好的","chunks":[{"audio_url":"/repeat.wav","sentence":"はい"},{"audio_url":"/repeat.wav","sentence":"はい"}]}\n\n';
  globalThis.fetch = async () => new Response(events);
  const result = await new Promise(resolve => streamChat({ prompt: 'test', onChunk() {},
    onEnd: (_text, meta) => resolve(meta) }));
  assert.deepEqual(result.audioUrls, ['/repeat.wav', '/repeat.wav']);
  assert.equal(result.chunks.length, 2);
});
test('the saved reply replaces streamed text the server did not keep', async () => {
  const events = 'event: text\ndata: {"delta_chinese":"好的，我会按照格式回复：\\n"}\n\n'
    + 'event: text\ndata: {"delta_chinese":"你好呀"}\n\n'
    + 'event: done\ndata: {"chinese":"你好呀","chunks":[]}\n\n';
  globalThis.fetch = async () => new Response(events);
  const shown = [];
  const text = await new Promise(resolve => streamChat({ prompt: 'test', onChunk: full => shown.push(full),
    onEnd: finalText => resolve(finalText) }));
  assert.equal(text, '你好呀');
  assert.equal(shown.at(-1), '你好呀');
});

test('continuous multi-character session switching during long chunk streams isolates queues and adaptive buffer lead', async () => {
  const startedChunks = [];
  const p = player({ onChunkStart: item => startedChunks.push(`${item.sessionId}:${item.index}:${item.emotion}`) });
  const emotions = ['gentle', 'happy', 'shy', 'tsundere', 'cool', 'sad', 'angry'];

  for (let sessionRound = 1; sessionRound <= 8; sessionRound++) {
    p.startSession(`msg_${sessionRound}`, null, { streaming: true });
    for (let idx = 0; idx < 6; idx++) {
      const em = emotions[(sessionRound + idx) % emotions.length];
      await p.enqueueChunk({
        url: `/char_${sessionRound}/chunk_${idx}.wav`,
        index: idx,
        sentence: `台词 ${sessionRound}-${idx}`,
        emotion: em,
        totalExpected: 6,
      });
    }
    p.finishInput();
    assert.equal(p.activeSources.length, 6);
    assert.ok(p.activeSources.every(s => s.item.sessionId === p.currentSessionId));
    // Simulate completing playback of first two chunks before switching character/session
    p.ctx.sources.slice(-6, -4).forEach(s => s.finish());
  }

  // Finish remaining chunks of the final session
  p.ctx.sources.slice(-4).forEach(s => s.finish());
  assert.equal(p.isPlaying, false);
  assert.equal(p.activeSources.length, 0);
  assert.ok(p.getStats().chunksPlayed >= 48);
});
