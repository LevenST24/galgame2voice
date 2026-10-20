import assert from 'node:assert/strict';
import test from 'node:test';
import { mountAdv } from '../frontend/src/adv/adv_app.js';
import { DEFAULT_SESSION_SETTINGS } from '../frontend/src/store.js';
import { fakeDocument, fakeClock, Element, microtasks } from './helpers/adv_dom.mjs';

const portrait = (file = '/art.png') => ({ enabled: true, defaultCostume: 'uniform',
  costumes: [{ id: 'uniform', faceRect: { x: 0, y: 0, width: 400, height: 450 }, headAnchor: { x: 200, y: 0 } }],
  sprites: { uniform: { smile: { file, width: 800, height: 1600 } } },
  faces: { smile: { primary: 'gentle' } }, expressionSets: { gentle: [{ id: 'smile' }] } });

function setup(t, options = {}) {
  const doc = fakeDocument(); globalThis.document = doc;
  const clock = fakeClock(), frames = new Map(), loads = [], listeners = new Map(), mediaListeners = new Set();
  const viewport = doc.createElement('div');
  let frameId = 0, voiceEnd, overlayChange, subscription;
  const sessions = [0, 1].map(n => ({ id: `s_${n}`, settings: { ...DEFAULT_SESSION_SETTINGS, advEnabled: true, ...options.settings }, messages: [] }));
  let active = sessions[0], character = { id: 1, name: '测试角色' }, voiceEnabled = options.voice !== false;
  class Image extends Element {
    constructor() { super('img', doc); }
    decode() {
      const load = { image: this, url: this.src };
      load.promise = new Promise((resolve, reject) => { load.resolve = resolve; load.reject = reject; });
      loads.push(load); return load.promise;
    }
  }
  const controller = {
    busy: false, sent: [], retried: [], stopped: 0,
    on(type, fn) { if (!listeners.has(type)) listeners.set(type, new Set()); listeners.get(type).add(fn); return () => listeners.get(type).delete(fn); },
    send(text) { if (this.busy || active.loadingOlder || active.deleting) return false; this.sent.push(text); return true; },
    canRetry(user) { return !this.busy && !active.loadingOlder && !active.deleting && !!user?.failed && active.messages.at(-1) === user; },
    retry(user) { if (this.canRetry(user)) this.retried.push(user); }, stop() { this.stopped++; },
  };
  const audio = { played: [], stopped: [],
    playLine(index, chunks, opts) { this.played.push({ index, chunks, opts }); return new Promise(resolve => { voiceEnd = resolve; }); },
    stopLine(opts) { this.stopped.push(opts); voiceEnd?.('interrupted'); },
  };
  const dom = Object.fromEntries(['root', 'sidebar', 'messages', 'input', 'micBtn'].map(key => [key, doc.createElement(key === 'input' ? 'textarea' : 'div')]));
  for (const el of Object.values(dom)) doc.body.appendChild(el);
  const mql = { matches: false, addEventListener: (_, fn) => mediaListeners.add(fn), removeEventListener: (_, fn) => mediaListeners.delete(fn) };
  const calls = { settings: 0, models: 0, newSession: 0, history: 0, mic: 0 };
  const app = mountAdv(dom, { controller, audio, clock, getSession: () => active, voiceEnabled: () => voiceEnabled,
    getCharacter: () => character, ImageClass: Image,
    loadPortrait: options.loadPortrait || (async () => options.art ? portrait() : null),
    raf: fn => { frames.set(++frameId, fn); return frameId; }, cancelRaf: id => frames.delete(id),
    matchMedia: () => mql, viewport,
    saveSettings: patch => { Object.assign(active.settings, patch); },
    onSettings: () => { calls.settings++; }, onModelSettings: () => { calls.models++; },
    onNewSession: () => { calls.newSession++; }, onMic: () => { calls.mic++; },
    renderHistory: () => { calls.history++; },
    subscribeOverlay: fn => { overlayChange = fn; return () => { overlayChange = null; }; },
    subscribe: fn => { subscription = fn; return () => { subscription = null; }; },
  });
  t.after(() => app.unmount());
  const emit = (type, event) => { for (const fn of [...listeners.get(type) || []]) fn(event); };
  let turn = 'turn_0';
  const start = (id = 'turn_0') => {
    turn = id; controller.busy = true;
    const userMessage = { id: 'user_0', role: 'user', content: '问题', recording: 'keep' };
    active.messages.push(userMessage);
    emit('turnstart', { originId: active.id, assistantMessage: { id }, userMessage });
  };
  const line = (index = 0, extra = {}) => {
    const record = { index, zh: '你好世界。', ja: 'こんにちは。', emotion: 'gentle', act: {}, ...extra };
    active.messages.push(record); emit('line', { messageId: turn, line: record }); return record;
  };
  const audioEvent = (index = 0, status = 'ready', id = turn) => {
    const event = { messageId: id, line: index, status,
      chunks: status === 'ready' ? [{ index: 0, line: index, audio_url: '/audio/0.wav' }] : [] };
    if (id === turn) {
      const record = active.messages.find(message => message.index === index);
      if (record) record.audio = { status, chunks: event.chunks };
    }
    emit('line_audio', event);
  };
  const done = (type = 'done') => {
    controller.busy = false;
    if (type === 'error' && active.messages.at(-1)?.role === 'user') active.messages.at(-1).failed = { error: '测试故障' };
    emit(type, { originId: active.id, error: type === 'error' ? '测试故障' : undefined });
  };
  return { app, doc, dom, clock, frames, loads, controller, audio, sessions, calls, listeners, mediaListeners, viewport,
    emit, start, line, audioEvent, done,
    endVoice: outcome => voiceEnd?.(outcome),
    flush: () => { for (const [id, fn] of [...frames]) { frames.delete(id); fn(); } },
    motion: matches => { mql.matches = matches; for (const fn of mediaListeners) fn({ matches }); },
    overlay: open => overlayChange(open),
    switchSession: n => { active = sessions[n]; subscription(); },
    switchCharacter: id => { character = { id, name: `角色 ${id}` }; subscription(); },
    voiceMode: value => { voiceEnabled = value; subscription(); },
    subscriptions: () => [subscription, overlayChange],
  };
}

test('line → audio wait → per-line voice and typewriter → click reveal → advance → player', t => {
  const h = setup(t); h.start(); h.line();
  assert.equal(h.app.player.state, 'waiting_audio'); h.clock.tick(1499);
  assert.equal(h.audio.played.length, 0); h.audioEvent();
  assert.equal(h.app.player.state, 'revealing'); assert.equal(h.audio.played[0].opts.msgId, 'turn_0');
  const primary = h.app.stage.stage.querySelector('.adv-primary-text');
  const before = primary.textContent; h.clock.tick(60); assert.notEqual(primary.textContent, before);
  h.app.action('advance'); assert.equal(h.app.player.state, 'waiting_advance');
  assert.equal(h.audio.stopped.length, 0, 'completing reveal retains voice');
  h.done(); assert.equal(h.app.player.state, 'waiting_advance', 'done never skips unread text');
  h.app.action('advance'); assert.equal(h.app.player.state, 'input');
  assert.equal(h.app.stage.stage.querySelector('.adv-name-text').textContent, 'Player');
});

test('timeout accepts late audio only on the current line and rejects old turn events', async t => {
  const h = setup(t, { settings: { advTextIntervalMs: 0 } }); h.start(); h.line(); h.line(1);
  h.clock.tick(1500); assert.equal(h.app.player.state, 'waiting_advance');
  h.audioEvent(); assert.equal(h.audio.played.length, 1);
  h.app.action('advance'); h.audioEvent(0); assert.equal(h.audio.played.length, 1);
  h.audioEvent(1); assert.equal(h.audio.played[1].index, 1);
  h.endVoice('failed'); await microtasks();
  assert.ok(h.app.stage.stage.querySelector('.adv-voice-display').classList.contains('is-hidden'));
  h.start('turn_1'); h.line(); h.audioEvent(0, 'ready', 'turn_0');
  assert.equal(h.app.player.state, 'waiting_audio');
});

for (const status of ['skipped', 'failed']) test(`${status} audio reveals without deadlock`, t => {
  const h = setup(t); h.start(); h.line(); h.audioEvent(0, status);
  assert.equal(h.app.player.state, 'revealing'); assert.equal(h.audio.played.length, 0);
  h.done(); h.clock.tick(500); h.app.action('advance'); assert.equal(h.app.player.state, 'input');
});

for (const mode of ['normal', 'voice-priority']) test(`AUTO ${mode} honors text delay and voice policy`, async t => {
  const h = setup(t, { settings: { advTextIntervalMs: 0, advAutoMode: mode, advAutoBaseMs: 0, advAutoPerCharMs: 0 } });
  h.start(); h.line(); h.line(1); h.audioEvent(); h.app.action('auto');
  h.clock.tick(1200);
  assert.equal(h.app.player.currentIndex, mode === 'normal' ? 1 : 0);
  if (mode === 'voice-priority') {
    h.endVoice('ended'); await microtasks(); h.clock.tick(1199); assert.equal(h.app.player.currentIndex, 0);
    h.clock.tick(1); assert.equal(h.app.player.currentIndex, 1);
  }
});

test('SKIP suppresses audio and advances once per 80 ms; hidden window consumes restore click', t => {
  const h = setup(t, { settings: { advTextIntervalMs: 0 } }); h.start(); h.line(); h.line(1); h.audioEvent();
  h.app.action('hide'); h.app.stage.stage.click(); assert.equal(h.app.player.hidden, false);
  assert.equal(h.app.player.currentIndex, 0, 'restore click is consumed');
  h.app.action('skip'); h.clock.tick(79); assert.equal(h.app.player.currentIndex, 0);
  h.clock.tick(1); assert.equal(h.app.player.currentIndex, 1); h.audioEvent(1);
  assert.equal(h.audio.played.length, 1, 'queued audio suppressed');
});

test('hidden window pauses AUTO, unhide restarts delay without losing a line', t => {
  const h = setup(t, { voice: false, settings: { advTextIntervalMs: 0 } }); h.start(); h.line(); h.line(1);
  h.app.action('auto'); h.app.action('hide'); h.clock.tick(10000);
  assert.equal(h.app.player.currentIndex, 0); assert.equal(h.app.textbox.isHidden, true);
  h.app.action('unhide'); h.clock.tick(2000); assert.equal(h.app.player.currentIndex, 1);
});

test('typing away keeps queued/future history, cancels voice/AUTO/SKIP and preserves IME draft', t => {
  const h = setup(t); h.start(); h.line(); h.line(1); h.audioEvent(); h.app.action('auto');
  h.app.action('input'); assert.equal(h.app.player.state, 'revealing', 'focus alone retains queue');
  const input = h.app.textbox.inputElement;
  input.value = '草稿'; input.dispatchEvent({ type: 'compositionstart' });
  assert.equal(h.app.player.state, 'input'); assert.equal(h.app.player.auto, false); assert.equal(h.app.player.skip, false);
  h.line(2); h.audioEvent(2); assert.equal(h.sessions[0].messages.length, 4); assert.equal(h.audio.played.length, 1);
  input.dispatchEvent({ type: 'keydown', key: 'Enter', keyCode: 229 }); assert.deepEqual(h.controller.sent, []);
  input.dispatchEvent({ type: 'compositionend' }); h.done();
  input.dispatchEvent({ type: 'keydown', key: 'Enter' }); assert.deepEqual(h.controller.sent, [], 'IME commit guard survives compositionend');
  input.dispatchEvent({ type: 'keyup', key: 'Enter' });
  input.dispatchEvent({ type: 'keydown', key: 'Enter' }); assert.deepEqual(h.controller.sent, ['草稿']);
});

test('live reduced motion finishes reveal and never re-announces/replays the line', t => {
  const h = setup(t); h.start(); h.line(); h.audioEvent();
  const live = h.app.stage.stage.querySelector('.adv-live-region'); const count = live.textWrites.length;
  h.clock.tick(30); h.motion(true);
  assert.equal(h.app.player.state, 'waiting_advance'); assert.equal(h.clock.timers.size, 0);
  assert.equal(live.textWrites.length, count); assert.equal(h.audio.played.length, 1);
  h.app.action('swap_languages'); assert.equal(live.textWrites.length, count);
  assert.equal(h.app.textbox.currentPrimaryText, 'こんにちは。'); h.motion(false);
});

test('stale sprite decode after character/session switch cannot repaint stage or crop', async t => {
  const h = setup(t, { art: true }); await microtasks(); h.start(); h.line();
  const old = h.loads.slice(-2); assert.equal(old.length, 2);
  const loadCount = h.loads.length;
  h.switchCharacter(2); await microtasks();
  for (const load of old) load.resolve(); await microtasks(); h.flush();
  assert.equal(h.app.sprite.getLastCommitted(), null);
  const latest = h.loads.slice(loadCount); latest.forEach(load => load.resolve()); await microtasks(); h.flush();
  assert.equal(h.app.sprite.getLastCommitted().faceUrl, '/art.png');
  const displayed = h.app.sprite.getLastCommitted(); assert.equal(displayed.state.outfit, 'uniform');
  h.switchSession(1); await microtasks(); old.forEach(load => load.resolve()); await microtasks(); h.flush();
  assert.equal(h.app.sprite.getLastCommitted(), null);
});

test('unmount cancels timers, rAF, image loads, subscriptions and every owned listener/node', async t => {
  const h = setup(t, { art: true }); await microtasks(); h.start(); h.line(); h.audioEvent();
  const nodes = [h.app.stage.shell, ...h.app.stage.shell.querySelectorAll('div, button, textarea, nav')];
  h.app.action('sessions'); assert.equal(h.dom.sidebar.parentNode.className, 'adv-drawer adv-drawer-sessions');
  h.app.unmount(); h.app.unmount(); h.loads.forEach(load => load.resolve()); await microtasks(); h.flush();
  assert.equal(h.dom.root.children.length, 0); assert.equal(h.dom.sidebar.parentNode, h.doc.body);
  assert.equal(h.clock.timers.size, 0); assert.equal(h.frames.size, 0); assert.equal(h.mediaListeners.size, 0);
  assert.ok([...h.listeners.values()].every(set => set.size === 0)); assert.deepEqual(h.subscriptions(), [null, null]);
  assert.ok(nodes.every(node => [...node.listeners.values()].every(set => set.size === 0)));
  assert.ok([...h.viewport.listeners.values()].every(set => set.size === 0));
});

test('voice off uses typewriter and count-based AUTO with no audio terminal or artwork', t => {
  const h = setup(t, { voice: false }); h.start(); h.line(); h.line(1); h.done();
  assert.equal(h.app.player.state, 'revealing', 'voice off never waits for synthesis');
  // Five graphemes; decorative quotation marks are excluded: 1500 + 5 * 45.
  h.clock.tick(180); h.app.action('auto'); h.clock.tick(1724); assert.equal(h.app.player.currentIndex, 0);
  h.clock.tick(1); assert.equal(h.app.player.currentIndex, 1); assert.equal(h.audio.played.length, 0);
  assert.equal(h.app.sprite.getLastCommitted(), null);
});

test('drawers and settings suspend playback, restore draft/focus, and recovery delegates retry', t => {
  const h = setup(t); h.start(); h.line(); h.audioEvent(); h.app.action('input');
  const input = h.app.textbox.inputElement; input.value = '保留'; input.dispatchEvent({ type: 'input' });
  h.app.action('log'); assert.equal(h.app.player.state, 'suspended');
  h.app.stage.stage.dispatchEvent({ type: 'keydown', key: 'Escape' }); assert.equal(h.app.textbox.draft, '保留');
  assert.equal(h.doc.activeElement, input); h.app.action('sessions');
  assert.equal(h.dom.sidebar.parentNode.className, 'adv-drawer adv-drawer-sessions'); h.app.action('close_overlay');
  h.overlay(true); h.clock.tick(9999); assert.equal(h.app.player.state, 'suspended'); h.overlay(false);
  h.start('retryable'); h.done('error'); const actions = h.app.stage.stage.querySelector('.adv-error-actions').children;
  assert.equal(actions[0].textContent, '重新发送'); actions[0].click(); actions[1].click();
  assert.equal(h.controller.retried[0].recording, 'keep'); assert.equal(h.calls.settings, 1);
  h.app.action('model_status'); assert.equal(h.calls.models, 1);
});

test('an interrupted previous voice promise cannot clear the next line VOICE indicator', async t => {
  const h = setup(t, { settings: { advTextIntervalMs: 0 } }); h.start(); h.line(); h.line(1); h.audioEvent();
  h.audioEvent(1); h.app.action('advance'); await microtasks();
  assert.equal(h.audio.played.length, 2);
  assert.equal(h.app.stage.stage.querySelector('.adv-voice-display').classList.contains('is-hidden'), false);
  h.endVoice('ended'); await microtasks();
  assert.equal(h.app.stage.stage.querySelector('.adv-voice-display').classList.contains('is-hidden'), true);
});

test('live art refresh retains decoded art through replacement failure without a retry loop', async t => {
  let requests = 0;
  const h = setup(t, { loadPortrait: async () => portrait(++requests === 1 ? '/old.png?exp=10000' : '/replacement.png') });
  await microtasks(); h.loads.forEach(load => load.resolve()); await microtasks(); h.flush();
  assert.equal(h.app.sprite.getLastCommitted().faceUrl, '/old.png?exp=10000');
  h.clock.tick(1000); await microtasks();
  assert.equal(h.app.sprite.getLastCommitted().faceUrl, '/old.png?exp=10000', 'refresh must retain decoded buffers');
  h.loads.filter(load => load.url === '/replacement.png').forEach(load => load.reject(new Error('decode')));
  await microtasks();
  const retryLoads = h.loads.filter(load => load.url === '/replacement.png');
  retryLoads.forEach(load => load.reject(new Error('decode'))); await microtasks();
  assert.equal(requests, 3, 'one proactive refresh and one recovery request');
  assert.equal(h.app.sprite.getLastCommitted().faceUrl, '/old.png?exp=10000');
});

test('missing primary falls back to the available text for typewriter and AUTO without duplication', t => {
  const h = setup(t, { voice: false }); h.start(); h.line(0, { zh: '', ja: '日本語。' });
  assert.equal(h.app.player.state, 'revealing'); h.clock.tick(120);
  assert.equal(h.app.textbox.currentPrimaryText, '日本語。'); assert.equal(h.app.textbox.currentSubText, '');
  h.app.action('auto'); h.clock.tick(1679); assert.equal(h.app.player.state, 'waiting_advance');
  h.done(); h.clock.tick(1); assert.equal(h.app.player.state, 'input'); assert.equal(h.app.player.auto, false);
});

test('hidden window does not activate incoming lines, and stops an active SKIP timer', t => {
  const h = setup(t, { voice: false, settings: { advTextIntervalMs: 0 } }); h.start(); h.app.action('hide'); h.line();
  assert.equal(h.app.player.currentIndex, null); h.app.action('unhide'); assert.equal(h.app.player.currentIndex, 0);
  h.line(1); h.app.action('skip'); h.app.action('hide'); h.clock.tick(1000);
  assert.equal(h.app.player.currentIndex, 0); assert.equal(h.app.player.skip, false);
});

test('one live announcement per activated line; viewport reflow never replays or announces', t => {
  const h = setup(t, { settings: { advTextIntervalMs: 0 } }); h.start();
  const live = h.app.stage.stage.querySelector('.adv-live-region'); const before = live.textWrites.length;
  h.line(); h.audioEvent(); h.line(1); assert.equal(live.textWrites.length, before + 1);
  h.viewport.dispatchEvent({ type: 'resize' }); assert.equal(live.textWrites.length, before + 1);
  assert.equal(h.audio.played.length, 1); h.app.action('advance');
  assert.equal(live.textWrites.length, before + 2);
});

test('mic, model and configuration controls are reachable without advancing the dialogue', t => {
  const h = setup(t, { voice: false, settings: { advTextIntervalMs: 0 } }); h.start(); h.line(); h.line(1);
  const inputButton = h.app.stage.stage.querySelector('.adv-app-tools').children[1]; inputButton.click();
  assert.equal(h.app.player.currentIndex, 0); assert.equal(h.app.player.state, 'waiting_advance');
  h.app.textbox.micButton.click(); assert.equal(h.calls.mic, 1); assert.equal(h.app.player.state, 'input');
  h.app.stage.stage.querySelector('.adv-qm-status').click(); assert.equal(h.calls.models, 1);
  h.app.stage.stage.querySelector('.adv-qm-btn-config').click(); assert.equal(h.calls.settings, 1);
});

test('closing overlays restores the same stage/crop revision without replaying or announcing', async t => {
  const h = setup(t, { art: true, settings: { advTextIntervalMs: 0 } });
  await microtasks(); h.start(); h.line(); h.audioEvent();
  h.loads.forEach(load => load.resolve()); await microtasks(); h.flush();
  const before = h.app.sprite.getLastCommitted();
  const live = h.app.stage.stage.querySelector('.adv-live-region'), announcements = live.textWrites.length;
  for (const overlay of ['log', 'settings']) {
    if (overlay === 'log') { h.app.action('log'); h.app.action('close_overlay'); }
    else { h.overlay(true); h.overlay(false); }
    h.loads.forEach(load => load.resolve()); await microtasks(); h.flush();
    assert.equal(h.app.sprite.getLastCommitted().faceUrl, before.faceUrl);
    assert.equal(h.app.sprite.getLastCommitted().state.face, before.state.face);
    const visibleCrop = h.app.textbox.faceSlot.children.filter(el => el.classList.contains('is-visible'));
    assert.equal(visibleCrop.length, 1); assert.equal(visibleCrop[0].src, before.faceUrl);
    assert.equal(live.textWrites.length, announcements); assert.equal(h.audio.played.length, 1);
  }
});

test('session drafts stay in memory by id and never pass through the shared legacy input', t => {
  const h = setup(t);
  h.app.textbox.setDraft('A 草稿'); h.switchSession(1);
  assert.equal(h.app.textbox.draft, ''); assert.equal(h.dom.input.value, '');
  h.app.textbox.setDraft('B 草稿'); h.switchSession(0);
  assert.equal(h.app.textbox.draft, 'A 草稿');
  h.switchSession(1); assert.equal(h.app.textbox.draft, 'B 草稿');
  h.app.unmount(); assert.equal(h.dom.input.value, '');
});

for (const trigger of ['click', 'Enter']) test(`hidden queued line survives done and restores on ${trigger} even in input context`, t => {
  const h = setup(t, { voice: false, settings: { advTextIntervalMs: 0 } });
  h.start(); h.app.action('hide'); h.line(); h.done();
  assert.equal(h.app.player.currentIndex, null);
  if (trigger === 'click') h.app.stage.stage.click();
  else h.app.stage.stage.dispatchEvent({ type: 'keydown', key: trigger });
  assert.equal(h.app.player.hidden, false); assert.equal(h.app.player.currentIndex, 0);
  assert.equal(h.app.player.state, 'waiting_advance');
  h.app.action('advance'); assert.equal(h.app.player.state, 'input');
});

for (const overlay of ['log', 'dialog', 'hidden']) test(`replay completion retains ${overlay} pause ownership`, async t => {
  const h = setup(t, { voice: false, settings: { advTextIntervalMs: 0, advAutoBaseMs: 0, advAutoPerCharMs: 0 } });
  h.start(); h.line(); h.line(1); h.audioEvent(); h.done();
  h.app.action('auto'); h.app.action('replay_voice');
  if (overlay === 'log') h.app.action('log');
  else if (overlay === 'dialog') h.overlay(true);
  else h.app.action('hide');
  h.endVoice('ended'); await microtasks(); h.clock.tick(5000);
  assert.equal(h.app.player.currentIndex, 0);
  assert.equal(h.app.player.state, 'suspended');
  if (overlay === 'log') h.app.action('close_overlay');
  else if (overlay === 'dialog') h.overlay(false);
  else h.app.action('unhide');
  h.clock.tick(1200); assert.equal(h.app.player.currentIndex, 1);
});

test('stale replay cannot resume a different turn behind a dialog', async t => {
  const h = setup(t, { voice: false, settings: { advTextIntervalMs: 0 } });
  h.start(); h.line(); h.audioEvent(); h.done(); h.app.action('replay_voice');
  h.start('new'); h.line(); h.line(1); h.overlay(true);
  h.endVoice('ended'); await microtasks();
  assert.equal(h.app.player.state, 'suspended');
});

test('partial failure exposes its reason and settings without an unsupported retry', t => {
  const h = setup(t); h.start(); h.line(); h.done('error');
  const actions = h.app.stage.stage.querySelector('.adv-error-actions').children;
  assert.deepEqual(actions.map(node => node.textContent), ['去设置']);
  assert.equal(h.app.stage.stage.querySelector('.adv-error-message').textContent, '测试故障');
});

test('error action listeners are disposed on replacement and clearing', t => {
  const h = setup(t); let calls = 0;
  h.app.textbox.showError({ actions: [{ label: 'old', run: () => calls++ }] });
  const old = h.app.stage.stage.querySelector('.adv-error-actions').children[0];
  h.app.textbox.showError({ actions: [{ label: 'new', run: () => calls++ }] });
  assert.equal(old.listeners.get('click').size, 0); old.click(); assert.equal(calls, 0);
  const next = h.app.stage.stage.querySelector('.adv-error-actions').children[0];
  h.app.textbox.clearError(); assert.equal(next.listeners.get('click').size, 0);
  assert.equal(h.app.stage.stage.querySelector('.adv-error-actions').children.length, 0);
});

test('empty outfit immediately restores the pack default after a non-default outfit', async t => {
  const payload = portrait(); payload.costumes.push({ id: 'other' });
  payload.sprites.other = { smile: { file: '/other.png', width: 800, height: 1600 } };
  const h = setup(t, { loadPortrait: async () => payload, settings: { advOutfit: 'other' } });
  await microtasks(); h.loads.forEach(load => load.resolve()); await microtasks(); h.flush();
  assert.equal(h.app.sprite.getLastCommitted().state.outfit, 'other');
  h.sessions[0].settings.advOutfit = ''; h.app.update();
  h.loads.forEach(load => load.resolve()); await microtasks(); h.flush();
  assert.equal(h.app.sprite.getLastCommitted().state.outfit, 'uniform');
});

for (const key of ['Enter', ' ']) test(`focused settings button retains native ${JSON.stringify(key)} activation`, t => {
  const h = setup(t, { voice: false, settings: { advTextIntervalMs: 0 } }); h.start(); h.line(); h.line(1);
  const button = h.app.stage.stage.querySelector('.adv-qm-btn-config'); button.focus();
  const event = { type: 'keydown', key }; button.dispatchEvent(event);
  assert.equal(Boolean(event.defaultPrevented), false);
  assert.equal(h.app.player.currentIndex, 0);
  // Model the browser default action, which only runs for unprevented keydowns.
  if (!event.defaultPrevented) button.click();
  assert.equal(h.calls.settings, 1);
});

test('rejected send keeps draft; accepted send clears it', t => {
  const h = setup(t); const input = h.app.textbox.inputElement;
  input.value = '保留草稿'; h.controller.send = () => false;
  input.dispatchEvent({ type: 'keydown', key: 'Enter' }); assert.equal(input.value, '保留草稿');
  h.controller.send = () => true;
  input.dispatchEvent({ type: 'keydown', key: 'Enter' }); assert.equal(input.value, '');
});
