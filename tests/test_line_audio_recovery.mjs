import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createLineAudioRecovery } from '../frontend/src/controllers/line_audio_recovery.js';
import { requestJson } from '../frontend/src/request.js';
import { createBacklog } from '../frontend/src/adv/backlog.js';
import { canReplayLine } from '../frontend/src/adv/replay.js';
import { normalizeLines } from '../frontend/src/session_data.js';
import { createConversation } from '../frontend/src/conversation.js';
import { state, loadState, saveState } from '../frontend/src/store.js';
import { fakeChatStream, tick } from './helpers/chat_stream.mjs';
import { fakeDocument, microtasks } from './helpers/adv_dom.mjs';

const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return { promise, resolve }; };
function fixture(request) {
  const old = Date.now() - 9 * 86400000;
  const message = { id: 'm_local', serverMessageId: 42, role: 'assistant', content: '原中文', japanese: '全文日文', ts: old,
    lines: [0, 1].map(index => ({ index, zh: `原中文${index}`, ja: `日本語${index}。`, emotion: 'neutral', act: {},
      audio: { status: 'failed', chunks: [] } })) };
  const session = { id: 'session', settings: { voiceProfileId: 7 }, messages: [message] };
  let current = session, saves = 0, deleting = false;
  const calls = [];
  const reply = { session_id: session.id, message_id: 42, line_index: 1, voice_profile_id: 7, persisted: true,
    audio: { status: 'ready', generated_at: Date.now(), voice_profile_id: 7, voice_name: '当前角色',
      chunks: [{ index: 0, line: 1, audio_url: '/audio/repair_unique.wav', sentence: '日本語1。' }] } };
  const recover = createLineAudioRecovery({ getSession: () => current, getVoiceProfiles: () => [{ id: 7, name: '当前角色' }],
    saveState: () => { saves++; return true; }, isSessionDeleting: () => deleting,
    request: async (...args) => {
      calls.push(args); const body = JSON.parse(args[1].body);
      return request ? request(reply) : { ...structuredClone(reply), message_id: body.message_id, persisted: body.message_id !== null };
    } });
  return { recover, session, message, calls, reply, get saves() { return saves; },
    switchSession: () => { current = { ...session }; }, deleteSession: () => { deleting = true; } };
}

test('one explicit repair posts only the selected line and keeps original timestamps and other lines', async () => {
  const f = fixture(); const original = structuredClone(f.message);
  const result = await f.recover({ messageId: f.message.id, lineIndex: 1 });
  assert.equal(f.calls.length, 1);
  const [url, options] = f.calls[0]; const body = JSON.parse(options.body);
  assert.equal(url, '/api/voice/regenerate-line'); assert.equal(options.method, 'POST');
  assert.equal(body.text, '日本語1。'); assert.equal(body.voice_profile_id, 7); assert.equal(body.message_id, 42);
  assert.equal(f.message.content, original.content); assert.equal(f.message.ts, original.ts);
  assert.deepEqual(f.message.lines[0], original.lines[0]);
  assert.deepEqual(f.message.lines[1].audio, result.audio); assert.equal(f.saves, 1);
  assert.equal(canReplayLine(f.message.lines[1], f.message, true), true, 'fresh audio on an old message stayed expired');
  assert.deepEqual(normalizeLines(f.message.lines)[1].audio, result.audio);
});

for (const action of ['session', 'profile', 'line', 'deletion', 'abort']) {
  test(`a late repair after ${action} does not change playback history`, async () => {
    const gate = deferred(); const f = fixture(() => gate.promise); const controller = new AbortController();
    const audio = structuredClone(f.message.lines[1].audio);
    const pending = f.recover({ messageId: f.message.id, lineIndex: 1, signal: controller.signal });
    if (action === 'session') f.switchSession();
    if (action === 'profile') f.session.settings.voiceProfileId = 9;
    if (action === 'line') f.message.lines[1].ja = '台词已变';
    if (action === 'deletion') f.deleteSession();
    if (action === 'abort') controller.abort();
    gate.resolve(f.reply); await assert.rejects(pending, { name: 'AbortError' });
    assert.equal(f.saves, 0); assert.deepEqual(f.message.lines[1].audio, audio); assert.equal(f.calls.length, 1);
  });
}

test('an unbound session does not silently use the globally available voice', async () => {
  const f = fixture(); f.session.settings.voiceProfileId = null;
  await assert.rejects(f.recover({ messageId: f.message.id, lineIndex: 1 }), /选择当前音色/);
  assert.equal(f.calls.length, 0);
});

test('an uncertain POST is never automatically repeated', async () => {
  const f = fixture(() => { const error = new Error('timeout'); error.code = 'REQUEST_TIMEOUT'; throw error; });
  await assert.rejects(f.recover({ messageId: f.message.id, lineIndex: 1 }), /结果尚未确认.*原音频/);
  assert.equal(f.calls.length, 1); assert.equal(f.saves, 0);
});

test('a slow repair can finish beyond the old 60 second deadline', async () => {
  const realTimer = globalThis.setTimeout, realFetch = globalThis.fetch;
  const f = fixture(); let deadline, aborted = false, posts = 0;
  globalThis.setTimeout = (fn, ms, ...args) => {
    deadline = ms;
    return realTimer(fn, ms === 60000 ? 10 : ms, ...args);
  };
  globalThis.fetch = (_url, options) => new Promise((resolve, reject) => {
    posts++;
    options.signal.addEventListener('abort', () => { aborted = true; reject(new DOMException('aborted', 'AbortError')); });
    realTimer(() => resolve(new Response(JSON.stringify(f.reply), { headers: { 'Content-Type': 'application/json' } })), 40);
  });
  const recover = createLineAudioRecovery({ getSession: () => f.session, getVoiceProfiles: () => [{ id: 7 }],
    saveState: () => true, request: requestJson });
  try {
    await recover({ messageId: f.message.id, lineIndex: 1 });
    assert.equal(deadline, 600000);
    assert.equal(aborted, false);
    assert.equal(posts, 1);
    assert.equal(f.message.lines[1].audio.status, 'ready');
  } finally {
    globalThis.setTimeout = realTimer; globalThis.fetch = realFetch;
  }
});

test('a mismatched repair response preserves the old reference', async () => {
  const f = fixture(reply => ({ ...reply, voice_profile_id: 9 }));
  await assert.rejects(f.recover({ messageId: f.message.id, lineIndex: 1 }), /原音频记录已保留/);
  assert.equal(f.saves, 0); assert.equal(f.message.lines[1].audio.status, 'failed');
});

function backlogFixture(t, recover) {
  const f = fixture(), doc = fakeDocument(), root = doc.createElement('div'); doc.body.appendChild(root);
  const played = []; let chosen = 0;
  const log = createBacklog(root, { getSession: () => f.session, getSettings: () => f.session.settings,
    getCharacter: () => ({ id: 7, name: '当前角色' }), getRecoveryVoice: () => f.session.settings.voiceProfileId ? { id: 7, name: '当前角色' } : null,
    regenerateLineAudio: recover || f.recover, onChooseVoice: () => { chosen++; }, voiceEnabled: () => true,
    audio: { stopLine() {}, async playLine(index, chunks) { played.push(chunks); return 'ended'; } },
  });
  t.after(() => log.destroy());
  return { ...f, log, root, doc, played, get chosen() { return chosen; } };
}

test('LOG offers a named current-voice repair and enables fresh playback on an old message', async t => {
  const f = backlogFixture(t);
  const entries = f.log.element.querySelectorAll('.adv-log-entry');
  const button = entries[1].querySelector('.adv-log-regenerate-btn');
  assert.ok(button, 'an unavailable line has no recovery action');
  assert.match(button.title, /当前角色/);
  await button.onclick({ stopPropagation() {} });
  const replay = entries[1].querySelector('.adv-log-replay-btn');
  assert.equal(replay.disabled, false);
  assert.equal(f.played.length, 0, 'generation must not unexpectedly start playback after a long request');
  await replay.onclick({ stopPropagation() {} });
  assert.equal(f.played[0][0].audio_url, '/audio/repair_unique.wav');
});

test('closing LOG aborts a pending repair and leaves the old line intact', async t => {
  const gate = deferred(); let signal;
  const f = backlogFixture(t, request => { signal = request.signal; return gate.promise; });
  const button = f.log.element.querySelectorAll('.adv-log-regenerate-btn')[1];
  assert.ok(button);
  const pending = button.onclick({ stopPropagation() {} }); await microtasks();
  assert.equal(button.textContent, '取消生成');
  f.log.destroy(); assert.equal(signal.aborted, true);
  gate.resolve({ audio: f.reply.audio, persisted: true, saved: true }); await pending;
  assert.equal(f.message.lines[1].audio.status, 'failed');
});

test('an unbound LOG line offers voice selection instead of guessing a voice', async t => {
  const f = backlogFixture(t); f.session.settings.voiceProfileId = null; f.log.renderAll();
  const button = f.log.element.querySelector('.adv-log-regenerate-btn');
  assert.ok(button); assert.equal(button.textContent, '选择音色');
  await button.onclick({ stopPropagation() {} }); assert.equal(f.chosen, 1);
});


test('a browser-only imported ID is not mistaken for a server message number', async () => {
  const f = fixture(); delete f.message.serverMessageId; f.message.id = 'm_42';
  await f.recover({ messageId: f.message.id, lineIndex: 1 });
  assert.equal(JSON.parse(f.calls[0][1].body).message_id, null);
});

test('a server-persisted LOG repair does not suggest exporting a chat backup when local save fails', async (t) => {
  const storage = new Map();
  const previousStorage = globalThis.localStorage;
  globalThis.localStorage = {
    getItem: key => (storage.has(key) ? storage.get(key) : null),
    setItem: (key, value) => { storage.set(key, String(value)); },
    removeItem: key => { storage.delete(key); },
  };
  const doc = fakeDocument();
  const root = doc.createElement('div');
  doc.body.appendChild(root);
  const logs = [];
  t.after(() => {
    for (const log of logs) log.destroy();
    globalThis.localStorage = previousStorage;
  });

  const session = {
    id: 's_sync',
    title: '持久会话',
    createdAt: 1000,
    updatedAt: 1000,
    settings: { voiceProfileId: 1 },
    messages: [{
      id: 'm_1',
      serverMessageId: 42,
      role: 'assistant',
      content: '你好',
      japanese: 'こんにちは',
      ts: Date.now(),
      lines: [{ index: 0, zh: '你好', ja: 'こんにちは', emotion: 'gentle', act: {}, audio: { status: 'failed', chunks: [] } }],
    }],
  };
  storage.set('gal2voice.chat.v1', JSON.stringify({
    sessions: [session], activeId: session.id, global: { voiceMode: true },
  }));
  loadState();
  const loaded = state.sessions.find(item => item.id === session.id);
  const diverged = JSON.parse(storage.get('gal2voice.chat.v1'));
  diverged.global.voiceMode = false;
  storage.set('gal2voice.chat.v1', JSON.stringify(diverged));
  assert.equal(saveState(), false);

  const calls = [];
  const repairReply = body => ({
    session_id: body.session_id,
    message_id: body.message_id,
    line_index: body.line_index,
    voice_profile_id: body.voice_profile_id,
    persisted: body.message_id !== null,
    audio: {
      status: 'ready',
      generated_at: Date.now(),
      voice_profile_id: body.voice_profile_id,
      voice_name: '角色A',
      chunks: [{ index: 0, line: body.line_index, audio_url: '/audio/repair_persisted01.wav', sentence: body.text }],
    },
  });
  const recover = createLineAudioRecovery({
    getSession: () => loaded,
    getVoiceProfiles: () => [{ id: 1, name: '角色A' }],
    saveState,
    request: async (url, options) => {
      calls.push({ url, body: JSON.parse(options.body) });
      return repairReply(calls.at(-1).body);
    },
  });
  const openLog = current => {
    const log = createBacklog(root, {
      getSession: () => current,
      getSettings: () => current.settings,
      getCharacter: () => ({ id: 1, name: '角色A' }),
      getRecoveryVoice: () => ({ id: 1, name: '角色A' }),
      regenerateLineAudio: recover,
      voiceEnabled: () => true,
      audio: { stopLine() {}, async playLine() { return 'ended'; } },
    });
    logs.push(log);
    return log;
  };

  const persistedLog = openLog(loaded);
  await persistedLog.element.querySelector('.adv-log-regenerate-btn').onclick({ stopPropagation() {} });
  const persistedStatus = persistedLog.element.querySelector('.adv-log-audio-status').textContent;
  assert.equal(calls[0].url, '/api/voice/regenerate-line');
  assert.equal(calls[0].body.message_id, 42);
  assert.equal(loaded.messages[0].lines[0].audio.chunks[0].audio_url, '/audio/repair_persisted01.wav');
  assert.equal(storage.get('gal2voice.chat.v1').includes('/audio/repair_persisted01.wav'), false);
  assert.equal(persistedStatus, '语音已生成；浏览器记录未更新，点语音播放。');
  assert.doesNotMatch(persistedStatus, /备份|导出/);

  const browserOnly = {
    id: 's_browser',
    settings: { voiceProfileId: 1 },
    messages: [{
      id: 'm_local',
      role: 'assistant',
      content: '仅浏览器',
      ts: Date.now(),
      lines: [{ index: 0, zh: '仅浏览器', ja: 'ブラウザだけ', emotion: 'gentle', act: {}, audio: { status: 'failed', chunks: [] } }],
    }],
  };
  const browserRecover = createLineAudioRecovery({
    getSession: () => browserOnly,
    getVoiceProfiles: () => [{ id: 1, name: '角色A' }],
    saveState: () => false,
    request: async (url, options) => {
      calls.push({ url, body: JSON.parse(options.body) });
      return repairReply(calls.at(-1).body);
    },
  });
  const browserLogLive = createBacklog(root, {
    getSession: () => browserOnly,
    getSettings: () => browserOnly.settings,
    getCharacter: () => ({ id: 1, name: '角色A' }),
    getRecoveryVoice: () => ({ id: 1, name: '角色A' }),
    regenerateLineAudio: browserRecover,
    voiceEnabled: () => true,
    audio: { stopLine() {}, async playLine() { return 'ended'; } },
  });
  logs.push(browserLogLive);
  await browserLogLive.element.querySelector('.adv-log-regenerate-btn').onclick({ stopPropagation() {} });
  const browserStatus = browserLogLive.element.querySelector('.adv-log-audio-status').textContent;
  assert.equal(calls.at(-1).body.message_id, null);
  assert.equal(browserStatus, '语音已生成，但保存失败。请到「设置中心 → 备份与更新」导出聊天备份。');
});

test('a completed SSE reply carries its real database ID into a subsequent repair', async () => {
  const f = fixture(), stream = fakeChatStream(); f.session.messages = [];
  let sequence = 0;
  const conversation = createConversation({
    state: { activeId: f.session.id, global: { voiceMode: false } },
    getActive: () => f.session, getSession: () => f.session, saveState() {},
    uid: prefix => `${prefix}_local_${++sequence}`,
    addMessage(role, content) { const message = { id: `local_${++sequence}`, role, content, ts: Date.now() }; f.session.messages.push(message); return message; },
    persistSession: async () => {}, isSessionDeleting: () => false, audioStore: new Map(), putCachedAudioBlob() {},
    fetchAndCacheAudio: async () => {}, audioPlayer: { interrupt() {} }, getCurrentVoice: () => null, stopCurrentVoice() {},
    fetch: stream.fetch,
  });
  assert.equal(conversation.send('提问'), true); await tick();
  await stream.requests[0].onEnd('中文回答', { message_id: 88, japanese: '日本語0。日本語1。', lines: fixture().message.lines });
  for (let i = 0; i < 10 && conversation.busy; i++) await tick();
  assert.equal(conversation.busy, false);
  const message = f.session.messages.find(item => item.role === 'assistant');
  assert.equal(message.serverMessageId, 88);
  await f.recover({ messageId: message.id, lineIndex: 1 });
  assert.equal(JSON.parse(f.calls[0][1].body).message_id, 88);
});

test('when another tab already repaired a line, receiving 409 fetches the latest audio and updates LOG without asking to reopen', async (t) => {
  const calls = [];
  const remoteRepairedAudio = {
    status: 'ready',
    generated_at: Date.now(),
    voice_profile_id: 7,
    voice_name: '当前角色',
    chunks: [{ index: 0, line: 1, audio_url: '/audio/repair_tab1.wav', sentence: '日本語1。' }],
  };

  const recover = createLineAudioRecovery({
    getSession: () => f.session,
    getVoiceProfiles: () => [{ id: 7, name: '当前角色' }],
    saveState: () => { f.savesCount++; return true; },
    request: async (url, options = {}) => {
      calls.push({ url, options });
      if (url === '/api/voice/regenerate-line') {
        const error = new Error('这句已有其他音频修复结果，未覆盖现有音频，请重新打开 LOG 检查。');
        error.status = 409;
        throw error;
      }
      if (url.startsWith('/api/chat/history')) {
        return {
          session_id: f.session.id,
          count: 1,
          has_more: false,
          oldest_id: 42,
          messages: [{
            id: 42,
            role: 'assistant',
            lines: [
              { index: 0, zh: '原中文0', ja: '日本語0。', emotion: 'neutral', act: {}, audio: { status: 'failed', chunks: [] } },
              { index: 1, zh: '原中文1', ja: '日本語1。', emotion: 'neutral', act: {}, audio: remoteRepairedAudio },
            ],
          }],
        };
      }
      throw new Error(`Unexpected request: ${url}`);
    },
  });

  const f = backlogFixture(t, recover);
  f.savesCount = 0;
  const entries = f.log.element.querySelectorAll('.adv-log-entry');
  const regenBtn = entries[1].querySelector('.adv-log-regenerate-btn');
  const replayBtn = entries[1].querySelector('.adv-log-replay-btn');
  const statusSpan = entries[1].querySelector('.adv-log-audio-status');

  assert.equal(replayBtn.disabled, true);
  await regenBtn.onclick({ stopPropagation() {} });

  assert.equal(calls.length, 2, 'expected exactly one POST and one GET');
  assert.equal(calls[0].url, '/api/voice/regenerate-line');
  assert.equal(calls[1].url, '/api/chat/history?session_id=session&limit=1&before_id=43');
  assert.equal(f.message.lines[1].audio.status, 'ready');
  assert.equal(f.message.lines[1].audio.chunks[0].audio_url, '/audio/repair_tab1.wav');
  assert.equal(statusSpan.textContent, '这一句已在另一网页修好，已更新');
  assert.equal(replayBtn.disabled, false);
  assert.equal(regenBtn.hidden, true);

  await replayBtn.onclick({ stopPropagation() {} });
  assert.equal(f.played.length, 1);
  assert.equal(f.played[0][0].audio_url, '/audio/repair_tab1.wav');

  // Reopen LOG and ensure repair is not requested again
  f.log.destroy();
  const log2 = createBacklog(f.root, {
    getSession: () => f.session,
    getSettings: () => f.session.settings,
    getCharacter: () => ({ id: 7, name: '当前角色' }),
    getRecoveryVoice: () => ({ id: 7, name: '当前角色' }),
    regenerateLineAudio: recover,
    voiceEnabled: () => true,
    audio: { stopLine() {}, async playLine(index, chunks) { f.played.push(chunks); return 'ended'; } },
  });
  t.after(() => log2.destroy());

  const entries2 = log2.element.querySelectorAll('.adv-log-entry');
  const regenBtn2 = entries2[1].querySelector('.adv-log-regenerate-btn');
  const replayBtn2 = entries2[1].querySelector('.adv-log-replay-btn');
  assert.equal(replayBtn2.disabled, false);
  assert.equal(regenBtn2.hidden, true);
  assert.equal(calls.length, 2, 'no extra synthesis requests after reopening');
});

test('when remote repair succeeds but saveState returns false, surfaces local cache failure while allowing playback', async (t) => {
  const calls = [];
  const remoteRepairedAudio = {
    status: 'ready',
    generated_at: Date.now(),
    voice_profile_id: 7,
    voice_name: '当前角色',
    chunks: [{ index: 0, line: 1, audio_url: '/audio/repair_tab_unsaved.wav', sentence: '日本語1。' }],
  };

  const recover = createLineAudioRecovery({
    getSession: () => f.session,
    getVoiceProfiles: () => [{ id: 7, name: '当前角色' }],
    saveState: () => false,
    request: async (url, options = {}) => {
      calls.push({ url, options });
      if (url === '/api/voice/regenerate-line') {
        const error = new Error('这句已有其他音频修复结果，未覆盖现有音频，请重新打开 LOG 检查。');
        error.status = 409;
        throw error;
      }
      if (url.startsWith('/api/chat/history')) {
        return {
          session_id: f.session.id,
          count: 1,
          messages: [{
            id: 42,
            role: 'assistant',
            lines: [
              { index: 0, zh: '原中文0', ja: '日本語0。', emotion: 'neutral', act: {}, audio: { status: 'failed', chunks: [] } },
              { index: 1, zh: '原中文1', ja: '日本語1。', emotion: 'neutral', act: {}, audio: remoteRepairedAudio },
            ],
          }],
        };
      }
      throw new Error(`Unexpected request: ${url}`);
    },
  });

  const f = backlogFixture(t, recover);
  const entries = f.log.element.querySelectorAll('.adv-log-entry');
  const regenBtn = entries[1].querySelector('.adv-log-regenerate-btn');
  const replayBtn = entries[1].querySelector('.adv-log-replay-btn');
  const statusSpan = entries[1].querySelector('.adv-log-audio-status');

  await regenBtn.onclick({ stopPropagation() {} });

  assert.equal(f.message.lines[1].audio.status, 'ready');
  assert.equal(statusSpan.textContent, '这一句已在另一网页修好；本地缓存未更新');
  assert.equal(replayBtn.disabled, false);
  assert.equal(regenBtn.hidden, true);

  await replayBtn.onclick({ stopPropagation() {} });
  assert.equal(f.played.length, 1);
  assert.equal(f.played[0][0].audio_url, '/audio/repair_tab_unsaved.wav');
});

function makeValidRemoteSnapshot(sessionId, messageId, lineIndex, text, profileId) {
  const audio = {
    status: 'ready',
    generated_at: Date.now(),
    voice_profile_id: profileId,
    voice_name: '当前角色',
    chunks: [{ index: 0, line: lineIndex, sentence: text, audio_url: '/audio/repair_valid.wav' }],
  };
  const message = {
    id: messageId,
    role: 'assistant',
    lines: [
      { index: 0, zh: '原中文0', ja: '日本語0。', emotion: 'neutral', act: {}, audio: { status: 'failed', chunks: [] } },
      { index: lineIndex, zh: '原中文1', ja: text, emotion: 'neutral', act: {}, audio },
    ],
  };
  return { session_id: sessionId, messages: [message] };
}

const invalidRemoteCases = [
  { name: 'history fetch network error', mutate: () => ({ error: new Error('网络连接中断') }) },
  { name: 'missing session echo', mutate: base => { delete base.session_id; return { data: base }; } },
  { name: 'mismatched session_id', mutate: base => { base.session_id = 'other_session'; return { data: base }; } },
  { name: 'history returns no messages', mutate: base => { base.messages = []; return { data: base }; } },
  { name: 'wrong message id', mutate: base => { base.messages[0].id = 99; return { data: base }; } },
  { name: 'conflicting id and serverMessageId', mutate: base => { base.messages[0].id = 99; base.messages[0].serverMessageId = 42; return { data: base }; } },
  { name: 'wrong message role', mutate: base => { base.messages[0].role = 'user'; return { data: base }; } },
  { name: 'requested line missing', mutate: base => { base.messages[0].lines = [base.messages[0].lines[0]]; return { data: base }; } },
  { name: 'remote line text changed', mutate: base => { base.messages[0].lines[1].ja = '台词已变化。'; return { data: base }; } },
  { name: 'remote audio status not ready', mutate: base => { base.messages[0].lines[1].audio.status = 'failed'; return { data: base }; } },
  { name: 'remote voice profile id mismatch', mutate: base => { base.messages[0].lines[1].audio.voice_profile_id = 999; return { data: base }; } },
  { name: 'remote audio chunks empty', mutate: base => { base.messages[0].lines[1].audio.chunks = []; return { data: base }; } },
  { name: 'duplicate repair chunk', mutate: base => { base.messages[0].lines[1].audio.chunks.push({ ...base.messages[0].lines[1].audio.chunks[0], index: 1 }); return { data: base }; } },
  { name: 'remote chunk audio_url does not start with repair prefix', mutate: base => { base.messages[0].lines[1].audio.chunks[0].audio_url = '/audio/stream_0.wav'; return { data: base }; } },
  { name: 'missing generated_at timestamp', mutate: base => { delete base.messages[0].lines[1].audio.generated_at; return { data: base }; } },
];

for (const tc of invalidRemoteCases) {
  test(`409 when ${tc.name} preserves original error and keeps old audio`, async () => {
    let saves = 0;
    const calls = [];
    const message = {
      id: 'm_local_inv',
      serverMessageId: 42,
      role: 'assistant',
      content: '原中文',
      japanese: '全文日文',
      ts: Date.now() - 3600000,
      lines: [
        { index: 0, zh: '原中文0', ja: '日本語0。', emotion: 'neutral', act: {}, audio: { status: 'failed', chunks: [] } },
        { index: 1, zh: '原中文1', ja: '日本語1。', emotion: 'neutral', act: {}, audio: { status: 'failed', chunks: [] } },
      ],
    };
    const session = { id: 'session_inv', settings: { voiceProfileId: 7 }, messages: [message] };

    const recover = createLineAudioRecovery({
      getSession: () => session,
      getVoiceProfiles: () => [{ id: 7, name: '当前角色' }],
      saveState: () => { saves++; return true; },
      request: async (url, options = {}) => {
        calls.push({ url, options });
        if (url === '/api/voice/regenerate-line') {
          const err = new Error('这句已有其他音频修复结果，未覆盖现有音频，请重新打开 LOG 检查。');
          err.status = 409;
          throw err;
        }
        if (url.startsWith('/api/chat/history')) {
          const valid = makeValidRemoteSnapshot(session.id, 42, 1, '日本語1。', 7);
          const outcome = tc.mutate(structuredClone(valid));
          if (outcome.error) throw outcome.error;
          return outcome.data;
        }
        throw new Error(`Unexpected ${url}`);
      },
    });

    await assert.rejects(
      recover({ messageId: message.id, lineIndex: 1 }),
      /这句已有其他音频修复结果/
    );
    assert.equal(message.lines[1].audio.status, 'failed');
    assert.equal(saves, 0);
    assert.equal(calls.filter(c => c.url === '/api/voice/regenerate-line').length, 1, 'never retry synthesis');
  });
}

test('409 on an unpersisted local line does not make a history GET and surfaces original 409 error', async () => {
  const calls = [];
  const message = {
    id: 'm_local_unpersisted',
    role: 'assistant',
    content: '本地未保存',
    ts: Date.now(),
    lines: [{ index: 0, zh: '本地', ja: 'ローカル', emotion: 'neutral', act: {}, audio: { status: 'failed', chunks: [] } }],
  };
  const session = { id: 'session_unpersisted', settings: { voiceProfileId: 7 }, messages: [message] };
  const recover = createLineAudioRecovery({
    getSession: () => session,
    getVoiceProfiles: () => [{ id: 7, name: '当前角色' }],
    saveState: () => true,
    request: async (url, options = {}) => {
      calls.push({ url, options });
      if (url === '/api/voice/regenerate-line') {
        const err = new Error('单句冲突');
        err.status = 409;
        throw err;
      }
      throw new Error(`Unexpected ${url}`);
    },
  });

  await assert.rejects(
    recover({ messageId: message.id, lineIndex: 0 }),
    /单句冲突/
  );
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, '/api/voice/regenerate-line');
});

const lateChangeCases = [
  'abort',
  'session',
  'profile',
  'line',
  'deletion',
  'turnConflict',
  'serverMessageId',
  'serverUserMessageId',
];

for (const change of lateChangeCases) {
  test(`409 when ${change} occurs during history GET throws AbortError and discards remote repair`, async () => {
    let saves = 0;
    let deleting = false;
    let currentSession;
    const controller = new AbortController();
    const gate = deferred();
    const calls = [];

    const message = {
      id: 'm_local_late',
      serverMessageId: 42,
      serverUserMessageId: 10,
      role: 'assistant',
      content: '原中文',
      japanese: '全文日文',
      ts: Date.now() - 3600000,
      lines: [{ index: 0, zh: '原中文', ja: '日本語。', emotion: 'neutral', act: {}, audio: { status: 'failed', chunks: [] } }],
    };
    const session = { id: 'session_late', settings: { voiceProfileId: 7 }, messages: [message] };
    currentSession = session;

    const recover = createLineAudioRecovery({
      getSession: () => currentSession,
      getVoiceProfiles: () => [{ id: 7, name: '当前角色' }, { id: 9, name: '新角色' }],
      saveState: () => { saves++; return true; },
      isSessionDeleting: () => deleting,
      request: async (url, options = {}) => {
        calls.push({ url, options });
        if (url === '/api/voice/regenerate-line') {
          const err = new Error('这句已有其他音频修复结果，未覆盖现有音频，请重新打开 LOG 检查。');
          err.status = 409;
          throw err;
        }
        if (url.startsWith('/api/chat/history')) {
          return gate.promise;
        }
        throw new Error(`Unexpected ${url}`);
      },
    });

    const pending = recover({ messageId: message.id, lineIndex: 0, signal: controller.signal });
    await tick();

    if (change === 'abort') controller.abort();
    if (change === 'session') currentSession = { ...session, id: 'session_switched' };
    if (change === 'profile') session.settings.voiceProfileId = 9;
    if (change === 'line') message.lines[0].ja = '台词已变化';
    if (change === 'deletion') deleting = true;
    if (change === 'turnConflict') message.turnConflict = true;
    if (change === 'serverMessageId') message.serverMessageId = 99;
    if (change === 'serverUserMessageId') message.serverUserMessageId = 88;

    gate.resolve({
      session_id: 'session_late',
      count: 1,
      messages: [{
        id: 42,
        role: 'assistant',
        lines: [{
          index: 0,
          ja: '日本語。',
          audio: {
            status: 'ready',
            generated_at: Date.now(),
            voice_profile_id: 7,
            chunks: [{ index: 0, line: 0, audio_url: '/audio/repair_late.wav', sentence: '日本語。' }],
          },
        }],
      }],
    });

    await assert.rejects(pending, { name: 'AbortError' });
    assert.equal(saves, 0);
    assert.equal(message.lines[0].audio.status, 'failed');
  });
}

function legacyFixture(request, { saveResult = true, fetchHistory } = {}) {
  const old = Date.now() - 9 * 86400000;
  const message = { id: 'm_legacy_42', serverMessageId: 42, role: 'assistant', content: '旧版中文', japanese: '旧版日本語。', ts: old, audioUrls: ['/audio/old.wav'] };
  const session = { id: 'session_legacy', settings: { voiceProfileId: 7 }, messages: [message] };
  let current = session, saves = 0, deleting = false;
  const calls = [];
  const reply = { session_id: session.id, message_id: 42, line_index: 0, voice_profile_id: 7, persisted: true,
    audio: { status: 'ready', generated_at: Date.now(), voice_profile_id: 7, voice_name: '当前角色',
      chunks: [{ index: 0, line: 0, audio_url: '/audio/repair_legacy.wav', sentence: '旧版日本語。' }] } };
  const recover = createLineAudioRecovery({
    getSession: () => current, getVoiceProfiles: () => [{ id: 7, name: '当前角色' }],
    saveState: () => { saves++; return saveResult; }, isSessionDeleting: () => deleting,
    fetchHistory,
    request: async (...args) => {
      calls.push(args); const body = JSON.parse(args[1].body);
      return request ? request(reply, body, args[1].signal) : { ...structuredClone(reply), message_id: body.message_id, persisted: body.message_id !== null };
    }
  });
  return { recover, session, message, calls, reply, get saves() { return saves; },
    switchSession: () => { current = { ...session }; }, deleteSession: () => { deleting = true; } };
}

test('legacy reply explicit repair installs canonical line 0, flattens audio, saves state and keeps original timestamps', async () => {
  const f = legacyFixture();
  const originalTs = f.message.ts;
  const result = await f.recover({ messageId: f.message.id, lineIndex: 0 });
  assert.equal(f.calls.length, 1);
  const [url, options] = f.calls[0];
  const body = JSON.parse(options.body);
  assert.equal(url, '/api/voice/regenerate-line');
  assert.equal(body.line_index, 0);
  assert.equal(body.text, '旧版日本語。');
  assert.equal(body.message_id, 42);
  assert.equal(body.voice_profile_id, 7);

  assert.equal(f.message.ts, originalTs, 'original timestamp must be preserved');
  assert.equal(f.message.content, '旧版中文');
  assert.equal(f.message.japanese, '旧版日本語。');
  assert.equal(Array.isArray(f.message.lines), true);
  assert.equal(f.message.lines.length, 1);
  assert.equal(f.message.lines[0].index, 0);
  assert.equal(f.message.lines[0].zh, '旧版中文');
  assert.equal(f.message.lines[0].ja, '旧版日本語。');
  assert.deepEqual(f.message.lines[0].audio, result.audio);
  assert.equal(f.message.audioUrls[0], '/audio/repair_legacy.wav');
  assert.equal(canReplayLine(f.message.lines[0], f.message, true), true);
  assert.equal(f.saves, 1);
});

for (const change of ['session', 'profile', 'late_lines', 'content', 'japanese', 'audio', 'deletion', 'abort', 'turnConflict', 'serverMessageId', 'serverUserMessageId']) {
  test(`legacy repair after ${change} discards late result and leaves lines undefined`, async () => {
    const gate = deferred();
    let controller = new AbortController();
    const f = legacyFixture((reply, body, signal) => gate.promise);

    const pending = f.recover({ messageId: f.message.id, lineIndex: 0, signal: controller.signal });
    await tick();

    if (change === 'abort') controller.abort();
    if (change === 'session') f.switchSession();
    if (change === 'profile') f.session.settings.voiceProfileId = 99;
    if (change === 'late_lines') f.message.lines = [{ index: 0, zh: '并发写入', ja: '遅い。', emotion: 'neutral', act: {}, audio: { status: 'ready', chunks: [] } }];
    if (change === 'content') f.message.content = '新中文内容';
    if (change === 'japanese') f.message.japanese = '新日本語台詞。';
    if (change === 'audio') f.message.audioUrls = ['/audio/concurrent.wav'];
    if (change === 'deletion') f.deleteSession();
    if (change === 'turnConflict') f.message.turnConflict = true;
    if (change === 'serverMessageId') f.message.serverMessageId = 999;
    if (change === 'serverUserMessageId') f.message.serverUserMessageId = 888;

    gate.resolve(f.reply);
    await assert.rejects(pending, { name: 'AbortError' });
    assert.equal(f.saves, 0);
    if (change !== 'late_lines') {
      assert.equal(f.message.lines, undefined);
    }
  });
}

test('legacy repair validates preconditions: wrong index, empty japanese, overlong japanese, explicit empty lines', async () => {
  const f = legacyFixture();

  // 1. wrong lineIndex > 0
  await assert.rejects(f.recover({ messageId: f.message.id, lineIndex: 1 }), /这句缺少日文台词/);

  // 2. empty japanese
  f.message.japanese = '   ';
  await assert.rejects(f.recover({ messageId: f.message.id, lineIndex: 0 }), /这句缺少日文台词/);

  // 3. overlong japanese > 2000
  f.message.japanese = 'あ'.repeat(2001);
  await assert.rejects(f.recover({ messageId: f.message.id, lineIndex: 0 }), /这句台词过长/);

  // 4. explicit empty array is not treated as legacy
  f.message.japanese = '日本語。';
  f.message.lines = [];
  await assert.rejects(f.recover({ messageId: f.message.id, lineIndex: 0 }), /这句缺少日文台词/);
});

test('when another tab already repaired legacy line, receiving 409 fetches remote line 0 and updates local message', async () => {
  const remoteAudio = {
    status: 'ready',
    generated_at: Date.now(),
    voice_profile_id: 7,
    voice_name: '当前角色',
    chunks: [{ index: 0, line: 0, audio_url: '/audio/repair_remote_legacy.wav', sentence: '旧版日本語。' }],
  };
  const historyResponse = {
    session_id: 'session_legacy',
    count: 1,
    messages: [{
      id: 42,
      role: 'assistant',
      content: '旧版中文',
      japanese: '旧版日本語。',
      lines: [{
        index: 0,
        zh: '旧版中文',
        ja: '旧版日本語。',
        emotion: 'neutral',
        act: {},
        audio: remoteAudio,
      }],
    }],
  };
  const f = legacyFixture(
    () => {
      const err = new Error('HTTP 409');
      err.status = 409;
      throw err;
    },
    { fetchHistory: async () => historyResponse }
  );

  const result = await f.recover({ messageId: f.message.id, lineIndex: 0 });
  assert.equal(result.remoteUpdated, true);
  assert.equal(result.persisted, true);
  assert.equal(f.message.lines.length, 1);
  assert.equal(f.message.lines[0].audio.chunks[0].audio_url, '/audio/repair_remote_legacy.wav');
  assert.equal(f.message.audioUrls[0], '/audio/repair_remote_legacy.wav');
  assert.equal(f.saves, 1);
});

test('legacy repair 409 history fetch abort or failure preserves original error and keeps lines undefined', async () => {
  const f = legacyFixture(
    () => {
      const err = new Error('HTTP 409');
      err.status = 409;
      throw err;
    },
    { fetchHistory: async () => { throw new Error('网络异常'); } }
  );

  await assert.rejects(f.recover({ messageId: f.message.id, lineIndex: 0 }), /HTTP 409/);
  assert.equal(f.message.lines, undefined);
  assert.equal(f.saves, 0);
});

test('legacy repair saveState failure surfaces saved: false while installing canonical line', async () => {
  const f = legacyFixture(null, { saveResult: false });
  const result = await f.recover({ messageId: f.message.id, lineIndex: 0 });
  assert.equal(result.saved, false);
  assert.equal(result.persisted, true);
  assert.equal(f.message.lines.length, 1);
  assert.equal(f.message.lines[0].audio.status, 'ready');
});
