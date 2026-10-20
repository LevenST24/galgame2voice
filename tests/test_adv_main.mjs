import assert from 'node:assert/strict';
import test from 'node:test';
// Import before installing a document: don't auto-boot authentication or devices.
import { initApp } from '../frontend/src/main.js';
import { DEFAULT_SESSION_SETTINGS } from '../frontend/src/store.js';
import { fakeDocument } from './helpers/adv_dom.mjs';
import { fakeChatStream, tick } from './helpers/chat_stream.mjs';

for (const terminal of ['done', 'error', 'cancelled']) test(`LOG opened mid-stream includes existing lines through ${terminal}`, async t => {
  const h = setup(t); h.app.sendMessage('问题');
  const request = h.stream.requests[0];
  await request.event('line', { index: 0, zh: '<img src=x onerror=alert(1)>', ja: '台詞' });
  await request.event('line_audio', { line: 0, status: 'skipped', chunks: [] });
  h.app.adv.action('log');
  const log = h.app.adv.backlog, text = '<img src=x onerror=alert(1)>';
  assert.equal(log.element.querySelectorAll('.adv-log-primary').some(e => e.textContent === text), true);
  assert.equal(log.element.querySelectorAll('img').length, 0);
  if (terminal === 'cancelled') h.app.conversation.stop();
  else await request.onEnd(text, terminal === 'error' ? { error: '断开' } : {});
  assert.equal(h.app.adv.backlog, log);
  assert.equal(log.element.querySelectorAll('.adv-log-primary').filter(e => e.textContent === text).length, 1);
  assert.equal(log.element.ownerDocument.activeElement, log.closeButton);
});

test('real empty done offers retry while LOG stays open and reconciles the failed user', async t => {
  const h = setup(t); h.app.sendMessage('空回复'); h.app.adv.action('log');
  await h.stream.requests[0].onEnd('');
  const log = h.app.adv.backlog;
  assert.equal(log.element.querySelector('.adv-log-failure')?.textContent, '未收到回复：模型这次没有返回任何内容');
  assert.equal(log.element.ownerDocument.activeElement, log.closeButton);
  h.app.adv.action('close_overlay');
  assert.equal(h.app.adv.player.state, 'error');
  const buttons = h.app.adv.stage.stage.querySelectorAll('.adv-error-btn');
  assert.equal(buttons.length, 2); buttons[0].click();
  assert.equal(h.stream.requests.length, 2);
  h.app.conversation.stop();
});

function setup(t) {
  const doc = fakeDocument(); globalThis.document = doc;
  globalThis.window = { innerWidth: 1280 };
  globalThis.localStorage = { getItem: () => null, setItem() {} };
  globalThis.fetch = async () => new Response(JSON.stringify({ characters: [] }), { headers: { 'Content-Type': 'application/json' } });
  const sessions = [0, 1].map(n => ({ id: `s_${n}`, title: `Session ${n}`, messages: [],
    settings: { ...DEFAULT_SESSION_SETTINGS, advEnabled: n === 0, advTextIntervalMs: n === 0 ? 0 : 100 } }));
  const state = { sessions, activeId: sessions[0].id, global: { voiceMode: true } };
  const getActive = () => sessions.find(s => s.id === state.activeId);
  const dom = Object.fromEntries(['messages', 'input', 'sendBtn', 'stopBtn', 'advRoot'].map(key => [key, doc.createElement('div')]));
  for (const node of Object.values(dom)) doc.body.appendChild(node);
  const layout = doc.createElement('div'); layout.className = 'app'; doc.body.appendChild(layout);
  const audio = { isPlaying: false, played: [], sequential: [], currentMsgId: null,
    playLine(index, chunks) { this.played.push({ index, chunks }); return Promise.resolve('ended'); },
    stopLine() {}, startSession(...args) { this.sequential.push(args); }, enqueueChunk() {},
  };
  const noop = () => {};
  const stream = fakeChatStream(); let sequence = 0, deleting = false;
  const app = initApp({ initialize: false, dom, state, getActive,
    getSession: id => sessions.find(s => s.id === id), isSessionDeleting: () => deleting,
    getCurrentActiveProvider: () => null, getVoiceProfiles: () => [], haveProvidersLoaded: () => true,
    saveState: noop, autoTitle: noop, persistSession: async () => {}, clearMicTimers: noop,
    getCurrentVoice: () => null, stopCurrentVoice: noop, setCurrentVoice: noop,
    uid: () => `m_${++sequence}`, fetch: stream.fetch, showToast: noop, fetchAndCacheAudio: async () => {},
    streamAudioController: audio,
    portraitStage: { updateDialogue: noop, setEmotion: noop, handleMessageEmotion: noop, refreshAffection: noop },
    addMessage(role, content) { const msg = { id: `m_${++sequence}`, role, content }; getActive().messages.push(msg); return msg; },
    renderMessages(root, session) {
      root.innerHTML = ''; const inner = doc.createElement('div'); root.appendChild(inner);
      for (const message of session.messages) { const el = doc.createElement('div'); el.textContent = message.content; inner.appendChild(el); }
      return inner;
    },
    createMessageEl(msg) { const el = doc.createElement('div'); el.textContent = msg.content; return { el, contentEl: el }; },
    createTypingEl: () => doc.createElement('div'),
  });
  app.fullRender(); t.after(() => { app.conversation.stop(); if (app.adv) { getActive().settings.advEnabled = false; app.syncAdv(); } });
  return { app, sessions, dom, state, audio, stream, getActive, layout, setDeleting: value => { deleting = value; } };
}

test('main mounts only opted-in sessions; toggling, session switch and re-mount restore the right settings', async t => {
  const h = setup(t); assert.ok(h.app.adv); assert.equal(h.layout.inert, true);
  const original = h.app.adv;
  h.sessions[0].settings.advEnabled = false; h.app.fullRender();
  assert.equal(h.app.adv, null); assert.equal(h.dom.advRoot.children.length, 0); assert.equal(h.layout.inert, false);
  h.sessions[0].settings.advEnabled = true; h.app.fullRender(); assert.notEqual(h.app.adv, original);
  await h.app.switchSession('s_1'); assert.equal(h.app.adv, null);
  h.sessions[1].settings.advEnabled = true; h.app.fullRender();
  assert.equal(h.app.adv.player.settings.revealMsPerGrapheme, 100);
  await h.app.switchSession('s_0'); assert.equal(h.app.adv.player.settings.revealMsPerGrapheme, 0);
  h.app.adv.unmount(); h.sessions[0].settings.advEnabled = false; h.app.syncAdv();
  h.sessions[0].settings.advEnabled = true; h.app.syncAdv(); assert.equal(h.app.adv.player.state, 'input');
});

test('real conversation events use per-line audio and keep skipped lines in history', async t => {
  const h = setup(t); h.app.sendMessage('问题'); await tick();
  const request = h.stream.requests[0];
  await request.event('line', { index: 0, zh: '你好。', ja: 'こんにちは。', emotion: 'gentle', act: {} });
  assert.equal(h.app.adv.player.state, 'waiting_audio');
  await request.event('audio_chunk', { index: 0, line: 0, audio_url: '/audio/cache/one.wav', sentence: 'こんにちは。' });
  assert.equal(h.audio.sequential.length, 0, 'legacy sequential playback never competes with ADV');
  await request.event('line_audio', { line: 0, status: 'ready', chunks: [{ index: 0, line: 0, audio_url: '/audio/cache/one.wav' }] });
  assert.equal(h.audio.played.length, 1);
  const input = h.app.adv.textbox.inputElement; h.app.adv.action('input'); input.value = '草稿'; input.dispatchEvent({ type: 'input' });
  await request.event('line', { index: 1, zh: '保留台词。', ja: '', emotion: 'happy', act: { motion: 'bounce' } });
  await request.event('line_audio', { line: 1, status: 'skipped', chunks: [] });
  await request.onEnd('你好。保留台词。');
  assert.equal(h.app.adv.player.state, 'input'); assert.equal(h.app.adv.textbox.draft, '草稿');
  const stored = h.getActive().messages.at(-1);
  assert.equal(stored.lines.length, 2); assert.equal(stored.lines[1].act.motion, 'bounce');
  assert.equal(h.audio.played.length, 1);
});

test('changing renderer during a live turn cancels generation and preserves delivered history', async t => {
  const h = setup(t); h.app.sendMessage('问题'); await tick();
  await h.stream.requests[0].event('line', { index: 0, zh: '已收到。', ja: '', emotion: 'gentle', act: {} });
  h.sessions[0].settings.advEnabled = false; h.app.fullRender();
  assert.equal(h.app.conversation.busy, false); assert.equal(h.app.adv, null);
  assert.equal(h.getActive().messages.at(-1).lines[0].zh, '已收到。');
});

test('Japanese-only line survives cancellation without a legacy text event', async t => {
  const h = setup(t); h.app.sendMessage('问题'); await tick();
  await h.stream.requests[0].event('line', { index: 0, zh: '', ja: '日本語だけ。', emotion: 'gentle', act: {} });
  h.app.conversation.stop();
  const message = h.getActive().messages.at(-1);
  assert.equal(message.role, 'assistant'); assert.equal(message.japanese, '日本語だけ。');
  assert.equal(message.lines[0].audio.status, 'failed');
});

test('character switch stops the real controller and saves delivered lines before resetting ADV', async t => {
  const h = setup(t); h.sessions[0].settings.voiceProfileId = 1; h.app.fullRender();
  h.app.sendMessage('问题'); await tick();
  await h.stream.requests[0].event('line', { index: 0, zh: '旧角色台词。', ja: '', emotion: 'gentle', act: {} });
  h.sessions[0].settings.voiceProfileId = 2; h.app.fullRender();
  assert.equal(h.app.conversation.busy, false);
  assert.equal(h.getActive().messages.at(-1).lines[0].zh, '旧角色台词。');
  assert.equal(h.app.adv.player.state, 'input');
  await h.stream.requests[0].event('line', { index: 1, zh: '迟到台词', ja: '', emotion: '', act: {} });
  assert.equal(h.getActive().messages.at(-1).lines.length, 1);
  assert.equal(h.app.adv.player.currentIndex, null);
});

test('LOG opened during a real stream contains the final saved reply after done', async t => {
  const h = setup(t); h.app.sendMessage('问题'); await tick();
  const request = h.stream.requests[0]; await request.onChunk('partial');
  h.app.adv.action('log'); await request.onChunk('partial final'); await request.onEnd('partial final');
  assert.equal(h.getActive().messages.at(-1).content, 'partial final');
  assert.ok(h.dom.messages.querySelectorAll('div').some(node => node.textContent === 'partial final'));
  const overlay = h.app.adv.stage.stage.querySelector('.adv-log-overlay');
  assert.ok(overlay, 'LOG overlay should be open');
  assert.ok(overlay.querySelectorAll('.adv-log-primary').some(node => node.textContent === 'partial final'));
});

test('main input keydown ignores Enter during IME composition (keyCode 229 / isComposing)', async t => {
  const h = setup(t);
  let defaultPrevented = false;

  // During IME composition (isComposing = true)
  h.dom.input.value = 'ceshi';
  h.app.handleInputKeydown({
    key: 'Enter',
    isComposing: true,
    preventDefault: () => { defaultPrevented = true; },
  });
  assert.equal(h.stream.requests.length, 0);
  assert.equal(defaultPrevented, false);

  // IME commit with keyCode 229
  h.app.handleInputKeydown({
    key: 'Enter',
    keyCode: 229,
    preventDefault: () => { defaultPrevented = true; },
  });
  assert.equal(h.stream.requests.length, 0);
  assert.equal(defaultPrevented, false);

  // Normal Enter sends
  h.app.handleInputKeydown({
    key: 'Enter',
    preventDefault: () => { defaultPrevented = true; },
  });
  await tick();
  assert.equal(h.stream.requests.length, 1);
  assert.equal(defaultPrevented, true);
});

for (const blocked of ['loading', 'deleting']) test(`real controller ${blocked} rejection preserves draft and disables ADV sending`, t => {
  const h = setup(t);
  if (blocked === 'loading') h.getActive().loadingOlder = true;
  else h.setDeleting(true);
  h.app.adv.action('input'); const input = h.app.adv.textbox.inputElement; input.value = '保留';
  input.dispatchEvent({ type: 'keydown', key: 'Enter' });
  assert.equal(h.stream.requests.length, 0); assert.equal(input.value, '保留');
  assert.equal(h.app.adv.stage.stage.querySelector('.adv-player-send-btn').disabled, true);
});
