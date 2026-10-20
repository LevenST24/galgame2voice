import assert from 'node:assert/strict';
import test from 'node:test';
import { mountAdv } from '../frontend/src/adv/adv_app.js';
import { createBacklog, resolveLogTexts } from '../frontend/src/adv/backlog.js';
import { decide, createShortcuts } from '../frontend/src/adv/shortcuts.js';
import { createConversation } from '../frontend/src/conversation.js';
import { createLinePlayer } from '../frontend/src/adv/line_player.js';
import { DEFAULT_SESSION_SETTINGS } from '../frontend/src/store.js';
import { fakeDocument, fakeClock, Element, microtasks } from './helpers/adv_dom.mjs';
import { fakeChatStream, tick } from './helpers/chat_stream.mjs';

function setupApp(t, options = {}) {
  const doc = fakeDocument();
  globalThis.document = doc;
  const clock = fakeClock();
  const frames = new Map();
  const loads = [];
  const listeners = new Map();
  const mediaListeners = new Set();
  const viewport = doc.createElement('div');
  let frameId = 0, voiceEnd, overlayChange, subscription;

  const sessions = [0, 1].map((n) => ({
    id: `s_${n}`,
    title: `会话 ${n}`,
    hasMoreHistory: false,
    settings: { ...DEFAULT_SESSION_SETTINGS, advEnabled: true, ...options.settings },
    messages: [],
  }));
  let active = sessions[0];
  let character = { id: 1, name: '测试角色' };
  let voiceEnabled = options.voice !== false;
  let wallTime = 1700000000000;

  const controller = {
    busy: false,
    sent: [],
    retried: [],
    stopped: 0,
    on(type, fn) {
      if (!listeners.has(type)) listeners.set(type, new Set());
      listeners.get(type).add(fn);
      return () => listeners.get(type)?.delete(fn);
    },
    send(text) {
      if (this.busy || active.loadingOlder || active.deleting) return false;
      this.sent.push(text);
      return true;
    },
    canRetry(user) {
      return !this.busy && !active.loadingOlder && !active.deleting && !!user?.failed && active.messages.at(-1) === user;
    },
    retry(user) {
      if (this.canRetry(user)) this.retried.push(user);
    },
    stop() {
      this.stopped++;
    },
  };

  const audio = {
    played: [],
    stopped: [],
    playLine(index, chunks, opts) {
      this.played.push({ index, chunks, opts });
      return new Promise((resolve) => {
        voiceEnd = resolve;
      });
    },
    stopLine(opts) {
      this.stopped.push(opts);
      voiceEnd?.('interrupted');
    },
  };

  const dom = Object.fromEntries(
    ['root', 'sidebar', 'messages', 'input', 'micBtn'].map((key) => [
      key,
      doc.createElement(key === 'input' ? 'textarea' : 'div'),
    ])
  );
  for (const el of Object.values(dom)) doc.body.appendChild(el);

  const mql = {
    matches: false,
    addEventListener: (_, fn) => mediaListeners.add(fn),
    removeEventListener: (_, fn) => mediaListeners.delete(fn),
  };

  const calls = {
    settings: 0,
    models: 0,
    newSession: 0,
    loadOlder: 0,
    playUserVoice: [],
    stoppedOther: 0,
  };

  const app = mountAdv(dom, {
    controller,
    audio,
    clock,
    getSession: () => active,
    voiceEnabled: () => voiceEnabled,
    toggleVoice: () => { voiceEnabled = !voiceEnabled; },
    getCharacter: () => character,
    raf: (fn) => {
      frames.set(++frameId, fn);
      return frameId;
    },
    cancelRaf: (id) => frames.delete(id),
    matchMedia: () => mql,
    viewport,
    saveSettings: (patch) => {
      Object.assign(active.settings, patch);
    },
    onSettings: () => { calls.settings++; },
    onModelSettings: () => { calls.models++; },
    onNewSession: () => { calls.newSession++; },
    loadOlderHistory: async () => {
      calls.loadOlder++;
      if (options.onLoadOlder) await options.onLoadOlder(active);
    },
    playUserVoice: (msg, ctl) => {
      calls.playUserVoice.push({ msg, ctl });
    },
    stopOtherVoice: () => {
      calls.stoppedOther++;
    },
    wallNow: () => wallTime,
    subscribeOverlay: (fn) => {
      overlayChange = fn;
      return () => { overlayChange = null; };
    },
    subscribe: (fn) => {
      subscription = fn;
      return () => { subscription = null; };
    },
  });

  t.after(() => app.unmount());

  const emit = (type, event) => {
    for (const fn of [...(listeners.get(type) || [])]) fn(event);
  };

  let turn = 'turn_0';
  const start = (id = 'turn_0', userText = '用户提问') => {
    turn = id;
    controller.busy = true;
    const userMessage = { id: `u_${id}`, role: 'user', content: userText, ts: wallTime };
    const assistantMessage = { id, role: 'assistant', lines: [], ts: wallTime };
    active.messages.push(userMessage, assistantMessage);
    emit('turnstart', { originId: active.id, assistantMessage, userMessage });
    return userMessage;
  };

  const line = (index = 0, extra = {}) => {
    const record = {
      index,
      zh: '你好世界。',
      ja: 'こんにちは世界。',
      emotion: 'gentle',
      act: {},
      audio: { status: 'ready', chunks: [{ index, line: index, audio_url: `/audio/${index}.wav` }] },
      ...extra,
    };
    const assistant = active.messages.find((m) => m.id === turn && m.role === 'assistant');
    if (assistant) {
      assistant.lines.push(record);
    } else {
      active.messages.push({ id: turn, role: 'assistant', lines: [record], ts: wallTime });
    }
    emit('line', { messageId: turn, line: record });
    return record;
  };

  const audioEvent = (index = 0, status = 'ready', chunks = null, id = turn) => {
    const chunkList = chunks !== null
      ? chunks
      : (status === 'ready' ? [{ index, line: index, audio_url: `/audio/${index}.wav` }] : []);
    const event = { messageId: id, line: index, status, chunks: chunkList };
    emit('line_audio', event);
  };

  const done = (type = 'done', extra = {}) => {
    controller.busy = false;
    // Like the real controller, discard an empty streaming placeholder at settlement.
    active.messages = active.messages.filter(m => m.role !== 'assistant' || m.content || m.lines?.length);
    emit(type, { originId: active.id, ...extra });
  };

  return {
    app, doc, dom, clock, controller, audio, sessions, calls, active: () => active,
    emit, start, line, audioEvent, done,
    setWallTime: (t) => { wallTime = t; },
    getWallTime: () => wallTime,
    endVoice: (outcome) => voiceEnd?.(outcome),
  };
}

test('LOG: entries for lines, legacy and user messages, and subtitle settings', (t) => {
  const h = setupApp(t);
  const session = h.active();
  const now = h.getWallTime();

  session.messages = [
    { id: 'm_u1', role: 'user', content: '用户第一句话', ts: now - 1000 },
    {
      id: 'm_a1',
      role: 'assistant',
      ts: now - 500,
      lines: [
        { index: 0, zh: '第一句中文', ja: '第一句日文', emotion: 'gentle', act: {}, audio: { status: 'ready', chunks: [{ index: 0, line: 0, audio_url: '/0.wav' }] } },
        { index: 1, zh: '第二句中文', ja: '第二句日文', emotion: 'happy', act: {}, audio: { status: 'ready', chunks: [{ index: 1, line: 1, audio_url: '/1.wav' }] } },
      ],
    },
    {
      id: 'm_a2',
      role: 'assistant',
      content: '旧版 aggregate 中文',
      japanese: '旧版 aggregate 日文',
      ts: now - 200,
    },
    {
      id: 'm_u2',
      role: 'user',
      content: '带录音的用户消息',
      recording: 'blob:user_voice',
      dur: 4,
      ts: now - 100,
    },
    {
      id: 'm_u3',
      role: 'user',
      content: '失败的用户消息',
      failed: { error: '连接超时' },
      ts: now,
    },
  ];

  h.app.action('log');
  const overlay = h.app.stage.stage.querySelector('.adv-log-overlay');
  assert.ok(overlay, 'LOG overlay should be rendered');
  assert.equal(overlay.getAttribute('role'), 'dialog');
  assert.equal(overlay.getAttribute('aria-modal'), 'true');
  assert.equal(overlay.getAttribute('aria-label'), 'LOG 历史记录');

  const entries = overlay.querySelectorAll('.adv-log-entry');
  assert.equal(entries.length, 6, 'Should render 1 user + 2 lines + 1 legacy + 1 user rec + 1 failed user = 6 entries');

  // Entry 0: User message
  assert.ok(entries[0].classList.contains('is-user'));
  assert.equal(entries[0].querySelector('.adv-log-speaker').textContent, 'Player');
  assert.equal(entries[0].querySelector('.adv-log-primary').textContent, '用户第一句话');

  // Entry 1: Assistant line 0
  assert.ok(entries[1].classList.contains('is-assistant'));
  assert.equal(entries[1].querySelector('.adv-log-speaker').textContent, '测试角色');
  assert.equal(entries[1].querySelector('.adv-log-primary').textContent, '第一句中文');
  assert.equal(entries[1].querySelector('.adv-log-subtitle').textContent, '第一句日文');
  const btn1 = entries[1].querySelector('.adv-log-replay-btn');
  assert.ok(btn1 && !btn1.disabled, 'Line with chunks should have enabled replay button');

  // Entry 2: Assistant line 1
  assert.equal(entries[2].querySelector('.adv-log-primary').textContent, '第二句中文');
  assert.equal(entries[2].querySelector('.adv-log-subtitle').textContent, '第二句日文');

  // Entry 3: Legacy assistant message
  assert.ok(entries[3].classList.contains('is-legacy'));
  assert.equal(entries[3].querySelector('.adv-log-primary').textContent, '旧版 aggregate 中文');
  assert.equal(entries[3].querySelector('.adv-log-subtitle').textContent, '旧版 aggregate 日文');

  // Entry 4: User message with recording
  const recBtn = entries[4].querySelector('.adv-log-user-recording-btn');
  assert.ok(recBtn, 'User message with recording should have play button');
  recBtn.click();
  assert.equal(h.calls.playUserVoice.length, 1);
  assert.equal(h.calls.playUserVoice[0].msg.id, 'm_u2');

  // Entry 5: Failed user message
  const failureEl = entries[5].querySelector('.adv-log-failure');
  assert.ok(failureEl, 'Failed user message should show failure reason');
  assert.ok(failureEl.textContent.includes('连接超时'));
  assert.equal(entries[5].querySelectorAll('.adv-log-entry-controls button, .adv-error-btn').length, 0, 'No action buttons inside LOG');

  h.app.action('close_overlay');
});

test('LOG: subtitle none setting hides subtitle line', (t) => {
  const h = setupApp(t, { settings: { advSubtitleLanguage: 'none' } });
  const session = h.active();
  session.messages = [
    {
      id: 'm1',
      role: 'assistant',
      ts: h.getWallTime(),
      lines: [{ index: 0, zh: '中文内容', ja: '日本語', emotion: 'gentle', act: {}, audio: { status: 'skipped', chunks: [] } }],
    },
  ];
  h.app.action('log');
  const overlay = h.app.stage.stage.querySelector('.adv-log-overlay');
  const entry = overlay.querySelector('.adv-log-entry');
  assert.equal(entry.querySelector('.adv-log-primary').textContent, '中文内容');
  assert.equal(entry.querySelector('.adv-log-subtitle'), null, 'Subtitle should be hidden when advSubtitleLanguage is none');
});

test('LOG: skipped lines are listed', (t) => {
  const h = setupApp(t);
  const session = h.active();
  session.messages = [
    {
      id: 'm1',
      role: 'assistant',
      ts: h.getWallTime(),
      lines: [
        { index: 0, zh: '第一句（已读）', ja: 'ja0', audio: { status: 'ready', chunks: [{ index: 0, audio_url: '/0.wav' }] } },
        { index: 1, zh: '第二句（已跳过）', ja: 'ja1', audio: { status: 'skipped', chunks: [] } },
        { index: 2, zh: '第三句（已跳过）', ja: 'ja2', audio: { status: 'skipped', chunks: [] } },
      ],
    },
  ];
  h.app.action('log');
  const entries = h.app.stage.stage.querySelectorAll('.adv-log-entry.is-assistant');
  assert.equal(entries.length, 3, 'All 3 lines including skipped ones must be listed');
  assert.equal(entries[0].querySelector('.adv-log-primary').textContent, '第一句（已读）');
  assert.equal(entries[1].querySelector('.adv-log-primary').textContent, '第二句（已跳过）');
  assert.equal(entries[2].querySelector('.adv-log-primary').textContent, '第三句（已跳过）');
});

test('LOG: replay of exactly one line, with any other voice stopped', async (t) => {
  const h = setupApp(t);
  const session = h.active();
  session.messages = [
    {
      id: 'm1',
      role: 'assistant',
      ts: h.getWallTime(),
      lines: [
        { index: 0, zh: '第0句', ja: 'ja0', audio: { status: 'ready', chunks: [{ index: 0, audio_url: '/0.wav' }] } },
        { index: 1, zh: '第1句', ja: 'ja1', audio: { status: 'ready', chunks: [{ index: 1, audio_url: '/1.wav' }] } },
      ],
    },
  ];

  h.app.action('log');
  const entries = h.app.stage.stage.querySelectorAll('.adv-log-entry.is-assistant');
  const replay0 = entries[0].querySelector('.adv-log-replay-btn');
  const replay1 = entries[1].querySelector('.adv-log-replay-btn');

  replay0.click();
  assert.equal(h.audio.played.length, 1);
  assert.equal(h.audio.played[0].index, 0);

  // Clicking replay1 stops replay0 and plays only line 1
  replay1.click();
  assert.ok(h.calls.stoppedOther > 0 || h.audio.stopped.length > 0, 'Starting a replay stops other voices');
  assert.equal(h.audio.played.length, 2);
  assert.equal(h.audio.played[1].index, 1);

  // Closing LOG stops the voice
  h.app.action('close_overlay');
  assert.ok(h.audio.stopped.length > 0);
});

test('LOG: disabled states (no audio, voice off, older than 7 days, failed load)', async (t) => {
  const h = setupApp(t);
  const session = h.active();
  const now = 1700000000000;
  h.setWallTime(now);

  session.messages = [
    {
      id: 'm_no_audio',
      role: 'assistant',
      ts: now,
      lines: [{ index: 0, zh: '无音频', ja: '', audio: { status: 'skipped', chunks: [] } }],
    },
    {
      id: 'm_old',
      role: 'assistant',
      // 8 days ago
      ts: now - 8 * 24 * 60 * 60 * 1000,
      lines: [{ index: 1, zh: '8天前', ja: '', audio: { status: 'ready', chunks: [{ index: 0, audio_url: '/old.wav' }] } }],
    },
    {
      id: 'm_valid',
      role: 'assistant',
      ts: now,
      lines: [{ index: 2, zh: '正常音频', ja: '', audio: { status: 'ready', chunks: [{ index: 0, audio_url: '/ok.wav' }] } }],
    },
  ];

  h.app.action('log');
  let entries = h.app.stage.stage.querySelectorAll('.adv-log-entry.is-assistant');

  // Case 1: no audio
  const btnNoAudio = entries[0].querySelector('.adv-log-replay-btn');
  assert.equal(btnNoAudio.disabled, true, 'No audio should disable replay button');
  assert.equal(btnNoAudio.title, '无语音');

  // Case 2: older than 7 days
  const btnOld = entries[1].querySelector('.adv-log-replay-btn');
  assert.equal(btnOld.disabled, true, 'Older than 7 days should disable replay button');
  assert.ok(btnOld.title.includes('7天'));

  // Case 3: valid audio
  const btnValid = entries[2].querySelector('.adv-log-replay-btn');
  assert.equal(btnValid.disabled, false);

  // Case 4: voice off disables all replay buttons
  h.app.action('close_overlay');
  h.app.action('voice_mode'); // toggle voice mode off
  h.app.action('log');
  entries = h.app.stage.stage.querySelectorAll('.adv-log-entry.is-assistant');
  const btnVoiceOff = entries[2].querySelector('.adv-log-replay-btn');
  assert.equal(btnVoiceOff.disabled, true, 'Voice off disables replay button');
  assert.equal(btnVoiceOff.title, '语音已关闭');

  // Case 5: chunk failed load disables button and shows text
  h.app.action('close_overlay');
  h.app.action('voice_mode'); // toggle voice mode back on
  h.app.action('log');
  entries = h.app.stage.stage.querySelectorAll('.adv-log-entry.is-assistant');
  const btnFailing = entries[2].querySelector('.adv-log-replay-btn');
  btnFailing.click();
  h.endVoice('failed');
  await microtasks();

  assert.equal(btnFailing.disabled, true, 'Failed load must disable replay button');
  assert.equal(btnFailing.title, '语音已过期或不可用');
  const statusSpan = entries[2].querySelector('.adv-log-audio-status');
  assert.equal(statusSpan.textContent, '语音已过期或不可用');
  assert.equal(statusSpan.hidden, false);
});

test('LOG: older paging keeps the scroll position', async (t) => {
  let olderLoaded = false;
  const h = setupApp(t, {
    onLoadOlder: async (session) => {
      olderLoaded = true;
      session.messages.unshift({
        id: 'm_older',
        role: 'user',
        content: '更早的历史消息',
        ts: 1000,
      });
      session.hasMoreHistory = false;
    },
  });

  const session = h.active();
  session.hasMoreHistory = true;
  session.messages = [
    { id: 'm1', role: 'user', content: '新消息1', ts: 2000 },
    { id: 'm2', role: 'user', content: '新消息2', ts: 3000 },
  ];

  h.app.action('log');
  const overlay = h.app.stage.stage.querySelector('.adv-log-overlay');
  const moreBtn = overlay.querySelector('.adv-log-more-btn');
  assert.ok(moreBtn && !moreBtn.hidden, 'More button should be visible when hasMoreHistory is true');
  assert.equal(moreBtn.textContent, '加载更早的记录');

  const listEl = overlay.querySelector('.adv-log-list');
  listEl.scrollHeight = 500;
  listEl.scrollTop = 100;

  moreBtn.click();
  await microtasks();
  assert.equal(olderLoaded, true, 'loadOlderSessionHistory should have been called');

  // Verify older message is now rendered at top
  const entries = overlay.querySelectorAll('.adv-log-entry');
  assert.equal(entries.length, 3);
  assert.equal(entries[0].querySelector('.adv-log-primary').textContent, '更早的历史消息');

  // Verify more button is now hidden since hasMoreHistory became false
  assert.equal(moreBtn.hidden, true);
});

test('LOG: a live turn updates an open LOG through done and through cancelled', (t) => {
  const h = setupApp(t);
  h.app.action('log');
  const overlay = h.app.stage.stage.querySelector('.adv-log-overlay');
  assert.ok(overlay);

  // Turn starts
  h.start('turn_live', '用户实时提问');
  let entries = overlay.querySelectorAll('.adv-log-entry');
  assert.equal(entries.length, 1);
  assert.equal(entries[0].querySelector('.adv-log-primary').textContent, '用户实时提问');

  // Line event arrives
  h.line(0, { zh: '第一句实时台词', ja: 'リアルタイム1' });
  entries = overlay.querySelectorAll('.adv-log-entry');
  assert.equal(entries.length, 2);
  assert.equal(entries[1].querySelector('.adv-log-primary').textContent, '第一句实时台词');

  // Line audio arrives
  const replayBtn = entries[1].querySelector('.adv-log-replay-btn');
  h.audioEvent(0, 'ready', [{ index: 0, audio_url: '/live.wav' }]);
  assert.equal(replayBtn.disabled, false);

  // Done event arrives
  h.done('done');
  entries = overlay.querySelectorAll('.adv-log-entry');
  assert.equal(entries.length, 2);

  // Next turn cancelled
  h.start('turn_cancel', '即将被取消的提问');
  h.line(0, { zh: '部分输出', ja: '' });
  h.done('cancelled');
  entries = overlay.querySelectorAll('.adv-log-entry');
  assert.equal(entries.length, 4, 'Cancelled turn retains delivered user and assistant lines');
});

test('LOG: pause reasons, SKIP cancel and focus return', (t) => {
  const h = setupApp(t);
  h.start('turn_p');
  h.line(0);
  h.audioEvent(0);

  // Start auto and skip
  h.app.action('auto');
  h.app.action('skip');
  assert.equal(h.app.player.skip, true);

  const prevActive = h.doc.createElement('input');
  h.doc.body.appendChild(prevActive);
  prevActive.focus();
  assert.equal(h.doc.activeElement, prevActive);

  // Open LOG
  h.app.action('log');
  assert.equal(h.app.player.skip, false, 'Opening LOG must cancel SKIP');
  assert.equal(h.app.player.state, 'suspended', 'Opening LOG must suspend player under overlay');

  // Close LOG
  h.app.action('close_overlay');
  assert.notEqual(h.app.player.state, 'suspended', 'Closing LOG should resume player');
  assert.equal(h.doc.activeElement, prevActive, 'Focus must return to previous element on close');
});

test('LOG: R, F5 and wheel open it; Esc closes it; wheel-down at bottom closes it', (t) => {
  const h = setupApp(t);
  const stage = h.app.stage.stage;

  // Active dialogue allows letter shortcuts
  h.start('turn_r');
  h.line(0);

  // 1. R key opens it
  stage.dispatchEvent({ type: 'keydown', key: 'r' });
  assert.ok(h.app.backlog, 'R opens LOG');

  // 2. Esc key closes it
  stage.dispatchEvent({ type: 'keydown', key: 'Escape' });
  assert.equal(h.app.backlog, null, 'Esc closes LOG');

  // In input mode, R does not open LOG
  h.app.action('input');
  h.app.textbox.inputElement.dispatchEvent({ type: 'input' });
  stage.dispatchEvent({ type: 'keydown', key: 'r' });
  assert.equal(h.app.backlog, null, 'R is ignored in input mode');

  // 3. F5 with stage focus opens it
  stage.focus();
  stage.dispatchEvent({ type: 'keydown', key: 'F5' });
  assert.ok(h.app.backlog, 'F5 with stage focus opens LOG');
  h.app.action('close_overlay');
  assert.equal(h.app.backlog, null);

  // 4. Wheel-up opens it
  stage.dispatchEvent({ type: 'wheel', deltaY: -100 });
  assert.ok(h.app.backlog, 'Wheel up opens LOG');

  // 5. Wheel-down at bottom closes it
  const listEl = h.app.backlog.listElement;
  listEl.scrollHeight = 200;
  listEl.clientHeight = 100;
  listEl.scrollTop = 100; // at bottom
  listEl.dispatchEvent({ type: 'wheel', deltaY: 50 });
  assert.equal(h.app.backlog, null, 'Wheel down at bottom closes LOG');
});

test('LOG: letter shortcuts are ignored inside editors', () => {
  const inInputCtx = { inInput: true, stageFocused: true, overlayOpen: false };
  assert.equal(decide({ type: 'keydown', key: 'r' }, inInputCtx), null);
  assert.equal(decide({ type: 'keydown', key: 'R' }, inInputCtx), null);
  assert.equal(decide({ type: 'keydown', key: 'a' }, inInputCtx), null);
  assert.equal(decide({ type: 'keydown', key: 'f' }, inInputCtx), null);
  assert.equal(decide({ type: 'keydown', key: 'h' }, inInputCtx), null);
  assert.equal(decide({ type: 'keydown', key: 'x' }, inInputCtx), null);
  assert.equal(decide({ type: 'wheel', deltaY: -100 }, inInputCtx), null);
});

test('LOG: the draft is preserved', (t) => {
  const h = setupApp(t);
  h.app.action('input');
  const input = h.app.textbox.inputElement;
  input.value = '保留的重要草稿';
  input.dispatchEvent({ type: 'input' });

  assert.equal(h.app.textbox.draft, '保留的重要草稿');

  h.app.action('log');
  assert.ok(h.app.backlog);
  assert.equal(h.app.textbox.draft, '保留的重要草稿', 'Draft preserved while LOG is open');

  h.app.action('close_overlay');
  assert.equal(h.app.backlog, null);
  assert.equal(h.app.textbox.draft, '保留的重要草稿', 'Draft preserved after LOG is closed');
  assert.equal(h.doc.activeElement, input, 'Focus returned to textarea');
});

/* -------------------------------------------------------------
 * Part 3 #1: First click after a completed reply
 * -----------------------------------------------------------*/
for (const status of ['failed', 'skipped']) for (const fullyRevealed of [false, true]) {
test(`Part 3 #1: real controller/player, audio ${status}, done ${fullyRevealed ? 'after' : 'during'} reveal`, async () => {
  const session = { id: 's', title: '会话', settings: { ...DEFAULT_SESSION_SETTINGS, advEnabled: true }, messages: [] };
  const state = { activeId: 's', global: { voiceMode: true, ttsPreset: 'balanced' } };
  const stream = fakeChatStream();
  const clock = fakeClock();

  const audioPlayer = {
    isPlaying: false, currentMsgId: null, paused: false,
    startSession() {}, enqueueChunk() {}, finishInput() {}, attachControl() {},
    pause() {}, resume() {}, interrupt() {},
  };

  const conversation = createConversation({
    state, getActive: () => session, getSession: () => session,
    uid: (p) => `${p}_1`, saveState() {}, persistSession: async () => {}, isSessionDeleting: () => false,
    addMessage(role, content) {
      const msg = { id: `m_${session.messages.length + 1}`, role, content, ts: Date.now() };
      session.messages.push(msg);
      return msg;
    },
    audioStore: new Map(), putCachedAudioBlob() {}, fetchAndCacheAudio: async () => {},
    audioPlayer, getCurrentVoice: () => null, stopCurrentVoice() {}, setCurrentVoice() {},
    clearMicTimers() {}, beforeSend() {}, onBlocked() {}, getAudioControl: () => null,
    getAudioMode: () => 'lines', fetch: stream.fetch,
  });

  let enteredInput = false;
  const render = {
    activate() {},
    reveal() {},
    completeReveal() {},
    glyph() {},
    thinking() {},
    enterInput() { enteredInput = true; },
  };

  const voice = {
    play: async () => 'ended',
    stop: () => {},
  };

  const player = createLinePlayer({
    clock,
    voice,
    render,
    settings: {
      revealMsPerGrapheme: 30,
      voiceEnabled: true,
      interruptVoice: true,
    },
  });

  let activeTurnId;
  conversation.on('turnstart', (e) => {
    activeTurnId = e.assistantMessage.id;
    player.startTurn(e.assistantMessage.id);
  });
  conversation.on('line', (e) => {
    player.pushLine(e.messageId, e.line);
  });
  conversation.on('line_audio', (e) => {
    player.setLineAudio(e.messageId, e.line, e);
  });
  conversation.on('done', (e) => {
    // Stored replies receive a new ID; the line player still owns the streaming ID.
    player.settle(activeTurnId, { ok: true });
  });

  // Step 1: send prompt
  conversation.send('提问');
  const req = stream.requests[0];

  // Step 2: line 0 arrives
  await req.event('line', { index: 0, zh: '第一句台词。', ja: 'こんにちは。', emotion: 'gentle', act: {} });
  assert.equal(player.state, 'waiting_audio');

  // Step 3: line_audio arrives as failed/skipped
  await req.event('line_audio', { line: 0, status, chunks: [] });
  assert.equal(player.state, 'revealing');

  // Step 4: Typewriter completes reveal, line 0 is displayed
  if (fullyRevealed) clock.tick(1000);
  assert.equal(player.state, fullyRevealed ? 'waiting_advance' : 'revealing');

  // Step 5: done arrives from server
  await req.onEnd('第一句台词。', { lines: [{ index: 0, zh: '第一句台词。', ja: 'こんにちは。', emotion: 'gentle', act: {}, audio: { status, chunks: [] } }] });
  assert.equal(player.state, fullyRevealed ? 'waiting_advance' : 'revealing');
  assert.equal(player.canSend, true);

  // Step 6: First click!
  player.advance();
  if (!fullyRevealed) {
    assert.equal(player.state, 'waiting_advance');
    assert.equal(enteredInput, false, 'First click completes the reveal');
    player.advance();
  }

  // If reveal was completed before the click, the first click advances directly to input!
  assert.equal(player.state, 'input');
  assert.equal(enteredInput, true);

  player.reset();
});
}

/* -------------------------------------------------------------
 * Part 3 #2: Empty reply defensive handling
 * -----------------------------------------------------------*/
test('Part 3 #2: empty done with no lines and no text shows error with Retry and Settings', (t) => {
  const h = setupApp(t);
  const userMsg = h.start('turn_empty', '这次没有回复');

  // Server sends done with empty reply (0 lines, empty text)
  h.done('done', { message: null });

  assert.notEqual(h.app.player.state, 'input', 'Stage must not be left silently in input mode');
  assert.equal(h.app.player.state, 'error', 'Player must enter error state');

  const errorEl = h.app.stage.stage.querySelector('.adv-error-message');
  assert.equal(errorEl.textContent, '模型这次没有返回任何内容');

  const actionBtns = [...h.app.stage.stage.querySelectorAll('.adv-error-btn')];
  assert.equal(actionBtns.length, 2);
  assert.equal(actionBtns[0].textContent, '重新发送');
  assert.equal(actionBtns[1].textContent, '去设置');

  // Verify retry button delegates to controller.retry
  actionBtns[0].click();
  assert.equal(h.controller.retried.length, 1);
  assert.equal(h.controller.retried[0], userMsg);

  // An empty reply is usually fixed in the model settings (思考强度), so 去设置 opens them.
  actionBtns[1].click();
  assert.equal(h.calls.models, 1);
  assert.equal(h.calls.settings, 0);
});

test('DOM models absent attributes and bubbling through Document', () => {
  const doc = fakeDocument(), button = doc.createElement('button');
  doc.body.appendChild(button);
  assert.equal(button.getAttribute('tabindex'), null);
  assert.equal(button.matches('[tabindex]'), false);
  button.tabIndex = 2;
  assert.equal(button.getAttribute('tabindex'), '2');
  button.removeAttribute('tabindex');
  assert.equal(button.tabIndex, 0);
  assert.equal(button.closest('[contenteditable]'), null);
  let seen;
  doc.addEventListener('click', e => { seen = e.target; });
  button.click();
  assert.equal(seen, button);
});

test('LOG owns pointer and Ctrl events and traps focus around enabled controls', t => {
  const h = setupApp(t); h.start(); h.line(); h.app.action('log');
  const log = h.app.backlog;
  log.listElement.dispatchEvent({ type: 'pointerdown' });
  assert.equal(h.doc.activeElement, log.closeButton);
  log.closeButton.dispatchEvent({ type: 'keydown', key: 'Control' });
  assert.equal(h.app.player.skip, false);
  const last = log.element.querySelector('.adv-log-replay-btn');
  log.closeButton.dispatchEvent({ type: 'keydown', key: 'Tab', shiftKey: true });
  assert.equal(h.doc.activeElement, last);
  last.dispatchEvent({ type: 'keydown', key: 'Tab' });
  assert.equal(h.doc.activeElement, log.closeButton);
});

test('LOG closing during pending paging ignores completion and user playback delegates once', async t => {
  let finish;
  const h = setupApp(t, { onLoadOlder: () => new Promise(resolve => { finish = resolve; }) });
  h.active().hasMoreHistory = true;
  h.active().messages = [{ id: 'u', role: 'user', content: '录音', voiceKey: 'rec' }];
  h.app.action('log');
  const log = h.app.backlog;
  log.element.querySelector('.adv-log-user-recording-btn').click();
  assert.equal(h.calls.playUserVoice.length, 1);
  h.calls.playUserVoice[0].ctl.setPlaying(true);
  log.moreButton.click();
  h.app.action('close_overlay');
  const count = log.listElement.children.length;
  h.active().messages.unshift({ id: 'old', role: 'user', content: '旧记录' });
  finish(); await microtasks();
  assert.equal(log.listElement.children.length, count);
  assert.equal(log.element.querySelector('.adv-log-user-recording-btn').classList.contains('is-playing'), false);
});

test('LOG audio events match both message and line, without mutating history', t => {
  const h = setupApp(t), now = h.getWallTime();
  const line = () => ({ index: 0, zh: '台词', audio: { status: 'pending', chunks: [] } });
  h.active().messages = ['old', 'live'].map(id => ({ id, role: 'assistant', ts: now, lines: [line()] }));
  h.app.action('log');
  h.audioEvent(0, 'ready', [{ index: 0, audio_url: '/live.wav' }], 'live');
  const buttons = h.app.backlog.element.querySelectorAll('.adv-log-replay-btn');
  assert.equal(buttons[0].disabled, true);
  assert.equal(buttons[1].disabled, false);
  assert.equal(h.active().messages[1].lines[0].audio.status, 'pending');
});

test('LOG replay failure: empty chunks remain disabled while existing chunks are playable', async t => {
  const h = setupApp(t);
  h.active().messages = [{ id: 'a', role: 'assistant', ts: h.getWallTime(), lines: [
    { index: 0, zh: '无音频失败', audio: { status: 'failed', chunks: [] } },
    { index: 1, zh: '部分音频失败', audio: { status: 'failed', chunks: [{ audio_url: '/bad.wav' }] } },
    { index: 2, zh: '可播放', audio: { status: 'ready', chunks: [{ audio_url: '/ok.wav' }] } },
  ] }];
  h.app.action('log');
  let buttons = h.app.backlog.element.querySelectorAll('.adv-log-replay-btn');
  assert.equal(buttons[0].disabled, true);
  assert.equal(buttons[1].disabled, false);
  assert.equal(buttons[2].disabled, false);
  buttons[2].click(); h.endVoice('failed'); await microtasks();
  h.app.backlog.renderAll();
  buttons = h.app.backlog.element.querySelectorAll('.adv-log-replay-btn');
  assert.equal(buttons[2].disabled, true);
  assert.equal(buttons[2].getAttribute('aria-label'), '语音已过期或不可用');
});

test('LOG repeated replay ignores the interrupted promise and clears prior playing indicator', async t => {
  const h = setupApp(t); h.start(); h.line(0); h.line(1); h.app.action('log');
  const buttons = h.app.backlog.element.querySelectorAll('.adv-log-replay-btn');
  buttons[0].click(); buttons[0].click(); await microtasks();
  assert.equal(buttons[0].classList.contains('is-playing'), true);
  buttons[1].click(); await microtasks();
  assert.equal(buttons[0].classList.contains('is-playing'), false);
  assert.equal(buttons[1].classList.contains('is-playing'), true);
  h.app.action('close_overlay');
  assert.equal(buttons[1].classList.contains('is-playing'), false);
});

test('LOG paging compensates actual growth and does not discard live entries', async t => {
  const h = setupApp(t, { onLoadOlder: async s => {
    s.messages.unshift({ id: 'older', role: 'user', content: 'older' });
    s.hasMoreHistory = false;
  } });
  h.active().hasMoreHistory = true; h.start(); h.line(); h.app.action('log');
  const list = h.app.backlog.listElement;
  Object.defineProperty(list, 'scrollHeight', { get: () => list.children.length * 100 });
  list.scrollTop = 70;
  const before = list.scrollHeight;
  h.app.backlog.moreButton.click(); await microtasks();
  assert.equal(list.scrollTop, 70 + list.scrollHeight - before);
  assert.equal(list.querySelectorAll('.adv-log-primary').some(e => e.textContent === '你好世界。'), true);
});

for (const kind of ['error', 'cancelled']) test(`LOG keeps dialog focus and overlay pause through ${kind}`, t => {
  const h = setupApp(t); h.start(); h.line(); h.app.action('log');
  const close = h.app.backlog.closeButton;
  h.done(kind, { error: '断开' });
  assert.equal(h.app.player.state, 'suspended');
  assert.equal(h.doc.activeElement, close);
  h.app.player.suspend('window');
  h.app.action('close_overlay');
  assert.equal(h.app.player.state, 'suspended');
  h.app.player.resume('window');
  assert.equal(h.app.player.state, kind === 'error' ? 'error' : 'input');
});

test('LOG keeps focus and an early draft when done arrives during player editing', t => {
  const h = setupApp(t); h.start(); h.line(); h.app.action('input');
  const input = h.app.textbox.inputElement;
  input.value = '未提交的草稿'; input.dispatchEvent({ type: 'input' });
  h.app.action('log'); const log = h.app.backlog;
  h.done();
  assert.equal(h.doc.activeElement === log.closeButton, true);
  assert.equal(h.app.player.state, 'suspended');
  h.app.action('close_overlay');
  assert.equal(h.app.player.state, 'input');
  assert.equal(h.app.textbox.draft, '未提交的草稿');
  assert.equal(h.doc.activeElement === input, true);
});

test('LOG disables the current replay button if a load fails across done reconciliation', async t => {
  const h = setupApp(t); h.start(); h.line(); h.app.action('log');
  h.app.backlog.element.querySelector('.adv-log-replay-btn').click();
  h.done();
  h.endVoice('failed'); await microtasks();
  const button = h.app.backlog.element.querySelector('.adv-log-replay-btn');
  assert.equal(button.disabled, true);
  assert.equal(button.getAttribute('aria-label'), '语音已过期或不可用');
});
