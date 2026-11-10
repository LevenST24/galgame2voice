import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import { mountAdv, formatTurnTimelineEntry } from '../frontend/src/adv/adv_app.js';
import { initApp } from '../frontend/src/main.js';
import { DEFAULT_SESSION_SETTINGS } from '../frontend/src/store.js';
import { fakeDocument, fakeClock, Element } from './helpers/adv_dom.mjs';
import { fakeChatStream, tick } from './helpers/chat_stream.mjs';

function setupStage(t, { clockMs = 1000, fetchImpl } = {}) {
  const doc = fakeDocument();
  globalThis.document = doc;
  let nowMs = clockMs;
  const baseClock = fakeClock();
  const clock = {
    ...baseClock,
    now: () => nowMs,
    setNow: (ms) => { nowMs = ms; },
  };
  const listeners = new Map();
  const session = {
    id: 's_0',
    settings: { ...DEFAULT_SESSION_SETTINGS, advEnabled: true, advTextIntervalMs: 0 },
    messages: [],
  };
  class Image extends Element {
    constructor() { super('img', doc); }
    decode() { return Promise.resolve(); }
  }
  const controller = {
    busy: false,
    on(type, fn) {
      if (!listeners.has(type)) listeners.set(type, new Set());
      listeners.get(type).add(fn);
      return () => listeners.get(type)?.delete(fn);
    },
    send() { return true; },
    canRetry() { return false; },
    retry() {},
    stop() {},
  };
  const audio = {
    played: [],
    stopped: [],
    supportsStartNotification: true,
    playLine(index, chunks, opts) {
      const entry = { index, chunks, opts };
      this.played.push(entry);
      return new Promise(resolve => { entry.resolve = resolve; });
    },
    stopLine(opts) {
      this.stopped.push(opts);
    },
  };
  const root = doc.createElement('div');
  const sidebar = doc.createElement('div');
  const gDashPerfSummary = doc.createElement('pre');
  gDashPerfSummary.id = 'g-dash-perf-summary';
  doc.body.appendChild(root);
  doc.body.appendChild(sidebar);
  doc.body.appendChild(gDashPerfSummary);

  const fetchCalls = [];
  globalThis.fetch = fetchImpl || (async (url, opts) => {
    fetchCalls.push({ url, opts });
    return Response.json({ backgrounds: [], works: [] });
  });

  const app = mountAdv({ root, sidebar, gDashPerfSummary }, {
    controller,
    audio,
    clock,
    getSession: () => session,
    voiceEnabled: () => true,
    getCharacter: () => ({ id: 1, name: '测试角色' }),
    ImageClass: Image,
    loadPortrait: async () => null,
    fetchBackgrounds: async () => ({ backgrounds: [], works: [] }),
    matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }),
  });
  t.after(() => app.unmount());

  const emit = (type, event) => {
    for (const fn of [...(listeners.get(type) || [])]) fn(event);
  };
  let turnCounter = 0;
  let currentTurnId = null;

  const startTurn = (atMs) => {
    if (atMs !== undefined) clock.setNow(atMs);
    currentTurnId = `turn_${++turnCounter}`;
    controller.busy = true;
    const userMessage = { id: `u_${turnCounter}`, role: 'user', content: '你好' };
    session.messages.push(userMessage);
    emit('turnstart', {
      originId: session.id,
      assistantMessage: { id: currentTurnId, lines: [] },
      userMessage,
    });
    return currentTurnId;
  };

  const pushLine = (index, { zh = `台词${index}`, ja = `セリフ${index}` } = {}) => {
    const record = { index, zh, ja, emotion: 'gentle', act: {} };
    emit('line', { messageId: currentTurnId, line: record });
    return record;
  };

  const pushLineAudio = (index, status = 'ready') => {
    const chunks = status === 'ready'
      ? [{ index: 0, line: index, audio_url: `/audio/${currentTurnId}_${index}.wav` }]
      : [];
    emit('line_audio', { messageId: currentTurnId, line: index, status, chunks });
  };

  const finishTurn = (telemetry, extra = {}) => {
    controller.busy = false;
    emit('done', {
      originId: session.id,
      message: { id: `m_${turnCounter}`, role: 'assistant', content: '回复', lines: [] },
      meta: { telemetry, ...extra },
    });
  };

  return {
    app,
    doc,
    dom: { root, sidebar, gDashPerfSummary },
    clock,
    audio,
    fetchCalls,
    startTurn,
    pushLine,
    pushLineAudio,
    finishTurn,
  };
}

test('onStart before done records all 4 stage milestones and formats summary', t => {
  const h = setupStage(t, { clockMs: 1000 });
  assert.equal(h.dom.gDashPerfSummary.textContent, '等待对话数据...');

  h.startTurn(1000);
  h.pushLine(0);
  h.pushLineAudio(0, 'ready');
  assert.equal(h.audio.played.length, 1);

  h.clock.setNow(1620);
  h.audio.played[0].opts.onStart();

  h.finishTurn({
    total_ms: 850.4,
    marks: {
      llm_first_token: 210.2,
      first_sentence_parsed: 340.6,
      first_tts_ready: 580.1,
    },
  });

  const timelines = h.app.getRecentTurnTimelines();
  assert.deepEqual(timelines, [
    {
      turn_seq: 1,
      request_start_ms: 0,
      llm_first_token_ms: 210,
      first_sentence_parsed_ms: 341,
      first_tts_ready_ms: 580,
      first_audio_play_ms: 620,
      total_ms: 850,
    },
  ]);
  const expectedLine = '#1 首字: 210ms | 首句切出: 341ms | 首音就绪: 580ms | 首音播放: 620ms';
  assert.equal(formatTurnTimelineEntry(timelines[0]), expectedLine);
  assert.equal(h.dom.gDashPerfSummary.textContent, expectedLine);
});

test('done arriving before onStart never leaks previous turn first_audio_play_ms and updates in place when onStart fires', t => {
  const h = setupStage(t, { clockMs: 1000 });

  // Turn 1
  h.startTurn(1000);
  h.pushLine(0);
  h.pushLineAudio(0, 'ready');
  h.clock.setNow(1620);
  h.audio.played[0].opts.onStart();
  h.audio.played[0].resolve('ended');
  h.finishTurn({
    total_ms: 850,
    marks: { llm_first_token: 210, first_sentence_parsed: 341, first_tts_ready: 580 },
  });

  // Turn 2: done arrives before audio playback starts
  h.startTurn(3000);
  h.pushLine(0);
  h.pushLineAudio(0, 'ready');
  assert.equal(h.audio.played.length, 2);

  h.finishTurn({
    total_ms: 400,
    marks: { llm_first_token: 120, first_sentence_parsed: 180, first_tts_ready: 310 },
  });

  let timelines = h.app.getRecentTurnTimelines();
  assert.equal(timelines.length, 2);
  assert.equal(timelines[1].turn_seq, 2);
  assert.equal(timelines[1].first_audio_play_ms, null);
  assert.equal(
    h.dom.gDashPerfSummary.textContent.split('\n')[0],
    '#2 首字: 120ms | 首句切出: 180ms | 首音就绪: 310ms | 首音播放: --',
  );

  // Audio playback starts at 3350ms
  h.clock.setNow(3350);
  h.audio.played[1].opts.onStart();

  timelines = h.app.getRecentTurnTimelines();
  assert.equal(timelines[1].first_audio_play_ms, 350);
  assert.equal(
    h.dom.gDashPerfSummary.textContent.split('\n')[0],
    '#2 首字: 120ms | 首句切出: 180ms | 首音就绪: 310ms | 首音播放: 350ms',
  );
});

test('skipped line 0 still records first_audio_play_ms when line 1 starts playing', t => {
  const h = setupStage(t, { clockMs: 5000 });

  h.startTurn(5000);
  h.pushLine(0, { zh: '（轻轻点头）', ja: '' });
  h.pushLineAudio(0, 'skipped');
  h.pushLine(1, { zh: '早上好。', ja: 'おはよう。' });
  h.pushLineAudio(1, 'ready');

  assert.equal(h.audio.played.length, 0);
  h.app.action('advance');
  assert.equal(h.audio.played.length, 1);
  assert.equal(h.audio.played[0].index, 1);

  h.clock.setNow(5480);
  h.audio.played[0].opts.onStart();
  h.finishTurn({
    total_ms: 700,
    marks: { llm_first_token: 150, first_sentence_parsed: 260, first_tts_ready: 410 },
  });

  const timelines = h.app.getRecentTurnTimelines();
  assert.equal(timelines.length, 1);
  assert.equal(timelines[0].first_audio_play_ms, 480);
  assert.equal(
    h.dom.gDashPerfSummary.textContent,
    '#1 首字: 150ms | 首句切出: 260ms | 首音就绪: 410ms | 首音播放: 480ms',
  );
});

test('ring buffer retains at most 5 recent turns and sends zero extra telemetry network requests', t => {
  const h = setupStage(t, { clockMs: 1000 });

  for (let i = 1; i <= 6; i++) {
    const startMs = i * 1000;
    h.startTurn(startMs);
    h.pushLine(0);
    h.pushLineAudio(0, 'ready');
    h.clock.setNow(startMs + 300 + i);
    h.audio.played.at(-1).opts.onStart();
    h.audio.played.at(-1).resolve('ended');
    h.finishTurn({
      total_ms: 500 + i,
      marks: {
        llm_first_token: 100 + i,
        first_sentence_parsed: 200 + i,
        first_tts_ready: 250 + i,
      },
    });
  }

  const timelines = h.app.getRecentTurnTimelines();
  assert.equal(timelines.length, 5);
  assert.deepEqual(timelines.map(entry => entry.turn_seq), [2, 3, 4, 5, 6]);
  const lines = h.dom.gDashPerfSummary.textContent.split('\n');
  assert.equal(lines.length, 5);
  assert.match(lines[0], /^#6 /);
  assert.match(lines[4], /^#2 /);
  assert.equal(h.fetchCalls.length, 0);
});

test('main.js auto-binds #g-dash-perf-summary and updates it from SSE done telemetry', async t => {
  const indexHtml = fs.readFileSync(new URL('../frontend/index.html', import.meta.url), 'utf8');
  assert.ok(indexHtml.includes('id="g-dash-perf-summary"'), 'index.html must include #g-dash-perf-summary');
  assert.ok(
    indexHtml.includes('<span class="diag-title">最近对话阶段耗时 (首字 / 首句切出 / 首音就绪 / 首音播放)</span>'),
    'index.html must include updated stage timeline title',
  );

  const doc = fakeDocument();
  globalThis.document = doc;
  globalThis.window = { innerWidth: 1280 };
  t.after(() => { delete globalThis.document; });
  globalThis.localStorage = { getItem: () => null, setItem() {} };

  const advRoot = doc.createElement('div');
  advRoot.id = 'advRoot';
  const perfNode = doc.createElement('pre');
  perfNode.id = 'g-dash-perf-summary';
  perfNode.textContent = '等待对话数据...';
  doc.body.appendChild(advRoot);
  doc.body.appendChild(perfNode);

  const sessions = [{
    id: 's_0',
    title: 'Session 0',
    messages: [],
    settings: { ...DEFAULT_SESSION_SETTINGS, advEnabled: true, advTextIntervalMs: 0 },
  }];
  const state = { sessions, activeId: 's_0', global: { voiceMode: true } };
  const getActive = () => sessions[0];
  const stream = fakeChatStream();
  let seq = 0;
  const audio = {
    isPlaying: false,
    played: [],
    supportsStartNotification: true,
    playLine(index, chunks, opts) {
      this.played.push({ index, chunks, opts });
      opts?.onStart?.();
      return Promise.resolve('ended');
    },
    stopLine() {},
  };

  const noop = () => {};
  const app = initApp({
    initialize: false,
    state,
    getActive,
    getSession: id => sessions.find(s => s.id === id),
    isSessionDeleting: () => false,
    getCurrentActiveProvider: () => null,
    getVoiceProfiles: () => [],
    haveProvidersLoaded: () => true,
    saveState: noop,
    autoTitle: noop,
    persistSession: async () => {},
    clearMicTimers: noop,
    getCurrentVoice: () => null,
    stopCurrentVoice: noop,
    uid: () => `m_${++seq}`,
    fetch: stream.fetch,
    showToast: noop,
    fetchAndCacheAudio: async () => {},
    streamAudioController: audio,
    addMessage(role, content) {
      const msg = { id: `m_${++seq}`, role, content };
      getActive().messages.push(msg);
      return msg;
    },
    fetchSessionHistoryPage: async () => ({ messages: [], has_more: false }),
  });
  app.fullRender();
  t.after(() => { app.conversation.stop(); app.adv?.unmount(); });

  app.sendMessage('测试端到端耗时');
  await tick();
  const req = stream.requests[0];
  await req.event('line', { index: 0, zh: '你好。', ja: 'こんにちは。', emotion: 'gentle', act: {} });
  await req.event('line_audio', {
    line: 0,
    status: 'ready',
    chunks: [{ index: 0, line: 0, audio_url: '/audio/cache/0.wav' }],
  });
  await req.onEnd('你好。', {
    telemetry: {
      total_ms: 610,
      marks: {
        llm_first_token: 140,
        first_sentence_parsed: 220,
        first_tts_ready: 390,
      },
    },
  });

  assert.match(
    perfNode.textContent,
    /^#1 首字: 140ms \| 首句切出: 220ms \| 首音就绪: 390ms \| 首音播放: \d+ms$/,
  );

  // Turn 2: real backend ChatTurnProfiler.to_dict() payload shape + latency_ms
  app.sendMessage('第二轮');
  await tick();
  const req2 = stream.requests[1];
  await req2.event('line', { index: 0, zh: '第二句。', ja: '二番目。', emotion: 'gentle', act: {} });
  await req2.event('line_audio', {
    line: 0,
    status: 'ready',
    chunks: [{ index: 0, line: 0, audio_url: '/audio/cache/1.wav' }],
  });
  await req2.onEnd('第二句。', {
    latency_ms: 720,
    telemetry: {
      t1_ttft_ms: 160.4,
      t2_first_sentence_ms: 280.2,
      t5_app_ttfa_ms: 450.8,
    },
  });

  const lines = perfNode.textContent.split('\n');
  assert.equal(lines.length, 2);
  assert.match(
    lines[0],
    /^#2 首字: 160ms \| 首句切出: 280ms \| 首音就绪: 451ms \| 首音播放: \d+ms$/,
  );
  assert.equal(app.adv.getRecentTurnTimelines()[1].total_ms, 720);
});
