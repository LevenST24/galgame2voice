import assert from 'node:assert/strict';
import { test, afterEach } from 'node:test';
import { StreamAudioController, streamAudioController } from '../frontend/src/audio_player.js';
import { initAudioPlayerUi, playAiVoice } from '../frontend/src/controllers/audio_player_ui.js';
import { createBacklog } from '../frontend/src/adv/backlog.js';
import { clearMemAudioCache, loadAudioBlob, putCachedAudioBlob } from '../frontend/src/cache.js';
import { streamChat } from '../frontend/src/ai.js';
import { fakeDocument } from './helpers/adv_dom.mjs';

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

test('line start notification waits for audio data and fires once for the first scheduled source', async () => {
  const p = player({ fetchTimeoutMs: 500 });
  const input = deferred();
  const started = deferred();
  let starts = 0;
  globalThis.fetch = async () => { await input.promise; return new Response(blob()); };
  const pending = p.playLine(0, ['/notify-0.wav', '/notify-1.wav'], { onStart: () => { starts++; started.resolve(); } });
  await tick();
  assert.equal(starts, 0);
  input.resolve();
  await tick();
  p.ctx.currentTime = 1;
  let deadline;
  try { await Promise.race([started.promise, new Promise((_, reject) => {
    deadline = setTimeout(() => reject(new Error('No line start notification')), 1000);
  })]); } finally { clearTimeout(deadline); }
  assert.equal(starts, 1);
  assert.equal(p.ctx.sources.length, 2);
  p.ctx.sources.forEach(source => source.finish());
  assert.equal(await pending, 'ended');
});

for (const state of ['running', 'suspended', 'interrupted']) {
  test(`line text waits for the audio clock while its context is ${state}`, async () => {
    const p = player({ fetchTimeoutMs: 500 });
    const started = deferred();
    let starts = 0;
    const pending = p.playLine(0, ['/clock-gated.wav'], { onStart: () => { starts++; started.resolve(); } });
    await tick();
    assert.equal(p.ctx.sources.length, 1);
    p.ctx.state = state;
    await tick(50);
    assert.equal(starts, 0, 'elapsed wall time is not evidence that sound has started');
    p.ctx.state = 'running';
    p.ctx.currentTime = p.ctx.sources[0].time / 2;
    await tick(30);
    assert.equal(starts, 0, 'resuming before the scheduled audio time cannot release text');
    p.ctx.currentTime = p.ctx.sources[0].time;
    let deadline;
    try {
      await Promise.race([started.promise, new Promise((_, reject) => {
        deadline = setTimeout(() => reject(new Error('Audio clock reached playback without a start notification')), 1000);
      })]);
    } finally { clearTimeout(deadline); }
    assert.equal(starts, 1);
    p.ctx.sources[0].finish();
    assert.equal(await pending, 'ended');
  });
}

test('interrupt cancels a line start notification that has not reached its scheduled time', async () => {
  const p = player(); p.currentBufferLeadSec = 0.12;
  let starts = 0;
  const pending = p.playLine(0, ['/notify-interrupted.wav'], { onStart: () => { starts++; } });
  await tick(5);
  assert.equal(p.ctx.sources.length, 1);
  p.stopLine({ fadeMs: 0 });
  assert.equal(await pending, 'interrupted');
  await tick(140);
  assert.equal(starts, 0);
});

for (const action of ['stopLine', 'interrupt', 'startSession', 'close']) {
  test(`${action} cancels line preparation before AudioContext resume completes`, async () => {
    const p = player({ fetchTimeoutMs: 500 });
    const context = p.ctx;
    const resumed = deferred();
    context.state = 'suspended';
    context.resume = async () => { await resumed.promise; if (context.state !== 'closed') context.state = 'running'; };
    const fetched = [];
    globalThis.fetch = async url => { fetched.push(url); return new Response(blob()); };
    const pending = p.playLine(0, ['/old-unlock.wav']);
    await tick();
    if (action === 'startSession') p.startSession('next-message');
    else if (action === 'stopLine') p.stopLine({ fadeMs: 0 });
    else if (action === 'interrupt') p.interrupt(0);
    else await p.close();
    resumed.resolve(); await tick(5);
    context.sources.forEach(source => source.finish());
    assert.equal(await pending, 'interrupted');
    assert.deepEqual(fetched, [], 'a cancelled preparation must not start downloading after resume');
    assert.equal(context.sources.length, 0);
  });
}

test('a newer line wins when AudioContext resume calls complete out of order', async () => {
  const p = player({ fetchTimeoutMs: 500 });
  const firstResume = deferred(), nextResume = deferred();
  const context = p.ctx; context.state = 'suspended';
  let resumes = 0;
  context.resume = async () => {
    await (++resumes === 1 ? firstResume.promise : nextResume.promise);
    context.state = 'running';
  };
  const fetched = [];
  globalThis.fetch = async url => { fetched.push(url); return new Response(blob()); };
  const old = p.playLine(0, ['/old-unlock.wav']);
  const current = p.playLine(1, ['/current-unlock.wav']);
  nextResume.resolve(); await tick(5);
  firstResume.resolve(); await tick(5);
  context.sources.forEach(source => source.finish());
  assert.equal(await old, 'interrupted');
  assert.equal(await current, 'ended');
  assert.deepEqual(fetched, ['/current-unlock.wav']);
  assert.equal(resumes, 2, 'each owned preparation makes one bounded resume attempt');
});

test('stopping a line releases its caller even when AudioContext resume never resolves', async () => {
  const p = player(); p.ctx.state = 'suspended';
  p.ctx.resume = () => new Promise(() => {});
  let outcome;
  const pending = p.playLine(0, ['/never-unlocked.wav']).then(value => { outcome = value; });
  p.stopLine({ fadeMs: 0 }); await tick();
  assert.equal(outcome, 'interrupted');
  await pending;
  assert.equal(p._linePlayState, null);
  assert.equal(p.ctx.sources.length, 0);
});

test('a refused line playback reports a browser recovery hint without fetching audio', async () => {
  const errors = [], fetched = [];
  const p = player({ onError: error => errors.push(error) }); p.ctx.state = 'suspended';
  p.ctx.resume = async () => { throw new Error('gesture required'); };
  globalThis.fetch = async url => { fetched.push(url); return new Response(blob()); };
  assert.equal(await p.playLine(0, ['/blocked-line.wav']), 'blocked');
  assert.equal(p._linePlayState, null);
  assert.equal(errors.length, 1);
  assert.match(errors[0].message, /浏览器.*LOG.*语音/);
  assert.deepEqual(fetched, []);
});

test('an unresolved line resume reaches its deadline and clears playback ownership', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const p = player(); p.ctx.state = 'suspended';
  p.ctx.resume = () => new Promise(() => {});
  const pending = p.playLine(0, ['/never-unlocked.wav']);
  t.mock.timers.tick(1500);
  assert.equal(await pending, 'blocked');
  assert.equal(p._linePlayState, null);
  assert.equal(p.ctx.sources.length, 0);
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

const DECODE_FAILURE_TOAST = '这句语音无法解码，原有内容已保留。请检查语音服务后重试，文字聊天仍可继续。';
const OTHER_PLAYBACK_TOAST = '部分语音暂时无法播放，已跳过并保留文字。可稍后在 LOG 里再点「语音」重试；若反复失败，请打开「运行状态」检查服务。';

function gainNode() {
  return {
    gain: { value: 1, setValueAtTime() {}, cancelScheduledValues() {}, linearRampToValueAtTime() {} },
    connect() {},
    disconnect() {},
  };
}

function audioContextClass({ failDecode }) {
  return class {
    state = 'running';
    currentTime = 0;
    destination = {};
    createGain() { return gainNode(); }
    createAnalyser() {
      return { fftSize: 256, smoothingTimeConstant: 0.6, frequencyBinCount: 128, connect() {}, disconnect() {} };
    }
    createBufferSource() {
      const source = { buffer: null, connect() {}, disconnect() {}, start() {}, stop() {}, onended: null };
      return source;
    }
    async decodeAudioData() {
      if (failDecode) throw new DOMException('Unable to decode audio data', 'EncodingError');
      return { duration: 1 };
    }
    async resume() { this.state = 'running'; }
    async suspend() { this.state = 'suspended'; }
    async close() { this.state = 'closed'; }
  };
}

test('decode failure shows its own notice while other playback errors keep the LOG retry notice', async () => {
  const previous = {
    window: globalThis.window,
    fetch: globalThis.fetch,
    document: globalThis.document,
  };
  const doc = fakeDocument();
  globalThis.document = doc;
  const toastRoot = doc.createElement('div');
  toastRoot.id = 'toastRoot';
  doc.body.appendChild(toastRoot);
  const fetches = [];
  globalThis.fetch = async url => {
    fetches.push(String(url));
    if (String(url).includes('down.wav')) return new Response('unavailable', { status: 500 });
    return new Response(new Uint8Array([0, 1, 2, 3]), { status: 200, headers: { 'Content-Type': 'audio/wav' } });
  };
  globalThis.window = { AudioContext: audioContextClass({ failDecode: true }) };
  clearMemAudioCache();
  initAudioPlayerUi({}, {});

  const root = doc.createElement('div');
  doc.body.appendChild(root);
  const session = {
    id: 's_decode',
    messages: [{
      id: 'm_decode',
      role: 'assistant',
      ts: Date.now(),
      lines: [{
        index: 0,
        zh: '你好',
        ja: 'こんにちは',
        audio: {
          status: 'ready',
          generated_at: Date.now(),
          chunks: [{ index: 0, line: 0, audio_url: '/audio/corrupt.wav', sentence: 'こんにちは' }],
        },
      }],
    }],
  };
  const log = createBacklog(root, {
    getSession: () => session,
    getSettings: () => ({}),
    voiceEnabled: () => true,
    audio: streamAudioController,
    regenerateLineAudio: async () => {},
  });

  const clearToasts = () => {
    for (const toast of toastRoot.querySelectorAll('.toast')) {
      clearTimeout(toast._timer);
      toast.remove();
    }
  };

  try {
    const replayBtn = log.element.querySelector('.adv-log-replay-btn');
    assert.equal(replayBtn.disabled, false);
    await replayBtn.onclick({ stopPropagation() {} });
    const decodeToast = toastRoot.querySelector('.toast')?.dataset?.msg || '';
    assert.equal(fetches.filter(url => url.includes('corrupt.wav')).length, 2);
    assert.equal(replayBtn.disabled, true);
    assert.equal(log.element.querySelector('.adv-log-audio-status').textContent, '语音已过期或不可用');
    assert.equal(decodeToast, DECODE_FAILURE_TOAST);
    assert.doesNotMatch(decodeToast, /再点「语音」重试/);

    clearToasts();
    await streamAudioController.close();
    globalThis.window = { AudioContext: audioContextClass({ failDecode: false }) };
    initAudioPlayerUi({}, {});
    const message = {
      id: 'm_other',
      role: 'assistant',
      audioUrls: ['/audio/ok.wav', '/audio/down.wav'],
    };
    await playAiVoice(message, { setLoading() {}, setPlaying() {}, setProgress() {} });
    let otherToast = '';
    const deadline = Date.now() + 1000;
    while (Date.now() < deadline) {
      otherToast = toastRoot.querySelector('.toast')?.dataset?.msg || '';
      if (otherToast) break;
      await tick(10);
    }
    assert.equal(otherToast, OTHER_PLAYBACK_TOAST);
    assert.match(otherToast, /再点「语音」重试/);
  } finally {
    clearToasts();
    log.destroy();
    streamAudioController.onError = null;
    streamAudioController.onQueueEmpty = null;
    await streamAudioController.close();
    clearMemAudioCache();
    globalThis.window = previous.window;
    globalThis.fetch = previous.fetch;
    if (previous.document === undefined) delete globalThis.document;
    else globalThis.document = previous.document;
  }
});
