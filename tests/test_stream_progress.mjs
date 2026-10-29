import assert from 'node:assert/strict';
import test from 'node:test';
import { streamChat } from '../frontend/src/ai.js';
import { fakeClock, microtasks } from './helpers/adv_dom.mjs';

async function setup(t, { policy = true, voiceBudget = 1000 } = {}) {
  const clock = fakeClock(), ends = [], chunks = [];
  t.mock.method(globalThis, 'setTimeout', clock.setTimeout);
  t.mock.method(globalThis, 'clearTimeout', clock.clearTimeout);
  t.mock.method(globalThis.performance, 'now', clock.now);
  let pending, signal, cancelled = 0, released = 0;
  const reader = {
    read: () => new Promise(resolve => { pending = resolve; }),
    async cancel() { cancelled++; pending?.({ done: true }); pending = null; },
    releaseLock() { released++; },
  };
  const stop = streamChat({ prompt: 'synthetic', idleTimeoutMs: 100,
    fetch: async (_url, options) => { signal = options.signal; return { ok: true,
      headers: new Headers(policy ? { 'X-G2V-Progress-Policy': '1', 'X-G2V-Voice-Job-Timeout-Ms': String(voiceBudget) } : {}),
      body: { getReader: () => reader } }; },
    onChunk: full => chunks.push(full), onEnd: (text, meta) => ends.push({ text, ...meta }),
  });
  t.after(async () => { stop(); await microtasks(); });
  await microtasks();
  const raw = async text => {
    const complete = pending; pending = null;
    assert.ok(complete, 'a reader owns the next packet');
    complete({ done: false, value: new TextEncoder().encode(text) });
    await microtasks();
  };
  return { clock, ends, chunks, stop, raw,
    event: (name, value) => raw(`event: ${name}\ndata: ${JSON.stringify(value)}\n\n`),
    tick: async ms => { clock.tick(ms); await microtasks(); },
    get signal() { return signal; }, get cancelled() { return cancelled; }, get released() { return released; },
  };
}

test('heartbeats keep the transport alive but cannot keep an idle reply pending forever', async t => {
  const h = await setup(t);
  for (let i = 0; i < 3; i++) { await h.tick(25); await h.raw(': keep-alive\n\n'); }
  await h.tick(25);
  assert.equal(h.ends.length, 1);
  assert.match(h.ends[0].error, /超时/);
  assert.equal(h.ends[0].cancelled, false);
  assert.equal(h.ends[0].errorCode, 'stream_idle_timeout');
  assert.equal(h.signal.aborted, true);
  assert.equal(h.cancelled, 1); assert.equal(h.released, 1);
  assert.equal(h.clock.timers.size, 0);
});

test('empty deltas, unknown events and malformed frames cannot renew the reply budget', async t => {
  const h = await setup(t);
  await h.tick(25); await h.event('text', { delta_chinese: '' });
  await h.tick(25); await h.event('ping', { alive: true });
  await h.tick(25); await h.raw('event: text\ndata: {malformed}\n\n');
  await h.tick(25);
  assert.equal(h.ends.length, 1);
  assert.match(h.ends[0].error, /超时/);
});

test('a packet dribbling an unfinished JSON frame does not renew the reply budget', async t => {
  const h = await setup(t);
  for (const fragment of ['event: text\n', 'data: {"delta_', 'chinese":"']) {
    await h.tick(25); await h.raw(fragment);
  }
  await h.tick(25);
  assert.equal(h.ends.length, 1);
  assert.match(h.ends[0].error, /超时/);
});

test('real text progress renews the idle budget rather than imposing a total turn limit', async t => {
  const h = await setup(t);
  for (const text of ['一', '二', '三']) {
    await h.tick(80); await h.event('text', { delta_chinese: text });
    assert.equal(h.ends.length, 0);
  }
  await h.tick(80); await h.event('done', { chinese: '一二三' });
  assert.equal(h.ends.length, 1);
  assert.equal(h.ends[0].text, '一二三');
  assert.equal(h.ends[0].error, null);
  assert.equal(h.clock.timers.size, 0);
});

test('structured lines and completed audio jobs are progress during long turns', async t => {
  const h = await setup(t);
  for (const [name, payload] of [
    ['line', { index: 0, zh: '中文', ja: '日本語', emotion: 'neutral' }],
    ['audio_chunk', { line: 0, index: 0, audio_url: '/audio/synthetic.wav' }],
    ['line_audio', { line: 0, status: 'failed', chunks: [] }],
    ['audio_chunk_error', { line: 1, index: 0, error: 'Synthetic synthesis failure' }],
  ]) {
    await h.tick(80); await h.event(name, payload);
    assert.equal(h.ends.length, 0, name);
  }
  await h.tick(80); await h.event('done', { chinese: '中文' });
  assert.equal(h.ends[0].error, null);
});

test('timeout after partial text preserves the received reply despite continuing heartbeats', async t => {
  const h = await setup(t);
  await h.tick(80); await h.event('text', { delta_chinese: '已收到的文字' });
  for (let i = 0; i < 3; i++) { await h.tick(25); await h.raw(': keep-alive\n\n'); }
  await h.tick(25);
  assert.equal(h.ends.length, 1);
  assert.equal(h.ends[0].text, '已收到的文字');
  assert.match(h.ends[0].error, /超时/);
  assert.equal(h.ends[0].errorCode, 'stream_idle_timeout');
});

test('manual stop during heartbeat-only waiting stays cancellation, with no timeout or retry', async t => {
  const h = await setup(t);
  await h.tick(50); await h.raw(': keep-alive\n\n');
  h.stop(); await microtasks(); await h.tick(500);
  assert.equal(h.ends.length, 1);
  assert.equal(h.ends[0].cancelled, true);
  assert.equal(h.ends[0].error, null);
  assert.equal(h.signal.aborted, true);
  assert.equal(h.cancelled, 1); assert.equal(h.released, 1);
  assert.equal(h.clock.timers.size, 0);
});

test('an announced slow voice job keeps its own bounded budget across ordinary model progress', async t => {
  const h = await setup(t);
  await h.event('voice_job', { index: 0, status: 'started' });
  await h.tick(80); await h.event('text', { delta_chinese: '模型仍在生成' });
  for (let i = 0; i < 8; i++) { await h.tick(50); await h.raw(': keep-alive\n\n'); }
  assert.equal(h.ends.length, 0, 'the model budget cannot shorten the active voice budget');
  await h.event('voice_job', { index: 0, status: 'finished' });
  await h.event('audio_chunk', { index: 0, audio_url: '/audio/slow.wav' });
  await h.event('done', { chinese: '模型仍在生成' });
  assert.equal(h.ends[0].error, null);
});

test('an old backend keeps the compatibility voice window without infinite heartbeat renewal', async t => {
  const h = await setup(t, { policy: false });
  for (let i = 0; i < 8; i++) { await h.tick(50); await h.raw(': keep-alive\n\n'); }
  assert.equal(h.ends.length, 0, 'old servers cannot advertise a long voice job');
  h.stop(); await microtasks();
  assert.equal(h.ends[0].cancelled, true);
});

test('duplicate starts cannot extend the fixed voice lease', async t => {
  const h = await setup(t);
  await h.event('voice_job', { index: 0, status: 'started' });
  // Advance a slow job using heartbeats. There is still a network budget per read.
  for (let i = 0; i < 9; i++) { await h.tick(99); await h.raw(': keep-alive\n\n'); }
  await h.event('voice_job', { index: 0, status: 'started' });
  for (let i = 0; i < 2 && h.ends.length === 0; i++) {
    await h.tick(99);
    if (!h.ends.length) await h.raw(': keep-alive\n\n');
  }
  assert.equal(h.ends.length, 1);
  assert.match(h.ends[0].error, /超时/);
});

test('a network stall still ends a voice wait even when its job lease has time left', async t => {
  const h = await setup(t);
  await h.event('voice_job', { index: 0, status: 'started' });
  await h.tick(100);
  assert.equal(h.ends.length, 1);
  assert.equal(h.signal.aborted, true);
});

test('a completed voice job releases the model budget and cannot be finished again to renew it', async t => {
  const h = await setup(t);
  await h.event('voice_job', { index: 0, status: 'started' });
  await h.tick(80); await h.event('voice_job', { index: 0, status: 'finished' });
  await h.tick(80); await h.event('voice_job', { index: 0, status: 'finished' });
  await h.tick(20);
  assert.equal(h.ends.length, 1);
  assert.match(h.ends[0].error, /超时/);
});

test('the old-server compatibility window also expires despite a continuous heartbeat', async t => {
  const h = await setup(t, { policy: false });
  for (let elapsed = 99; elapsed < 600000; elapsed += 99) {
    await h.tick(99); await h.raw(': keep-alive\n\n');
  }
  await h.tick(99);
  assert.equal(h.ends.length, 1);
  assert.equal(h.ends[0].errorCode, 'stream_idle_timeout');
});
