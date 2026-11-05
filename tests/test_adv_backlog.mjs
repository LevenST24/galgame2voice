import assert from 'node:assert/strict';
import test from 'node:test';
import { mountAdv } from '../frontend/src/adv/adv_app.js';
import { createBacklog, resolveLogTexts } from '../frontend/src/adv/backlog.js';
import { StreamAudioController } from '../frontend/src/audio_player.js';
import { createLineAudioRecovery } from '../frontend/src/controllers/line_audio_recovery.js';
import { decide, createShortcuts } from '../frontend/src/adv/shortcuts.js';
import { createConversation } from '../frontend/src/conversation.js';
import { createLinePlayer } from '../frontend/src/adv/line_player.js';
import { clearMemAudioCache } from '../frontend/src/cache.js';
import { initApp } from '../frontend/src/main.js';
import { state as storedState, loadState, DEFAULT_SESSION_SETTINGS, GREETING } from '../frontend/src/store.js';
import { streamAudioController } from '../frontend/src/audio_player.js';
import { initAudioPlayerUi, stopCurrentVoice } from '../frontend/src/controllers/audio_player_ui.js';
import { initVoiceSettings, fetchVoiceProfiles } from '../frontend/src/controllers/voice_settings.js';
import { audioStore } from '../frontend/src/voice.js';
import { fakeDocument, fakeClock, Element, microtasks } from './helpers/adv_dom.mjs';
import { fakeChatStream, tick } from './helpers/chat_stream.mjs';

test('LOG replays legacy assistant audio and failed player recordings through the shared playback service', t => {
  const doc = fakeDocument(), root = doc.createElement('div'); doc.body.appendChild(root);
  const calls = [], now = Date.now();
  const session = { id: 'legacy', messages: [
    { id: 'a', role: 'assistant', content: '旧格式中文', japanese: '日本語', ts: now, audioUrls: ['/audio/cache/old.wav'] },
    { id: 'u', role: 'user', content: '失败录音', dur: 3, voiceKey: 'recording', failed: { error: '离线' } },
  ] };
  let voice = true;
  const log = createBacklog(root, { getSession: () => session, getSettings: () => DEFAULT_SESSION_SETTINGS,
    getCharacter: () => ({ name: '角色' }), voiceEnabled: () => voice, wallNow: () => now,
    stopOtherVoice: () => calls.push('stop'), audio: { stopLine() {} },
    playLegacyVoice: msg => calls.push(msg.id), playUserVoice: msg => calls.push(msg.id),
  });
  t.after(() => log.destroy());
  log.element.querySelector('.adv-log-replay-btn').click(); assert.equal(calls.at(-1), 'a');
  log.element.querySelector('.adv-log-user-recording-btn').click(); assert.equal(calls.at(-1), 'u');
  voice = false; log.renderAll(); assert.equal(log.element.querySelector('.adv-log-replay-btn').disabled, true);
  voice = true; session.messages[0].ts = now - 8 * 24 * 60 * 60 * 1000;
  log.renderAll(); assert.equal(log.element.querySelector('.adv-log-replay-btn').disabled, true);
});

test('LOG provides explicit repair action for legacy assistant message when audio is expired or missing', t => {
  const doc = fakeDocument(), root = doc.createElement('div'); doc.body.appendChild(root);
  const now = Date.now();
  const session = { id: 'legacy_repair', settings: { voiceProfileId: 1 }, messages: [
    { id: 'm_legacy', role: 'assistant', content: '旧格式中文', japanese: '日本語。', ts: now - 8 * 86400000, audioUrls: ['/audio/cache/old.wav'] },
  ] };
  const log = createBacklog(root, {
    getSession: () => session,
    getSettings: () => DEFAULT_SESSION_SETTINGS,
    getCharacter: () => ({ name: '角色' }),
    voiceEnabled: () => true,
    wallNow: () => now,
    playLegacyVoice: () => {},
    getRecoveryVoice: () => ({ id: 1, name: '当前音色' }),
    regenerateLineAudio: async () => {},
  });
  t.after(() => log.destroy());
  const entry = log.element.querySelector('.adv-log-entry.is-legacy');
  assert.ok(entry, 'legacy entry should be rendered');
  const replayBtn = entry.querySelector('.adv-log-replay-btn');
  assert.equal(replayBtn.disabled, true, 'replay button should be disabled when expired');
  const regenerateBtn = entry.querySelector('.adv-log-regenerate-btn');
  assert.ok(regenerateBtn, 'regenerate button should exist for legacy entry');
  assert.equal(regenerateBtn.hidden, false, 'regenerate button should not be hidden');
  assert.equal(regenerateBtn.textContent, '用当前音色重新生成');
});


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

  const audio = options.audio || {
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
    regenerateLineAudio: options.regenerateLineAudio,
    getRecoveryVoice: options.getRecoveryVoice,
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
  assert.equal(entries[0].querySelector('.adv-log-speaker').textContent, '我');
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
  assert.equal(entries[5].querySelectorAll('.adv-error-btn').length, 2, 'Latest failure offers retry and settings');

  h.app.action('close_overlay');
});

for (const [code, opened] of [['PROVIDER_ERROR', 'models'], ['OPENAI_RATE_LIMIT', 'settings']]) {
  test(`LOG: 去设置 for a ${code} failure opens the ${opened === 'models' ? 'model' : 'session'} settings`, (t) => {
    const h = setupApp(t);
    h.active().messages = [
      { id: 'm_f', role: 'user', content: '失败的用户消息', failed: { error: '提供商调用异常', code }, ts: h.getWallTime() },
    ];
    h.app.action('log');
    h.app.stage.stage.querySelector('.adv-log-overlay').querySelectorAll('.adv-error-btn').at(-1).click();
    assert.deepEqual([h.calls.models, h.calls.settings], opened === 'models' ? [1, 0] : [0, 1]);
  });
}

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
  assert.equal(h.app.stage.stage.querySelector('.adv-app-tools').children.find(button => button.dataset.action === 'voice_mode').getAttribute('aria-pressed'), 'false');
  h.app.action('log');
  entries = h.app.stage.stage.querySelectorAll('.adv-log-entry.is-assistant');
  const btnVoiceOff = entries[2].querySelector('.adv-log-replay-btn');
  assert.equal(btnVoiceOff.disabled, true, 'Voice off disables replay button');
  assert.equal(btnVoiceOff.title, '语音已关闭');

  // Case 5: chunk failed load disables button and shows text
  h.app.action('close_overlay');
  h.app.action('voice_mode'); // toggle voice mode back on
  assert.equal(h.app.stage.stage.querySelector('.adv-app-tools').children.find(button => button.dataset.action === 'voice_mode').getAttribute('aria-pressed'), 'true');
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

  // Line event arrives, but the suspended stage has not shown it: LOG keeps it out
  h.line(0, { zh: '第一句实时台词', ja: 'リアルタイム1' });
  entries = overlay.querySelectorAll('.adv-log-entry');
  assert.equal(entries.length, 1, 'A line the stage never showed must not spoil the backlog');

  // Closing LOG lets the stage activate it, and a reopened LOG lists it
  h.app.action('close_overlay');
  assert.equal(h.app.player.currentIndex, 0);
  h.app.action('log');
  const reopened = h.app.stage.stage.querySelector('.adv-log-overlay');
  entries = reopened.querySelectorAll('.adv-log-entry');
  assert.equal(entries.length, 2);
  assert.equal(entries[1].querySelector('.adv-log-primary').textContent, '第一句实时台词');

  // Line audio arrives
  const replayBtn = entries[1].querySelector('.adv-log-replay-btn');
  h.audioEvent(0, 'ready', [{ index: 0, audio_url: '/live.wav' }]);
  assert.equal(replayBtn.disabled, false);

  // Done event arrives
  h.done('done');
  entries = reopened.querySelectorAll('.adv-log-entry');
  assert.equal(entries.length, 2);

  // Next turn cancelled
  h.start('turn_cancel', '即将被取消的提问');
  h.line(0, { zh: '部分输出', ja: '' });
  h.done('cancelled');
  entries = reopened.querySelectorAll('.adv-log-entry');
  assert.equal(entries.length, 4, 'Cancelled turn retains delivered user and assistant lines');
});

test('LOG: the presented turn lists only the lines the stage has shown', (t) => {
  const doc = fakeDocument(), root = doc.createElement('div'); doc.body.appendChild(root);
  const now = 1700000000000;
  const line = (index) => ({
    index, zh: `第${index}句`, ja: '',
    audio: { status: 'ready', chunks: [{ index, audio_url: `/${index}.wav` }] },
  });
  const live = { id: 'live', role: 'assistant', ts: now, lines: [line(0), line(1), line(2)] };
  const other = { id: 'other', role: 'assistant', ts: now, lines: [line(0)] };
  const session = { id: 's', messages: [{ id: 'u', role: 'user', content: '提问', ts: now }, live, other] };

  const listeners = new Map();
  const controller = {
    on(type, fn) {
      if (!listeners.has(type)) listeners.set(type, new Set());
      listeners.get(type).add(fn);
      return () => listeners.get(type)?.delete(fn);
    },
  };
  const emit = (type, event) => { for (const fn of [...listeners.get(type) || []]) fn(event); };

  let hidden = { messageIds: ['live'], after: 0 };
  const log = createBacklog(root, {
    controller, audio: { stopLine() {} }, getSession: () => session,
    getSettings: () => DEFAULT_SESSION_SETTINGS, getCharacter: () => ({ name: '角色' }),
    getHiddenFrom: () => hidden,
  });
  t.after(() => log.destroy());

  const texts = (msgId) => [...log.element.querySelectorAll('.adv-log-entry')]
    .filter(el => el.classList.contains('is-assistant') && el.dataset.msgId === msgId)
    .map(el => el.querySelector('.adv-log-primary').textContent);

  assert.deepEqual(texts('live'), ['第0句'], 'Only the shown line of the presented turn is listed');
  assert.deepEqual(texts('other'), ['第0句'], 'Another message is never filtered');

  emit('line', { messageId: 'live', line: line(2) });
  assert.deepEqual(texts('live'), ['第0句'], 'A hidden live line adds no entry');

  emit('line', { messageId: 'other', line: line(1) });
  assert.deepEqual(texts('other'), ['第0句', '第1句'], 'A line of another message still arrives live');

  hidden = null;
  log.renderAll();
  assert.deepEqual(texts('live'), ['第0句', '第1句', '第2句'], 'Nothing is hidden once the turn stops being presented');
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
    fetch: stream.fetch,
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
  const h = setupApp(t);
  h.active().messages = [{ id: 'a', role: 'assistant', ts: h.getWallTime(), lines: [
    { index: 0, zh: '第0句', ja: '', audio: { status: 'ready', chunks: [{ index: 0, audio_url: '/0.wav' }] } },
    { index: 1, zh: '第1句', ja: '', audio: { status: 'ready', chunks: [{ index: 1, audio_url: '/1.wav' }] } },
  ] }];
  h.app.action('log');
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

test('LOG re-render during line regeneration keeps cancel and shows the result on the rebuilt entry', async t => {
  const doc = fakeDocument();
  const root = doc.createElement('div');
  doc.body.appendChild(root);
  const session = {
    id: 's_regen',
    settings: { voiceProfileId: 7 },
    messages: [{
      id: 'm_1',
      role: 'assistant',
      ts: Date.now(),
      lines: [0, 1].map(index => ({
        index,
        zh: index === 0 ? '第一句' : '早上好',
        ja: index === 0 ? 'いち。' : 'おはよう。',
        emotion: 'neutral',
        act: {},
        audio: { status: 'failed', chunks: [] },
      })),
    }],
  };
  const calls = [];
  let pendingRequest = null;
  let now = Date.now();
  const recover = createLineAudioRecovery({
    getSession: () => session,
    getVoiceProfiles: () => [{ id: 7, name: '角色A' }],
    saveState: () => true,
    request: (url, options) => {
      calls.push(url);
      let settle;
      const promise = new Promise((resolve, reject) => { settle = { resolve, reject }; });
      options.signal.addEventListener('abort', () => {
        settle.reject(new DOMException('操作已取消', 'AbortError'));
      });
      pendingRequest = { ...settle, signal: options.signal };
      return promise;
    },
  });
  const log = createBacklog(root, {
    getSession: () => session,
    getSettings: () => session.settings,
    getCharacter: () => ({ id: 7, name: '角色A' }),
    getRecoveryVoice: () => ({ id: 7, name: '角色A' }),
    voiceEnabled: () => true,
    audio: { stopLine() {}, async playLine() { return 'ended'; } },
    regenerateLineAudio: recover,
    wallNow: () => now,
  });
  t.after(() => log.destroy());

  const entry = lineIndex => [...log.element.querySelectorAll('.adv-log-entry')]
    .find(el => el.dataset.lineIndex === String(lineIndex));
  const button = lineIndex => entry(lineIndex).querySelector('.adv-log-regenerate-btn');
  const status = lineIndex => entry(lineIndex).querySelector('.adv-log-audio-status');
  const click = node => node.onclick({ stopPropagation() {} });

  const inflight = click(button(1));
  await microtasks();
  assert.equal(calls[0], '/api/voice/regenerate-line');
  assert.equal(button(1).textContent, '取消生成');

  log.renderAll();
  assert.equal(button(1).textContent, '取消生成', 'rebuilt entry must still offer cancel');
  assert.equal(button(0).textContent, '用当前音色重新生成');
  assert.equal(status(1).hidden, false);
  assert.equal(status(1).textContent, '正在生成这句语音…');

  const signal = pendingRequest.signal;
  now += 500;
  click(button(1));
  assert.equal(signal.aborted, true);
  assert.equal(calls.length, 1, 'cancel must not post a second repair');
  assert.equal(button(1).textContent, '用当前音色重新生成');
  assert.equal(status(1).textContent, '已取消本次生成，原音频已保留。');
  await inflight;
  assert.equal(session.messages[0].lines[1].audio.status, 'failed');
  assert.equal(status(1).textContent, '已取消本次生成，原音频已保留。');

  const failed = click(button(1));
  await microtasks();
  assert.equal(calls.length, 2);
  log.renderAll();
  pendingRequest.reject(new Error('语音引擎未就绪，请打开运行状态检查'));
  await failed;
  assert.equal(status(1).textContent, '语音引擎未就绪，请打开运行状态检查');
  assert.equal(status(1).hidden, false);
  assert.equal(button(1).textContent, '用当前音色重新生成');
  assert.equal(status(0).textContent, '');
  log.renderAll();
  assert.equal(status(1).textContent, '语音引擎未就绪，请打开运行状态检查');
  assert.equal(button(1).textContent, '用当前音色重新生成');

  const completed = click(button(1));
  await microtasks();
  log.renderAll();
  pendingRequest.resolve({
    session_id: session.id,
    message_id: null,
    line_index: 1,
    voice_profile_id: 7,
    persisted: false,
    audio: {
      status: 'ready',
      generated_at: Date.now(),
      voice_profile_id: 7,
      chunks: [{ index: 0, line: 1, audio_url: '/audio/repair_after_render.wav', sentence: 'おはよう。' }],
    },
  });
  await completed;
  assert.equal(status(1).textContent, '语音已生成，修复记录保存在本浏览器；点语音播放。');
  assert.equal(entry(1).querySelector('.adv-log-replay-btn').disabled, false);
  log.renderAll();
  assert.equal(status(1).textContent, '语音已生成，修复记录保存在本浏览器；点语音播放。');
  assert.equal(entry(1).querySelector('.adv-log-replay-btn').disabled, false);
});

class LineRegenAudioContext {
  state = 'running';
  currentTime = 0;
  destination = {};
  createGain() {
    return {
      gain: { value: 1, setValueAtTime() {}, cancelScheduledValues() {}, linearRampToValueAtTime() {} },
      connect() {}, disconnect() {},
    };
  }
  createBufferSource() {
    return { connect() {}, disconnect() {}, start() {}, stop() {} };
  }
  async decodeAudioData() { return { duration: 1 }; }
  async resume() { this.state = 'running'; }
  async close() { this.state = 'closed'; }
}

test('double-clicking 用当前音色重新生成 does not cancel, and 取消生成 says the old audio was kept', async t => {
  const realFetch = globalThis.fetch;
  const realWindow = globalThis.window;
  const posts = [];
  let repair = null;
  globalThis.window = { AudioContext: LineRegenAudioContext };
  globalThis.fetch = (url, options = {}) => {
    const path = String(url);
    if (path.includes('/api/voice/regenerate-line')) {
      posts.push(path);
      return new Promise((resolve, reject) => {
        const fail = () => reject(new DOMException('操作已取消', 'AbortError'));
        if (options.signal?.aborted) fail();
        else options.signal?.addEventListener('abort', fail, { once: true });
        repair = { resolve, signal: options.signal };
      });
    }
    if (path.includes('/api/backgrounds')) {
      return Promise.resolve(Response.json({ backgrounds: [], works: [] }));
    }
    return Promise.reject(new TypeError(`unexpected ${path}`));
  };
  const player = new StreamAudioController();
  let session = null;
  const recover = createLineAudioRecovery({
    getSession: () => session,
    getVoiceProfiles: () => [{ id: 7, name: '角色A' }],
    saveState: () => true,
  });
  const h = setupApp(t, {
    audio: player,
    regenerateLineAudio: recover,
    getRecoveryVoice: () => ({ id: 7, name: '角色A' }),
  });
  t.after(async () => {
    globalThis.fetch = realFetch;
    if (realWindow === undefined) delete globalThis.window;
    else globalThis.window = realWindow;
    await player.close();
  });

  session = h.active();
  session.settings.voiceProfileId = 7;
  const originalAudio = { status: 'failed', chunks: [] };
  session.messages = [{
    id: 'm_double',
    serverMessageId: 42,
    role: 'assistant',
    ts: h.getWallTime(),
    lines: [{
      index: 0, zh: '早上好', ja: 'おはよう。', emotion: 'neutral', act: {},
      audio: originalAudio,
    }],
  }];
  h.app.action('log');
  const button = () => h.app.backlog.element.querySelector('.adv-log-regenerate-btn');
  const status = () => h.app.backlog.element.querySelector('.adv-log-audio-status');
  assert.equal(button().textContent, '用当前音色重新生成');
  assert.equal(h.audio, player);

  const sameButton = button();
  sameButton.click();
  sameButton.click();
  await microtasks();
  assert.equal(posts.length, 1);
  assert.equal(posts[0].includes('/api/voice/regenerate-line'), true);
  assert.equal(repair.signal.aborted, false,
    `second click aborted the repair; status="${status().textContent}" button="${button().textContent}"`);
  assert.equal(button().textContent, '取消生成');
  assert.equal(status().textContent, '正在生成这句语音…');
  assert.equal(session.messages[0].lines[0].audio, originalAudio);

  h.setWallTime(h.getWallTime() + 500);
  button().click();
  await microtasks();
  assert.equal(repair.signal.aborted, true);
  assert.equal(posts.length, 1, 'cancel must not post a second repair');
  assert.equal(button().textContent, '用当前音色重新生成');
  assert.equal(status().textContent, '已取消本次生成，原音频已保留。');
  assert.equal(status().hidden, false);
  assert.equal(session.messages[0].lines[0].audio, originalAudio);
  assert.equal(session.messages[0].lines[0].audio.status, 'failed');
  h.app.backlog.renderAll();
  assert.equal(status().textContent, '已取消本次生成，原音频已保留。');
  assert.equal(button().textContent, '用当前音色重新生成');
});

test('a fresh LOG line stays replayable when loading fails and disables only confirmed loss', async (t) => {
  const previousWindow = globalThis.window;
  const previousFetch = globalThis.fetch;
  let lastFetched = '';
  let networkAttempts = 0;
  class AudioContextMock {
    state = 'running';
    currentTime = 0;
    destination = {};
    sources = [];
    createGain() {
      return {
        gain: { value: 1, setValueAtTime() {}, cancelScheduledValues() {}, linearRampToValueAtTime() {} },
        connect() {}, disconnect() {},
      };
    }
    createBufferSource() {
      const source = { done: false, connect() {}, disconnect() {}, start() {}, stop() {}, onended: null };
      this.sources.push(source);
      return source;
    }
    async decodeAudioData() {
      if (String(lastFetched).includes('fresh-bad')) throw new DOMException('Unable to decode audio data', 'EncodingError');
      return { duration: 1 };
    }
    async resume() { this.state = 'running'; }
    async close() { this.state = 'closed'; }
  }
  globalThis.window = { AudioContext: AudioContextMock };
  const player = new StreamAudioController({ fetchTimeoutMs: 40 });
  t.after(async () => {
    await player.close();
    clearMemAudioCache();
    globalThis.window = previousWindow;
    globalThis.fetch = previousFetch;
  });
  globalThis.fetch = async (url, options) => {
    const target = String(url);
    lastFetched = target;
    if (target.includes('fresh-net')) {
      networkAttempts += 1;
      if (networkAttempts === 1) throw new TypeError('failed to fetch');
      return new Response(new Uint8Array([1, 2, 3]), { status: 200, headers: { 'Content-Type': 'audio/wav' } });
    }
    if (target.includes('fresh-slow')) {
      await new Promise((_, reject) => {
        const abort = () => reject(new DOMException('播放已取消', 'AbortError'));
        if (options?.signal?.aborted) abort();
        else options?.signal?.addEventListener('abort', abort, { once: true });
      });
    }
    if (target.includes('fresh-404')) return new Response('', { status: 404 });
    if (target.includes('fresh-410')) return new Response('', { status: 410 });
    if (target.includes('fresh-500')) return new Response('', { status: 500 });
    if (target.includes('fresh-bad')) return new Response(new Uint8Array([9, 9, 9]), { status: 200, headers: { 'Content-Type': 'audio/wav' } });
    return new Response(new Uint8Array([1, 2, 3]), { status: 200, headers: { 'Content-Type': 'audio/wav' } });
  };
  const h = setupApp(t, {
    audio: player,
    regenerateLineAudio: async () => { throw new Error('regenerate should stay unused'); },
    getRecoveryVoice: () => ({ id: 1, name: '当前音色' }),
  });
  const now = h.getWallTime();
  const voiced = (index, zh, url) => ({
    index,
    zh,
    ja: 'あたらしいせりふ',
    audio: { status: 'ready', generated_at: now, chunks: [{ index: 0, audio_url: url, sentence: 'あたらしいせりふ' }] },
  });
  h.active().messages = [{
    id: 'fresh',
    role: 'assistant',
    ts: now,
    lines: [
      voiced(0, '网络', '/audio/fresh-net.wav'),
      voiced(1, '超时', '/audio/fresh-slow.wav'),
      voiced(2, '丢失', '/audio/fresh-404.wav'),
      voiced(3, '撤销', '/audio/fresh-410.wav'),
      voiced(4, '损坏', '/audio/fresh-bad.wav'),
      voiced(5, '服务', '/audio/fresh-500.wav'),
    ],
  }];
  h.app.action('log');

  const entry = (label) => {
    const found = [...h.app.backlog.element.querySelectorAll('.adv-log-entry.is-assistant')]
      .find(node => node.querySelector('.adv-log-primary').textContent === label);
    assert.ok(found, label);
    return found;
  };
  const replay = async (label) => {
    const button = entry(label).querySelector('.adv-log-replay-btn');
    await button.onclick({ stopPropagation() {} });
    return entry(label);
  };
  const replayUntilSound = async (label) => {
    const button = entry(label).querySelector('.adv-log-replay-btn');
    const pending = button.onclick({ stopPropagation() {} });
    const deadline = Date.now() + 1000;
    let source;
    while (Date.now() < deadline) {
      source = player.ctx?.sources?.find(item => !item.done && typeof item.onended === 'function');
      if (source) break;
      await new Promise(resolve => setTimeout(resolve, 5));
    }
    assert.ok(source, `${label} did not start playback`);
    source.done = true;
    source.onended();
    await pending;
    return entry(label);
  };

  const network = await replay('网络');
  const networkButton = network.querySelector('.adv-log-replay-btn');
  const networkStatus = network.querySelector('.adv-log-audio-status');
  assert.equal(networkButton.disabled, false, `label="${networkButton.getAttribute('aria-label')}" status="${networkStatus.textContent}"`);
  assert.equal(networkStatus.hidden, false);
  assert.equal(networkStatus.textContent, '音频加载失败，请稍后在 LOG 里再点「语音」重试。');
  assert.equal(network.querySelector('.adv-log-regenerate-btn').hidden, true);
  h.app.backlog.renderAll();
  assert.equal(entry('网络').querySelector('.adv-log-replay-btn').disabled, false);
  assert.equal(entry('网络').querySelector('.adv-log-audio-status').textContent, '音频加载失败，请稍后在 LOG 里再点「语音」重试。');

  const retried = await replayUntilSound('网络');
  assert.equal(networkAttempts, 2);
  assert.equal(retried.querySelector('.adv-log-replay-btn').disabled, false);
  assert.equal(retried.querySelector('.adv-log-audio-status').textContent, '');
  assert.equal(retried.querySelector('.adv-log-audio-status').hidden, true);

  const timedOut = await replay('超时');
  const timeoutButton = timedOut.querySelector('.adv-log-replay-btn');
  assert.equal(timeoutButton.disabled, false, `label="${timeoutButton.getAttribute('aria-label')}" status="${timedOut.querySelector('.adv-log-audio-status').textContent}"`);
  assert.equal(timedOut.querySelector('.adv-log-audio-status').textContent, '音频加载超时，请稍后在 LOG 里再点「语音」重试。');
  assert.equal(timedOut.querySelector('.adv-log-regenerate-btn').hidden, true);

  const unavailable = await replay('服务');
  assert.equal(unavailable.querySelector('.adv-log-replay-btn').disabled, false);
  assert.equal(unavailable.querySelector('.adv-log-audio-status').textContent, '音频加载失败，请稍后在 LOG 里再点「语音」重试。');

  for (const label of ['丢失', '撤销', '损坏']) {
    const gone = await replay(label);
    const button = gone.querySelector('.adv-log-replay-btn');
    assert.equal(button.disabled, true, label);
    assert.equal(button.getAttribute('aria-label'), '语音已过期或不可用');
    assert.equal(gone.querySelector('.adv-log-audio-status').textContent, '语音已过期或不可用');
    assert.equal(gone.querySelector('.adv-log-regenerate-btn').hidden, false);
    assert.equal(gone.querySelector('.adv-log-regenerate-btn').textContent, '用当前音色重新生成');
  }
});

test('LOG legacy repair lifecycle: repair success enables replay button, updates status, and plays canonical audio', async t => {
  const doc = fakeDocument(), root = doc.createElement('div'); doc.body.appendChild(root);
  const now = Date.now();
  const session = { id: 'legacy_flow', settings: { voiceProfileId: 1 }, messages: [
    { id: 'm_leg', serverMessageId: 99, role: 'assistant', content: '旧版中文', japanese: '旧版日本語。', ts: now - 8 * 86400000, audioUrls: ['/audio/cache/old.wav'] },
  ] };
  const played = [];
  const repairResult = {
    persisted: true,
    saved: true,
    audio: { status: 'ready', generated_at: now, voice_profile_id: 1, voice_name: '测试音色',
      chunks: [{ index: 0, line: 0, audio_url: '/audio/repair_leg.wav', sentence: '旧版日本語。' }] },
    line: { index: 0, zh: '旧版中文', ja: '旧版日本語。', emotion: 'neutral', act: {},
      audio: { status: 'ready', generated_at: now, voice_profile_id: 1, voice_name: '测试音色',
        chunks: [{ index: 0, line: 0, audio_url: '/audio/repair_leg.wav', sentence: '旧版日本語。' }] } },
  };
  const log = createBacklog(root, {
    getSession: () => session,
    getSettings: () => DEFAULT_SESSION_SETTINGS,
    getCharacter: () => ({ name: '角色' }),
    voiceEnabled: () => true,
    wallNow: () => now,
    getRecoveryVoice: () => ({ id: 1, name: '测试音色' }),
    regenerateLineAudio: async () => {
      session.messages[0].lines = [repairResult.line];
      return repairResult;
    },
    audio: {
      stopLine() {},
      async playLine(index, chunks) {
        played.push({ index, chunks });
        return 'ended';
      },
    },
  });
  t.after(() => log.destroy());

  const entry = log.element.querySelector('.adv-log-entry.is-legacy');
  assert.ok(entry);
  const replayBtn = entry.querySelector('.adv-log-replay-btn');
  const regenBtn = entry.querySelector('.adv-log-regenerate-btn');
  const statusSpan = entry.querySelector('.adv-log-audio-status');

  assert.equal(replayBtn.disabled, true);
  assert.equal(regenBtn.hidden, false);
  assert.equal(regenBtn.textContent, '用当前音色重新生成');

  await regenBtn.onclick({ stopPropagation() {} });
  assert.equal(replayBtn.disabled, false);
  assert.equal(regenBtn.hidden, true);
  assert.equal(statusSpan.textContent, '语音已生成，点语音播放。');

  await replayBtn.onclick({ stopPropagation() {} });
  assert.equal(played.length, 1);
  assert.equal(played[0].index, 0);
  assert.equal(played[0].chunks[0].audio_url, '/audio/repair_leg.wav');
});

test('LOG legacy repair double-click grace ignores rapid click, and delayed second click cancels with notice', async t => {
  const doc = fakeDocument(), root = doc.createElement('div'); doc.body.appendChild(root);
  let now = 8 * 86400000 + 1000;
  const session = { id: 'legacy_cancel', settings: { voiceProfileId: 1 }, messages: [
    { id: 'm_cancel', role: 'assistant', content: '旧版中文', japanese: '旧版日本語。', ts: 1000, audioUrls: ['/audio/cache/old.wav'] },
  ] };
  let signalRef;
  let resolveGate;
  const gatePromise = new Promise(r => { resolveGate = r; });
  const log = createBacklog(root, {
    getSession: () => session,
    getSettings: () => DEFAULT_SESSION_SETTINGS,
    getCharacter: () => ({ name: '角色' }),
    voiceEnabled: () => true,
    wallNow: () => now,
    getRecoveryVoice: () => ({ id: 1, name: '测试音色' }),
    regenerateLineAudio: async ({ signal }) => {
      signalRef = signal;
      return gatePromise;
    },
  });
  t.after(() => log.destroy());

  const entry = log.element.querySelector('.adv-log-entry.is-legacy');
  const regenBtn = entry.querySelector('.adv-log-regenerate-btn');
  const statusSpan = entry.querySelector('.adv-log-audio-status');

  const pending = regenBtn.onclick({ stopPropagation() {} });
  await microtasks();
  assert.equal(regenBtn.textContent, '取消生成');
  assert.equal(statusSpan.textContent, '正在生成这句语音…');

  // Rapid second click (<500ms) is ignored
  now += 200;
  await regenBtn.onclick({ stopPropagation() {} });
  assert.equal(signalRef.aborted, false);
  assert.equal(regenBtn.textContent, '取消生成');

  // Second click after grace window (>=500ms) cancels
  now += 350;
  await regenBtn.onclick({ stopPropagation() {} });
  assert.equal(signalRef.aborted, true);
  assert.equal(statusSpan.textContent, '已取消本次生成，原音频已保留。');
  assert.equal(session.messages[0].lines, undefined);
});

test('LOG legacy repair validation and guidance: missing voice, missing japanese, overlong japanese', t => {
  const doc = fakeDocument(), root = doc.createElement('div'); doc.body.appendChild(root);
  const now = Date.now();
  let chosen = 0;
  let voice = null;
  const session = { id: 'legacy_val', settings: { voiceProfileId: null }, messages: [
    { id: 'm_no_voice', role: 'assistant', content: '中文', japanese: '日本語。', ts: now - 8 * 86400000 },
    { id: 'm_no_ja', role: 'assistant', content: '中文', japanese: '   ', ts: now - 8 * 86400000 },
    { id: 'm_overlong', role: 'assistant', content: '中文', japanese: 'あ'.repeat(2001), ts: now - 8 * 86400000 },
  ] };
  const log = createBacklog(root, {
    getSession: () => session,
    getSettings: () => DEFAULT_SESSION_SETTINGS,
    getCharacter: () => ({ name: '角色' }),
    voiceEnabled: () => true,
    wallNow: () => now,
    getRecoveryVoice: () => voice,
    onChooseVoice: () => { chosen++; },
    regenerateLineAudio: async () => {},
  });
  t.after(() => log.destroy());

  const entries = log.element.querySelectorAll('.adv-log-entry.is-legacy');

  // Entry 0: No voice selected
  const btn0 = entries[0].querySelector('.adv-log-regenerate-btn');
  assert.equal(btn0.textContent, '选择音色');
  btn0.onclick({ stopPropagation() {} });
  assert.equal(chosen, 1);

  // Now provide voice
  voice = { id: 1, name: '测试音色' };
  log.renderAll();
  const refreshedEntries = log.element.querySelectorAll('.adv-log-entry.is-legacy');

  // Entry 1: Missing Japanese
  const btn1 = refreshedEntries[1].querySelector('.adv-log-regenerate-btn');
  assert.equal(btn1.disabled, true);
  assert.equal(btn1.title, '这句缺少可配音的日文台词');

  // Entry 2: Overlong Japanese (>2000)
  const btn2 = refreshedEntries[2].querySelector('.adv-log-regenerate-btn');
  assert.equal(btn2.disabled, true);
  assert.equal(btn2.title, '这句台词过长，暂时无法单句重新生成');
});

test('LOG legacy repaired line playback outcomes: retryable network and timeout remain replayable; only confirmed failed disables replay', async t => {
  for (const outcome of ['ended', 'failed', 'network', 'timeout']) {
    const doc = fakeDocument(), root = doc.createElement('div'); doc.body.appendChild(root);
    const message = { id: `m_rep_${outcome}`, serverMessageId: 42, role: 'assistant', content: '旧中文', japanese: 'こんにちは。',
      ts: Date.now() - 9 * 86400000, audioUrls: ['/audio/old.wav'] };
    const session = { id: `sess_${outcome}`, settings: { voiceProfileId: 7 }, messages: [message] };
    const audio = { status: 'ready', generated_at: Date.now(), voice_profile_id: 7,
      chunks: [{ index: 0, line: 0, audio_url: '/audio/repair_probe.wav', sentence: message.japanese }] };
    const canonical = { index: 0, zh: message.content, ja: message.japanese, emotion: 'neutral', act: {}, audio };
    const log = createBacklog(root, {
      getSession: () => session, getSettings: () => ({ voiceEnabled: true }), voiceEnabled: () => true,
      getRecoveryVoice: () => ({ id: 7, name: '当前音色' }), playLegacyVoice() {},
      audio: { stopLine() {}, async playLine() { return outcome; } },
      regenerateLineAudio: async () => { message.lines = [canonical]; return { line: canonical, audio, persisted: true, saved: true }; },
    });
    t.after(() => log.destroy());

    await log.element.querySelector('.adv-log-regenerate-btn').onclick({ stopPropagation() {} });
    const replay = log.element.querySelector('.adv-log-replay-btn');
    assert.equal(replay.disabled, false, 'validated repair should be playable');
    await replay.onclick({ stopPropagation() {} });
    assert.equal(replay.disabled, outcome === 'failed', `${outcome} must preserve retryable playback unless confirmed unusable`);
    const statusSpan = log.element.querySelector('.adv-log-audio-status');
    if (outcome === 'network') {
      assert.equal(statusSpan.textContent, '音频加载失败，请稍后在 LOG 里再点「语音」重试。');
    } else if (outcome === 'timeout') {
      assert.equal(statusSpan.textContent, '音频加载超时，请稍后在 LOG 里再点「语音」重试。');
    } else if (outcome === 'failed') {
      assert.equal(statusSpan.textContent, '语音已过期或不可用');
    }
  }
});

test('LOG chunk-only legacy audio remains playable when fresh, and confirmed failure exposes repair action', async t => {
  const doc = fakeDocument(), root = doc.createElement('div'); doc.body.appendChild(root);
  const now = Date.now();
  const message = { id: 'm_chunk_fresh', role: 'assistant', content: '旧中文', japanese: 'こんにちは。', ts: now,
    audioChunks: [{ index: 0, url: '/audio/fresh-chunk.wav', sentence: 'こんにちは。' }] };
  const session = { id: 'chunk-fresh-session', settings: { voiceProfileId: 7 }, messages: [message] };
  let playLineCalled = false;
  let playOutcome = 'failed';
  const log = createBacklog(root, {
    getSession: () => session,
    getSettings: () => ({ voiceEnabled: true }),
    voiceEnabled: () => true,
    wallNow: () => now,
    getRecoveryVoice: () => ({ id: 7, name: '当前音色' }),
    audio: {
      stopLine() {},
      async playLine(index, chunks) {
        playLineCalled = true;
        assert.equal(index, 0);
        assert.equal(chunks[0].url, '/audio/fresh-chunk.wav');
        return playOutcome;
      },
    },
    regenerateLineAudio: async () => {},
  });
  t.after(() => log.destroy());

  const replay = log.element.querySelector('.adv-log-replay-btn');
  const regenBtn = log.element.querySelector('.adv-log-regenerate-btn');
  assert.equal(replay.disabled, false, 'fresh chunk-only legacy audio should be playable initially');
  assert.equal(regenBtn.hidden, true, 'repair button should be hidden while replay is enabled');

  // Trigger playback which returns confirmed 'failed' (e.g. 404 from server)
  await replay.onclick({ stopPropagation() {} });
  assert.equal(playLineCalled, true);
  assert.equal(replay.disabled, true, 'confirmed playback failure disables replay');
  assert.equal(regenBtn.hidden, false, 'repair button becomes visible after confirmed failure');
  assert.equal(regenBtn.textContent, '用当前音色重新生成');
});

test('LOG assistant message with explicit empty lines array preserves text and audio but hides repair button', t => {
  const doc = fakeDocument(), root = doc.createElement('div'); doc.body.appendChild(root);
  const now = Date.now();
  const message = {
    id: 'm_explicit_empty',
    role: 'assistant',
    content: '显式空数组台词',
    japanese: 'セリフ。',
    ts: now - 8 * 86400000,
    lines: [],
    audioUrls: ['/audio/old.wav'],
  };
  const session = { id: 'empty-lines-sess', settings: { voiceProfileId: 7 }, messages: [message] };
  const log = createBacklog(root, {
    getSession: () => session,
    getSettings: () => ({ voiceEnabled: true }),
    voiceEnabled: () => true,
    getRecoveryVoice: () => ({ id: 7, name: '当前音色' }),
    regenerateLineAudio: async () => {},
  });
  t.after(() => log.destroy());

  const entry = log.element.querySelector('.adv-log-entry.is-legacy');
  assert.ok(entry, 'entry is rendered');
  const primary = entry.querySelector('.adv-log-primary');
  assert.equal(primary.textContent, '显式空数组台词');
  const replay = entry.querySelector('.adv-log-replay-btn');
  assert.equal(replay.disabled, true, 'expired replay is disabled');
  const regenBtn = entry.querySelector('.adv-log-regenerate-btn');
  assert.equal(regenBtn.hidden, true, 'repair button must be hidden for explicit empty lines array');
});

test('a line with noVoice flag does not render 语音 or 重新生成 buttons in LOG', () => {
  const doc = fakeDocument(), root = doc.createElement('div'); doc.body.appendChild(root);
  const now = Date.now();
  const session = {
    id: 's_lines',
    settings: { ...DEFAULT_SESSION_SETTINGS },
    messages: [
      {
        id: 'm_split',
        role: 'assistant',
        ts: now,
        lines: [
          { index: 0, zh: '有语音句子', ja: '声あり', audio: { status: 'skipped' } },
          { index: 1, zh: '无语音句子', ja: '声なし', audio: { status: 'skipped' }, noVoice: true },
        ],
      },
      {
        id: 'm_entire_no_voice',
        role: 'assistant',
        ts: now,
        noVoice: true,
        lines: [
          { index: 0, zh: '整条消息无语音', ja: '声なしメッセージ', audio: { status: 'skipped' } },
        ],
      },
    ],
  };

  const log = createBacklog(root, {
    getSession: () => session,
    getSettings: () => DEFAULT_SESSION_SETTINGS,
    getCharacter: () => ({ name: '角色' }),
    voiceEnabled: () => true,
    wallNow: () => now,
    regenerateLineAudio: async () => ({ persisted: true }),
  });

  const findEntry = (msgId, lineIndex) => [...log.element.querySelectorAll('.adv-log-entry')].find(el =>
    el.dataset.msgId === String(msgId) && el.dataset.lineIndex === String(lineIndex)
  );

  const entry0 = findEntry('m_split', 0);
  const entry1 = findEntry('m_split', 1);
  const entryEntire = findEntry('m_entire_no_voice', 0);

  assert.ok(entry0.querySelector('.adv-log-replay-btn'), 'line 0 has replay button');
  assert.ok(entry0.querySelector('.adv-log-regenerate-btn'), 'line 0 has regenerate button');

  assert.equal(entry1.querySelector('.adv-log-replay-btn'), null, 'noVoice line 1 has no replay button');
  assert.equal(entry1.querySelector('.adv-log-regenerate-btn'), null, 'noVoice line 1 has no regenerate button');

  assert.equal(entryEntire.querySelector('.adv-log-replay-btn'), null, 'noVoice msg has no replay button');
  assert.equal(entryEntire.querySelector('.adv-log-regenerate-btn'), null, 'noVoice msg has no regenerate button');

  log.destroy();
});

test('the welcome line, stored without split lines, shows no 语音 or 重新生成 button in LOG', () => {
  const doc = fakeDocument(), root = doc.createElement('div'); doc.body.appendChild(root);
  const now = Date.now();
  const session = {
    id: 's_welcome',
    settings: { ...DEFAULT_SESSION_SETTINGS },
    messages: [
      { id: 'greet', role: 'assistant', content: GREETING, noVoice: true, ts: now },
      { id: 'old_reply', role: 'assistant', content: '旧版回复', japanese: '古い返事', ts: now },
    ],
  };
  const log = createBacklog(root, {
    getSession: () => session,
    getSettings: () => DEFAULT_SESSION_SETTINGS,
    getCharacter: () => ({ name: '角色' }),
    voiceEnabled: () => true,
    wallNow: () => now,
    regenerateLineAudio: async () => ({ persisted: true }),
  });
  const entryOf = msgId => [...log.element.querySelectorAll('.adv-log-entry')].find(el => el.dataset.msgId === msgId);

  const welcome = entryOf('greet');
  assert.ok(welcome, 'the welcome line is listed');
  assert.equal(welcome.querySelector('.adv-log-replay-btn'), null, 'welcome line has no 语音 button');
  assert.equal(welcome.querySelector('.adv-log-regenerate-btn'), null, 'welcome line has no 重新生成 button');

  const reply = entryOf('old_reply');
  assert.ok(reply.querySelector('.adv-log-replay-btn'), 'an old reply keeps its 语音 button');
  assert.equal(reply.querySelector('.adv-log-regenerate-btn').hidden, false, 'an old reply without audio still offers regeneration');

  log.destroy();
});


test('LOG replay of line voice, legacy voice, and user recording keeps an in-flight line regeneration alive while preserving explicit cancel and LOG close', async (t) => {
  const prev = {
    document: globalThis.document,
    window: globalThis.window,
    Audio: globalThis.Audio,
    fetch: globalThis.fetch,
    localStorage: globalThis.localStorage,
  };
  const doc = fakeDocument();
  globalThis.document = doc;

  const contexts = [];
  class AudioContextBoundary {
    state = 'running';
    currentTime = 0;
    destination = {};
    sources = [];
    constructor() {
      contexts.push(this);
    }
    createGain() {
      return {
        gain: { value: 1, setValueAtTime() {}, cancelScheduledValues() {}, linearRampToValueAtTime() {} },
        connect() {},
        disconnect() {},
      };
    }
    createBufferSource() {
      const source = {
        started: false,
        stopped: false,
        connect() {},
        disconnect() {},
        start(time) { this.started = true; this.time = time; },
        stop() { this.stopped = true; },
        finish() { this.onended?.(); },
      };
      this.sources.push(source);
      return source;
    }
    async decodeAudioData() {
      return { duration: 4 };
    }
    async suspend() { this.state = 'suspended'; }
    async resume() { this.state = 'running'; }
    async close() { this.state = 'closed'; }
  }

  const audioElements = [];
  class AudioElementBoundary {
    constructor(src) {
      this.src = src;
      this.paused = true;
      this.duration = 2;
      this.currentTime = 0;
      audioElements.push(this);
    }
    async play() {
      this.paused = false;
    }
    pause() {
      this.paused = true;
    }
  }

  globalThis.window = { innerWidth: 1280, AudioContext: AudioContextBoundary };
  globalThis.Audio = AudioElementBoundary;

  const storage = new Map();
  globalThis.localStorage = {
    getItem: key => (storage.has(key) ? storage.get(key) : null),
    setItem: (key, value) => { storage.set(key, String(value)); },
    removeItem: key => { storage.delete(key); },
  };

  const regenRequests = [];
  const audioFetches = [];
  const wavBlob = () => new Blob([new Uint8Array([1, 2, 3, 4])], { type: 'audio/wav' });
  globalThis.fetch = (url, options = {}) => {
    if (url === '/api/voice/profiles') {
      return Promise.resolve(Response.json({ profiles: [{ id: 7, name: '丛雨' }], active_profile_id: 7 }));
    }
    if (url === '/api/backgrounds') {
      return Promise.resolve(Response.json({ backgrounds: [], works: [] }));
    }
    if (String(url).startsWith('/api/characters/')) {
      return Promise.resolve(Response.json({}));
    }
    if (String(url).startsWith('/audio/')) {
      audioFetches.push(String(url));
      return Promise.resolve(new Response(wavBlob(), { status: 200 }));
    }
    if (url === '/api/voice/regenerate-line') {
      const body = JSON.parse(options.body);
      let resolve, reject;
      const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
      options.signal?.addEventListener('abort', () => {
        reject(new DOMException('aborted', 'AbortError'));
      }, { once: true });
      regenRequests.push({
        body,
        signal: options.signal,
        resolve: payload => resolve(Response.json(payload)),
        reject,
      });
      return promise;
    }
    return Promise.reject(new Error(`Unexpected fetch: ${url}`));
  };

  const now = Date.now();
  const initialSession = {
    id: 's_real_log',
    title: '真实接线会话',
    createdAt: now - 5000,
    updatedAt: now,
    settings: { ...DEFAULT_SESSION_SETTINGS, voiceProfileId: 7 },
    messages: [
      {
        id: 'm_legacy',
        role: 'assistant',
        content: '旧式整段回复。',
        japanese: '昔の返答。',
        audioUrls: ['/audio/cache/legacy.wav'],
        ts: now - 3000,
      },
      {
        id: 'm_user',
        role: 'user',
        content: '带录音的玩家提问',
        dur: 2,
        voiceKey: 'rec_u1',
        ts: now - 2000,
      },
      {
        id: 'm_10',
        serverMessageId: 10,
        role: 'assistant',
        content: '第一句。第二句。第三句。',
        japanese: '一句目。二句目。三句目。',
        ts: now - 1000,
        lines: [
          {
            index: 0,
            zh: '第一句。',
            ja: '一句目。',
            emotion: 'gentle',
            act: {},
            audio: {
              status: 'ready',
              generated_at: now - 1000,
              chunks: [{ index: 0, line: 0, audio_url: '/audio/cache/line0.wav', sentence: '一句目。' }],
            },
          },
          {
            index: 1,
            zh: '第二句。',
            ja: '二句目。',
            emotion: 'gentle',
            act: {},
            audio: { status: 'failed', chunks: [] },
          },
          {
            index: 2,
            zh: '第三句。',
            ja: '三句目。',
            emotion: 'gentle',
            act: {},
            audio: { status: 'failed', chunks: [] },
          },
        ],
      },
    ],
  };

  storage.set('gal2voice.chat.v1', JSON.stringify({
    sessions: [initialSession],
    activeId: initialSession.id,
    global: { voiceMode: true },
  }));
  loadState();
  clearMemAudioCache();
  audioStore.clear();
  audioStore.set('m_user', { url: 'blob:user-rec-1', dur: 2 });
  initAudioPlayerUi({}, {});
  initVoiceSettings({}, {});
  await fetchVoiceProfiles();

  const dom = { advRoot: doc.createElement('div') };
  doc.body.appendChild(dom.advRoot);
  const app = initApp({ initialize: false, dom });
  app.fullRender();

  t.after(async () => {
    app.conversation.stop();
    app.adv?.unmount();
    stopCurrentVoice();
    streamAudioController.stopLine({ fadeMs: 0 });
    streamAudioController.interrupt(0);
    await streamAudioController.close();
    audioStore.clear();
    clearMemAudioCache();
    for (const [k, v] of Object.entries(prev)) {
      if (v === undefined) delete globalThis[k];
      else globalThis[k] = v;
    }
  });

  const flush = async () => {
    for (let i = 0; i < 6; i++) await tick();
  };

  app.adv.action('log');
  const log = app.adv.backlog;
  assert.ok(log, 'LOG overlay mounted through real adv_app');

  const entries = log.element.querySelectorAll('.adv-log-entry');
  assert.equal(entries.length, 5);
  const legacyReplayBtn = entries[0].querySelector('.adv-log-replay-btn');
  const userRecBtn = entries[1].querySelector('.adv-log-user-recording-btn');
  const line0ReplayBtn = entries[2].querySelector('.adv-log-replay-btn');
  const line1ReplayBtn = entries[3].querySelector('.adv-log-replay-btn');
  const line1RegenBtn = entries[3].querySelector('.adv-log-regenerate-btn');
  const line1Status = entries[3].querySelector('.adv-log-audio-status');
  const line2RegenBtn = entries[4].querySelector('.adv-log-regenerate-btn');
  const line2Status = entries[4].querySelector('.adv-log-audio-status');

  const playingButtons = () => [...log.element.querySelectorAll('button')]
    .filter(btn => btn.classList.contains('is-playing'));

  // 1. Start single-line regeneration on Line 1
  line1RegenBtn.click();
  await flush();
  assert.equal(regenRequests.length, 1);
  assert.equal(regenRequests[0].body.line_index, 1);
  assert.equal(regenRequests[0].body.message_id, 10);
  assert.equal(line1RegenBtn.textContent, '取消生成');
  assert.equal(line1RegenBtn.getAttribute('aria-busy'), 'true');
  assert.equal(line1Status.textContent, '正在生成这句语音…');
  assert.equal(line1ReplayBtn.disabled, true);

  // 2. Replay another line's「语音」while Line 1 regeneration is in flight
  line0ReplayBtn.click();
  await flush();
  assert.equal(
    regenRequests[0].signal.aborted,
    false,
    `replaying Line 0 aborted Line 1 regeneration; button="${line1RegenBtn.textContent}" status="${line1Status.textContent}"`
  );
  assert.equal(line1RegenBtn.textContent, '取消生成');
  assert.equal(line1RegenBtn.getAttribute('aria-busy'), 'true');
  assert.equal(line1Status.textContent, '正在生成这句语音…');
  assert.deepEqual(playingButtons(), [line0ReplayBtn]);
  const ctx = contexts[0];
  assert.ok(ctx && ctx.sources.length === 1 && ctx.sources[0].started && !ctx.sources[0].stopped);
  const line0Source = ctx.sources[0];

  // 3. Replay legacy assistant entry's「语音」while Line 1 regeneration is still in flight
  legacyReplayBtn.click();
  await flush();
  await new Promise(r => setTimeout(r, 60));
  assert.ok(streamAudioController._linePlayState, 'starting legacy replay sets active line state');
  assert.equal(streamAudioController._linePlayState.msgId, 'm_legacy', 'starting legacy replay switches active line to m_legacy');
  assert.equal(streamAudioController._linePlayState.lineIndex, 0);
  assert.equal(line0Source.stopped, true, 'starting legacy replay stops Line 0 audio source');
  assert.equal(regenRequests[0].signal.aborted, false, 'legacy replay must not abort Line 1 regeneration');
  assert.equal(line1RegenBtn.textContent, '取消生成');
  assert.equal(line1Status.textContent, '正在生成这句语音…');
  assert.deepEqual(playingButtons(), [legacyReplayBtn]);
  const legacySource = ctx.sources.at(-1);
  assert.ok(legacySource !== line0Source && legacySource.started);

  // 4. Play user message recording「播放录音」while Line 1 regeneration is still in flight
  userRecBtn.click();
  await flush();
  assert.equal(streamAudioController._linePlayState, null, 'starting user recording interrupts legacy playback');
  assert.equal(streamAudioController.isPlaying, false);
  assert.equal(regenRequests[0].signal.aborted, false, 'user recording playback must not abort Line 1 regeneration');
  assert.equal(line1RegenBtn.textContent, '取消生成');
  assert.equal(line1Status.textContent, '正在生成这句语音…');
  assert.deepEqual(playingButtons(), [userRecBtn]);
  assert.equal(audioElements.length, 1);
  assert.equal(audioElements[0].paused, false);

  // 5. Complete Line 1 regeneration while user recording is still playing
  const generatedAt = now + 500;
  regenRequests[0].resolve({
    session_id: initialSession.id,
    message_id: 10,
    line_index: 1,
    voice_profile_id: 7,
    persisted: true,
    audio: {
      status: 'ready',
      generated_at: generatedAt,
      voice_profile_id: 7,
      voice_name: '丛雨',
      chunks: [{ index: 0, line: 1, audio_url: '/audio/repair_line1.wav', sentence: '二句目。' }],
    },
  });
  await flush();

  // User recording keeps playing without button state conflict; Line 1 updates audio, status, and buttons
  assert.equal(audioElements[0].paused, false);
  assert.deepEqual(playingButtons(), [userRecBtn]);
  assert.equal(line1RegenBtn.hidden, true);
  assert.equal(line1RegenBtn.getAttribute('aria-busy'), null);
  assert.equal(line1ReplayBtn.disabled, false);
  assert.equal(line1Status.hidden, false);
  assert.equal(line1Status.textContent, '语音已生成，点语音播放。');
  const activeSession = storedState.sessions.find(s => s.id === initialSession.id);
  assert.equal(activeSession.messages[2].lines[1].audio.status, 'ready');
  assert.equal(activeSession.messages[2].lines[1].audio.chunks[0].audio_url, '/audio/repair_line1.wav');

  // 6. Replaying freshly regenerated Line 1 stops the user recording and plays the repaired audio
  line1ReplayBtn.click();
  await flush();
  assert.equal(audioElements[0].paused, true);
  assert.deepEqual(playingButtons(), [line1ReplayBtn]);
  assert.equal(audioFetches.at(-1), '/audio/repair_line1.wav');
  const line1Source = ctx.sources.at(-1);
  assert.ok(line1Source.started && !line1Source.stopped);

  // 7. Starting and explicitly cancelling Line 2 regeneration («取消生成») keeps Line 1 playback untouched
  line2RegenBtn.click();
  await flush();
  assert.equal(regenRequests.length, 2);
  assert.equal(line2RegenBtn.textContent, '取消生成');
  assert.equal(line1Source.stopped, false, 'starting regeneration must not interrupt active replay');
  assert.deepEqual(playingButtons(), [line1ReplayBtn]);

  await new Promise(r => setTimeout(r, 550));
  line2RegenBtn.click();
  await flush();
  assert.equal(regenRequests[1].signal.aborted, true, 'explicit 取消生成 aborts in-flight regeneration');
  assert.equal(line2RegenBtn.textContent, '用当前音色重新生成');
  assert.equal(line2Status.textContent, '已取消本次生成，原音频已保留。');
  assert.equal(line1Source.stopped, false, 'cancelling regeneration must not interrupt active replay');
  assert.deepEqual(playingButtons(), [line1ReplayBtn]);

  // 8. Closing LOG cancels in-flight regeneration AND stops active replay
  line2RegenBtn.click();
  await flush();
  assert.equal(regenRequests.length, 3);
  assert.equal(regenRequests[2].signal.aborted, false);

  app.adv.action('close_overlay');
  await flush();
  assert.equal(regenRequests[2].signal.aborted, true, 'closing LOG aborts in-flight regeneration');
  assert.equal(streamAudioController._linePlayState, null, 'closing LOG interrupts active replay');
});
