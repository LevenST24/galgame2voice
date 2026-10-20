import assert from 'node:assert/strict';
import { test, beforeEach, afterEach } from 'node:test';
import { createConversation } from '../frontend/src/conversation.js';
import { StreamAudioController } from '../frontend/src/audio_player.js';
import { normalizeLines, normalizeSnapshot, portableSnapshot } from '../frontend/src/session_data.js';
import { mapBackendMessage } from '../frontend/src/main.js';
import { state, loadState, saveState, DEFAULT_SESSION_SETTINGS, DEFAULT_GLOBAL } from '../frontend/src/store.js';
import { fakeChatStream, tick } from './helpers/chat_stream.mjs';

/* -------------------------------------------------------------
 * 1. Controller Tests
 * -----------------------------------------------------------*/
function setupController({ voiceMode = false, audioMode = 'sequential' } = {}) {
  const session = { id: 's', title: '新对话', settings: { temperature: 0.7, voiceProfileId: 3 }, messages: [] };
  const appState = { activeId: 's', global: { voiceMode, ttsPreset: 'balanced' }, sessions: [session] };
  const stream = fakeChatStream();
  const saves = [], persisted = [], cached = [], audio = [];
  let sequence = 0, currentVoice = null, control = null;
  const audioPlayer = {
    isPlaying: false, currentMsgId: null, paused: false,
    startSession(id, ctl, options) { this.currentMsgId = id; this.isPlaying = true; audio.push(['start', id, ctl, options]); },
    enqueueChunk(chunk) { audio.push(['chunk', chunk]); },
    finishInput() { audio.push(['finish']); },
    attachControl(ctl, id) { this.currentMsgId = id; audio.push(['attach', ctl, id]); },
    pause() { this.paused = true; }, resume() { this.paused = false; },
    interrupt(ms) { this.isPlaying = false; audio.push(['interrupt', ms]); },
  };
  const doSaveState = () => saves.push({ busy: conversation.busy, messages: structuredClone(session.messages) });
  const conversation = createConversation({
    state: appState, getActive: () => session, getSession: id => id === session.id ? session : null,
    uid: prefix => `${prefix}_${++sequence}`, saveState: doSaveState,
    addMessage(role, content) {
      const message = { id: `m_${++sequence}`, role, content, ts: Date.now() };
      session.messages.push(message); session.title = content; doSaveState(); return message;
    },
    persistSession: async s => persisted.push(s.id), isSessionDeleting: () => false,
    audioStore: new Map(), putCachedAudioBlob: () => {},
    fetchAndCacheAudio: async url => cached.push(url), audioPlayer,
    getCurrentVoice: () => currentVoice, setCurrentVoice: voice => { currentVoice = voice; },
    stopCurrentVoice: () => { currentVoice = null; audioPlayer.interrupt(40); },
    getAudioControl: () => control, onBlocked: () => {},
    getAudioMode: () => audioMode,
    fetch: stream.fetch,
  });
  return { conversation, session, state: appState, stream, saves, cached, audio, audioPlayer,
    get voice() { return currentVoice; }, set control(value) { control = value; } };
}

test('Controller: line, line_audio and done.lines reconciliation', async () => {
  const app = setupController({ audioMode: 'lines' });
  const lineEvents = [], lineAudioEvents = [];
  app.conversation.on('line', event => lineEvents.push(event));
  app.conversation.on('line_audio', event => lineAudioEvents.push(event));

  app.conversation.send('开始');
  const request = app.stream.requests[0];
  await request.onChunk('你好');

  // 1. Emit line 0
  const serverLine0 = { index: 0, zh: '你好', ja: 'こんにちは', emotion: 'gentle', act: {} };
  await request.event('line', serverLine0);
  assert.equal(lineEvents.length, 1);
  assert.equal(lineEvents[0].line.index, 0);
  assert.equal(lineEvents[0].line.zh, '你好');
  assert.equal(lineEvents[0].line.audio.status, 'pending');

  // 2. Emit line_audio 0
  const lineAudio0 = {
    line: 0,
    status: 'ready',
    chunks: [{ index: 0, line: 0, audio_url: '/audio/0.wav', sentence: 'こんにちは' }],
  };
  await request.event('line_audio', lineAudio0);
  assert.equal(lineAudioEvents.length, 1);
  assert.equal(lineAudioEvents[0].line, 0);
  assert.equal(lineAudioEvents[0].status, 'ready');
  assert.equal(lineAudioEvents[0].chunks.length, 1);

  // 3. Emit done with authoritative done.lines snapshot
  const authoritativeLines = [
    {
      index: 0, zh: '你好', ja: 'こんにちは', emotion: 'happy', act: { face: 'smile' },
      audio: { status: 'ready', chunks: [{ index: 0, line: 0, audio_url: '/audio/authoritative.wav', sentence: 'こんにちは' }] },
    },
  ];
  await request.onEnd('你好', { lines: authoritativeLines });

  const finalMsg = app.session.messages.at(-1);
  assert.deepEqual(finalMsg.lines, authoritativeLines);
});

test('Controller: audio_chunk arriving before its line', async () => {
  const app = setupController({ audioMode: 'lines' });
  const lineEvents = [];
  app.conversation.on('line', event => lineEvents.push(event));

  app.conversation.send('早到音频');
  const request = app.stream.requests[0];
  await request.onChunk('你好');

  // audio_chunk arrives before line 0
  await request.event('audio_chunk', {
    index: 0, line: 0, audio_url: '/audio/early.wav', sentence: 'こんにちは', emotion: 'happy',
  });

  // Now line 0 arrives
  await request.event('line', { index: 0, zh: '你好', ja: 'こんにちは', emotion: 'happy', act: {} });
  assert.equal(lineEvents.length, 1);
  assert.equal(lineEvents[0].line.audio.status, 'pending');
  assert.equal(lineEvents[0].line.audio.chunks.length, 1);
  assert.equal(lineEvents[0].line.audio.chunks[0].audio_url, '/audio/early.wav');

  await request.onEnd('你好');
  const finalMsg = app.session.messages.at(-1);
  assert.equal(finalMsg.lines[0].audio.chunks[0].audio_url, '/audio/early.wav');
});

test('Controller: failed with partial chunks', async () => {
  const app = setupController({ audioMode: 'lines' });
  const lineAudioEvents = [];
  app.conversation.on('line_audio', event => lineAudioEvents.push(event));

  app.conversation.send('部分失败');
  const request = app.stream.requests[0];
  await request.onChunk('回复');

  await request.event('line', { index: 0, zh: '回复', ja: 'へんじ', emotion: 'sad', act: {} });
  await request.event('line_audio', {
    line: 0,
    status: 'failed',
    chunks: [{ index: 0, line: 0, audio_url: '/audio/partial.wav', sentence: 'へん' }],
    error: 'TTS failed',
  });

  assert.equal(lineAudioEvents.length, 1);
  assert.equal(lineAudioEvents[0].status, 'failed');
  assert.equal(lineAudioEvents[0].chunks.length, 1);
  assert.equal(lineAudioEvents[0].chunks[0].audio_url, '/audio/partial.wav');

  await request.onEnd('回复');
  const finalMsg = app.session.messages.at(-1);
  assert.equal(finalMsg.lines[0].audio.status, 'failed');
  assert.equal(finalMsg.lines[0].audio.chunks.length, 1);
});

test('Controller: pending lines becoming failed on error and on cancel', async () => {
  // 1. On error
  const appError = setupController({ audioMode: 'lines' });
  appError.conversation.send('错误测试');
  const reqError = appError.stream.requests[0];
  await reqError.onChunk('错误');
  await reqError.event('line', { index: 0, zh: '错误', ja: 'エラー', emotion: 'sad', act: {} });
  await reqError.onEnd('', { error: 'Network Error', errorCode: 'NETWORK_ERR' });
  assert.equal(appError.session.messages.length, 2);
  const assistantMsg = appError.session.messages[1];
  assert.equal(assistantMsg.lines[0].audio.status, 'failed');
  assert.equal(appError.conversation.busy, false);

  // 2. On cancel
  const appCancel = setupController({ audioMode: 'lines' });
  appCancel.conversation.send('取消测试');
  const reqCancel = appCancel.stream.requests[0];
  await reqCancel.onChunk('部分回复');
  await reqCancel.event('line', { index: 0, zh: '部分', ja: 'ぶぶん', emotion: 'gentle', act: {} });
  appCancel.conversation.stop();
  await tick();
  const cancelMsg = appCancel.session.messages.at(-1);
  assert.equal(cancelMsg.lines[0].audio.status, 'failed');
});

test('Controller: a truncated done stays a completion and settles pending lines', async () => {
  for (const meta of [{ truncated: true }, {}]) {
    const app = setupController({ audioMode: 'lines' });
    const ends = [];
    app.conversation.on('done', event => ends.push(['done', event]));
    app.conversation.on('cancelled', event => ends.push(['cancelled', event]));
    app.conversation.send('截断测试');
    const request = app.stream.requests[0];
    await request.onChunk('前半');
    await request.event('line', { index: 0, zh: '前半', ja: 'ぜんはん', emotion: 'gentle', act: {} });
    await request.onEnd('前半', meta);
    // The old chat view must not treat a server-side truncation as a user cancel.
    assert.deepEqual(ends.map(([kind]) => kind), ['done']);
    assert.equal(ends[0][1].meta.cancelled, false);
    assert.equal(ends[0][1].meta.truncated, Boolean(meta.truncated));
    // No line_audio can arrive after the turn settles.
    assert.equal(app.session.messages.at(-1).lines[0].audio.status, 'failed');
  }
});

test('Controller: an old server with no line events keeps lines undefined', async () => {
  const app = setupController();
  app.conversation.send('旧版服务器');
  const request = app.stream.requests[0];
  await request.onChunk('你好');
  await request.event('audio_chunk', { audio_url: '/audio/legacy.wav', sentence: 'こんにちは' });
  await request.onEnd('你好');

  const finalMsg = app.session.messages.at(-1);
  assert.equal(finalMsg.content, '你好');
  assert.equal(finalMsg.lines, undefined);
});

test('Controller: sequential vs lines mode, with no autoplay in lines mode', async () => {
  // Sequential mode autoplays
  const appSeq = setupController({ voiceMode: true, audioMode: 'sequential' });
  appSeq.conversation.send('顺序播放');
  const reqSeq = appSeq.stream.requests[0];
  await reqSeq.onChunk('顺序');
  await reqSeq.event('audio_chunk', { audio_url: '/audio/seq.wav', sentence: 'じゅんばん' });
  assert.ok(appSeq.audio.some(([type]) => type === 'start'));
  assert.ok(appSeq.audio.some(([type]) => type === 'chunk'));
  await reqSeq.onEnd('顺序');

  // Lines mode does NOT autoplay
  const appLines = setupController({ voiceMode: true, audioMode: 'lines' });
  appLines.conversation.send('按行播放');
  const reqLines = appLines.stream.requests[0];
  await reqLines.onChunk('按行');
  await reqLines.event('line', { index: 0, zh: '按行', ja: 'ぎょう', emotion: 'gentle', act: {} });
  await reqLines.event('audio_chunk', { index: 0, line: 0, audio_url: '/audio/line0.wav', sentence: 'ぎょう' });
  assert.equal(appLines.audio.length, 0); // No autoplay calls!
  assert.equal(appLines.voice, null);
  await reqLines.onEnd('按行');
});

test('Controller: stale turns ignored', async () => {
  const app = setupController({ audioMode: 'lines' });
  const lineEvents = [], lineAudioEvents = [];
  app.conversation.on('text', () => app.conversation.stop());
  app.conversation.on('line', e => lineEvents.push(e));
  app.conversation.on('line_audio', e => lineAudioEvents.push(e));

  app.conversation.send('第一轮');
  const req1 = app.stream.requests[0];
  await req1.packet([
    ['text', { delta_chinese: '收到' }],
    ['line', { index: 0, zh: '延迟台词', ja: 'おそい', emotion: 'gentle', act: {} }],
    ['line_audio', { line: 0, status: 'ready', chunks: [] }],
  ]);
  assert.equal(lineEvents.length, 0);
  assert.equal(lineAudioEvents.length, 0);
});

/* -------------------------------------------------------------
 * 2. Audio Player Tests (playLine, stopLine, fake AudioContext)
 * -----------------------------------------------------------*/
class FakeAudioContext {
  state = 'running';
  currentTime = 0;
  destination = {};
  resumes = 0;
  createdGains = [];
  createdSources = [];
  createGain() {
    const gainNode = {
      gain: {
        value: 1,
        setValueAtTime(v) { this.value = v; },
        cancelScheduledValues() {},
        linearRampToValueAtTime(v) { this.value = v; },
      },
      connections: new Set(), disconnects: 0,
      connect(target) { this.connections.add(target); },
      disconnect() { this.connections.clear(); this.disconnects++; },
    };
    this.createdGains.push(gainNode);
    return gainNode;
  }
  createBufferSource() {
    const src = {
      buffer: null,
      connect() {},
      disconnect() {},
      start(t) { this.startTime = t; },
      stop() { this.stopped = true; },
      finish() { this.onended?.(); },
    };
    this.createdSources.push(src);
    return src;
  }
  async decodeAudioData() {
    return { duration: 1 };
  }
  async resume() { this.resumes++; this.state = 'running'; }
  async close() { this.state = 'closed'; }
}

const blob = () => new Blob([new Uint8Array([1, 2, 3])], { type: 'audio/wav' });

test('AudioPlayer: every terminal path disconnects its line gain exactly once', async () => {
  globalThis.window = { AudioContext: FakeAudioContext };
  const player = new StreamAudioController();
  for (const outcome of ['ended', 'stop', 'fade', 'decode', 'fetch']) {
    globalThis.fetch = async () => { if (outcome === 'fetch') throw new Error('fetch failure'); return new Response(blob()); };
    player.ensureContext().decodeAudioData = async () => { if (outcome === 'decode') throw new Error('decode failure'); return { duration: 1 }; };
    const promise = player.playLine(0, [{ audio_url: `/audio/cleanup-${outcome}.wav` }]);
    const gain = player.ctx.createdGains.at(-1); await tick(10);
    if (outcome === 'ended') player.ctx.createdSources.at(-1).finish();
    if (outcome === 'stop' || outcome === 'fade') player.stopLine({ fadeMs: outcome === 'fade' ? 1 : 0 });
    assert.equal(await promise, ['stop', 'fade'].includes(outcome) ? 'interrupted' : ['decode', 'fetch'].includes(outcome) ? 'failed' : 'ended');
    if (outcome === 'fade') await new Promise(resolve => setTimeout(resolve, 30));
    player.stopLine({ fadeMs: 0 });
    assert.equal(gain.connections.size, 0, outcome); assert.equal(gain.disconnects, 1, outcome);
  }
  await player.close();
});

test('AudioPlayer: playLine ended on completion of all chunks', async () => {
  globalThis.window = { AudioContext: FakeAudioContext };
  globalThis.fetch = async () => new Response(blob());
  const player = new StreamAudioController();

  const playPromise = player.playLine(0, [
    { audio_url: '/audio/c0.wav', index: 0, sentence: '一' },
    { audio_url: '/audio/c1.wav', index: 1, sentence: '二' },
  ]);

  await tick(10);
  assert.equal(player.ctx.createdSources.length, 2);

  // Finish both sources
  player.ctx.createdSources[0].finish();
  player.ctx.createdSources[1].finish();

  const result = await playPromise;
  assert.equal(result, 'ended');
  await player.close();
});

test('AudioPlayer: playLine interrupted by stopLine', async () => {
  globalThis.window = { AudioContext: FakeAudioContext };
  globalThis.fetch = async () => new Response(blob());
  const player = new StreamAudioController();

  const playPromise = player.playLine(0, [{ audio_url: '/audio/c0.wav', index: 0 }]);
  await tick(10);

  player.stopLine({ fadeMs: 0 });
  const result = await playPromise;
  assert.equal(result, 'interrupted');
  await player.close();
});

test('AudioPlayer: playLine interrupted by a new playLine', async () => {
  globalThis.window = { AudioContext: FakeAudioContext };
  globalThis.fetch = async () => new Response(blob());
  const player = new StreamAudioController();

  const p1 = player.playLine(0, [{ audio_url: '/audio/c0.wav', index: 0 }]);
  await tick(10);

  const p2 = player.playLine(1, [{ audio_url: '/audio/c1.wav', index: 1 }]);
  const r1 = await p1;
  assert.equal(r1, 'interrupted');

  await tick(10);
  player.ctx.createdSources.at(-1).finish();
  const r2 = await p2;
  assert.equal(r2, 'ended');
  await player.close();
});

test('AudioPlayer: playLine failed if no chunk can play', async () => {
  globalThis.window = { AudioContext: FakeAudioContext };
  const player = new StreamAudioController();

  // Empty chunks
  const rEmpty = await player.playLine(0, []);
  assert.equal(rEmpty, 'failed');

  // Network failure for all chunks
  globalThis.fetch = async () => { throw new TypeError('Network error'); };
  const pFailed = player.playLine(0, [{ audio_url: '/audio/fail.wav', index: 0 }]);
  await tick(10);
  const rFailed = await pFailed;
  assert.equal(rFailed, 'failed');
  await player.close();
});

test('AudioPlayer: playLine partial-failure plays remaining chunks and ends', async () => {
  globalThis.window = { AudioContext: FakeAudioContext };
  let calls = 0;
  globalThis.fetch = async () => {
    calls++;
    if (calls === 1) throw new Error('Chunk 0 failed');
    return new Response(blob());
  };
  const player = new StreamAudioController();

  const playPromise = player.playLine(0, [
    { audio_url: '/audio/bad.wav', index: 0 },
    { audio_url: '/audio/good.wav', index: 1 },
  ]);

  await tick(10);
  assert.equal(player.ctx.createdSources.length, 1);
  player.ctx.createdSources[0].finish();

  const result = await playPromise;
  assert.equal(result, 'ended');
  await player.close();
});

test('AudioPlayer: satisfies adapter shape { play, stop }', async () => {
  globalThis.window = { AudioContext: FakeAudioContext };
  globalThis.fetch = async () => new Response(blob());
  const controller = new StreamAudioController();

  const adapter = {
    play: (i, chunks) => controller.playLine(i, chunks),
    stop: (o) => controller.stopLine(o),
  };

  const p = adapter.play(0, [{ audio_url: '/audio/a.wav' }]);
  await tick(10);
  adapter.stop({ fadeMs: 0 });
  assert.equal(await p, 'interrupted');
  await controller.close();
});

/* -------------------------------------------------------------
 * 3. Persistence Tests
 * -----------------------------------------------------------*/
test('Persistence: round trip through localStorage keeps lines', () => {
  const saved = new Map();
  globalThis.localStorage = {
    getItem: key => saved.get(key) ?? null,
    setItem: (key, val) => saved.set(key, val),
    removeItem: key => saved.delete(key),
  };

  loadState();
  const session = state.sessions[0];
  session.messages.push({
    id: 'm_test',
    role: 'assistant',
    content: '你好',
    lines: [
      {
        index: 0,
        zh: '你好',
        ja: 'こんにちは',
        emotion: 'gentle',
        act: { face: 'smile' },
        audio: { status: 'ready', chunks: [{ index: 0, line: 0, audio_url: '/a.wav', sentence: 'こんにちは' }] },
      },
    ],
    ts: Date.now(),
  });
  saveState();

  // Reload from storage
  loadState();
  const reloadedSession = state.sessions.find(s => s.id === session.id);
  const reloadedMsg = reloadedSession.messages.find(m => m.id === 'm_test');
  assert.ok(reloadedMsg.lines);
  assert.equal(reloadedMsg.lines.length, 1);
  assert.equal(reloadedMsg.lines[0].zh, '你好');
  assert.equal(reloadedMsg.lines[0].audio.status, 'ready');
  assert.equal(reloadedMsg.lines[0].audio.chunks[0].audio_url, '/a.wav');
});

test('Persistence: stored pending audio becomes failed', () => {
  const [line] = normalizeLines([{ index: 0, zh: '你好', ja: 'こんにちは', emotion: 'gentle', act: {},
    audio: { status: 'pending', chunks: [] } }]);
  assert.equal(line.audio.status, 'failed');
});

test('Persistence: normalizeSnapshot validates lines and drops invalid entries', () => {
  const rawData = {
    sessions: [
      {
        id: 's_test',
        title: '测试',
        createdAt: 100,
        updatedAt: 200,
        settings: {},
        messages: [
          {
            id: 'm_valid',
            role: 'assistant',
            content: '有效',
            lines: [
              { index: 0, zh: '正确', ja: 'せいかい', emotion: 'gentle', act: { face: 'smile' }, audio: { status: 'ready', chunks: [] } },
              { index: -1, zh: '负索引' }, // invalid index
              { index: 1, zh: 123 }, // invalid zh
              { index: 2, zh: '无act', ja: 'ja', emotion: 'gentle', act: 'not-an-object' }, // invalid act
              { index: 3, zh: '未知状态', ja: 'ja', emotion: 'gentle', act: {}, audio: { status: 'weird' } }, // status becomes 'skipped'
            ],
            ts: 100,
          },
          {
            id: 'm_no_lines',
            role: 'user',
            content: '无lines',
            ts: 101,
          },
        ],
      },
    ],
  };

  const normalized = normalizeSnapshot(rawData, DEFAULT_SESSION_SETTINGS, DEFAULT_GLOBAL);
  const validMsg = normalized.sessions[0].messages[0];
  assert.equal(validMsg.lines.length, 2);
  assert.equal(validMsg.lines[0].index, 0);
  assert.equal(validMsg.lines[0].audio.status, 'ready');
  assert.equal(validMsg.lines[1].index, 3);
  assert.equal(validMsg.lines[1].audio.status, 'skipped'); // normalized unknown status

  const noLinesMsg = normalized.sessions[0].messages[1];
  assert.equal(noLinesMsg.lines, undefined);
});

test('Persistence: portableSnapshot removes cache URLs and sets audio to skipped', () => {
  const snapshot = {
    sessions: [
      {
        id: 's_port',
        title: '导出',
        createdAt: 100,
        updatedAt: 200,
        settings: {},
        messages: [
          {
            id: 'm_port',
            role: 'assistant',
            content: '导出内容',
            lines: [
              {
                index: 0,
                zh: '导出行',
                ja: 'エクスポート',
                emotion: 'happy',
                act: { motion: 'nod' },
                audio: { status: 'ready', chunks: [{ index: 0, line: 0, audio_url: '/cached/file.wav', sentence: 'エクスポート' }] },
              },
            ],
            ts: 100,
          },
        ],
      },
    ],
  };

  const portable = portableSnapshot(snapshot);
  const portMsg = portable.sessions[0].messages[0];
  assert.equal(portMsg.lines[0].index, 0);
  assert.equal(portMsg.lines[0].zh, '导出行');
  assert.deepEqual(portMsg.lines[0].act, { motion: 'nod' });
  assert.deepEqual(portMsg.lines[0].audio, { status: 'skipped', chunks: [] });
  assert.ok(!JSON.stringify(portable).includes('/cached/file.wav'));
});

test('Persistence: mapBackendMessage with and without lines', () => {
  // With lines
  const bmWithLines = {
    id: 42,
    role: 'assistant',
    content_chinese: '后端中文',
    content_japanese: '日本語',
    audio_url: '/audio/total.wav',
    created_at: '2026-10-19 12:00:00',
    lines: [
      { index: 0, zh: '第一句', ja: 'ひとつめ', emotion: 'gentle', act: {} },
    ],
  };
  const msgWith = mapBackendMessage(bmWithLines);
  assert.equal(msgWith.id, 'm_42');
  assert.ok(Array.isArray(msgWith.lines));
  assert.equal(msgWith.lines.length, 1);
  assert.equal(msgWith.lines[0].zh, '第一句');
  assert.equal(msgWith.lines[0].audio.status, 'skipped');

  // Without lines
  const bmWithoutLines = {
    id: 43,
    role: 'assistant',
    content_chinese: '旧消息',
    created_at: '2026-10-19 12:00:00',
    lines: null,
  };
  const msgWithout = mapBackendMessage(bmWithoutLines);
  assert.equal(msgWithout.id, 'm_43');
  assert.equal(msgWithout.lines, undefined);
});
