import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createLineAudioRecovery } from '../frontend/src/controllers/line_audio_recovery.js';
import { createBacklog } from '../frontend/src/adv/backlog.js';
import { canReplayLine } from '../frontend/src/adv/replay.js';
import { normalizeLines } from '../frontend/src/session_data.js';
import { createConversation } from '../frontend/src/conversation.js';
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
  return { ...f, log, played, get chosen() { return chosen; } };
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
