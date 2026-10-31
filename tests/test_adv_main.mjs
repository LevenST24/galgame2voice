import assert from 'node:assert/strict';
import test from 'node:test';
// Import before installing a document: don't auto-boot authentication or devices.
import { setup } from './helpers/adv_main.mjs';
import { tick } from './helpers/chat_stream.mjs';
import { state as storedState, getActive as storedActive, getSession as storedSession, DEFAULT_SESSION_SETTINGS, DEFAULT_GLOBAL } from '../frontend/src/store.js';
import { initVoiceSettings, ensureSessionVoice, getVoiceProfiles, fetchVoiceProfiles, getActiveProfileId } from '../frontend/src/controllers/voice_settings.js';
import { createTitleScreen } from '../frontend/src/adv/title.js';
import { initApp } from '../frontend/src/main.js';
import { fakeDocument } from './helpers/adv_dom.mjs';

const ENGINE_NOT_LOADED = '语音模型尚未加载。请在「全局设置 → 状态诊断」检查语音状态。';
async function homeVoiceFixture(t, { status = 502, defer = false } = {}) {
  storedState.sessions = [1, 2].map(id => ({ id: `home_${id}`, title: '假会话', updatedAt: id,
    settings: { ...DEFAULT_SESSION_SETTINGS, voiceProfileId: id }, messages: [{ role: 'user', content: '你好' }] }));
  storedState.activeId = 'home_1';
  const requests = [];
  let finishSwitch;
  const h = setup(t, { callbacks: { state: storedState, getActive: storedActive, getSession: storedSession, getVoiceProfiles },
    fetch: async (url, options = {}) => {
      if (url === '/api/voice/profiles') return Response.json({ profiles: status === 404 && requests.length ? [] : [{ id: 1, name: '甲' }, { id: 2, name: '乙' }], active_profile_id: 99 });
      if (url === '/api/voice/switch') {
        requests.push(JSON.parse(options.body).profile_id);
        const response = () => Response.json(status === 200 ? { profile: '假角色' } : { detail: ENGINE_NOT_LOADED }, { status });
        return defer ? new Promise(resolve => { finishSwitch = () => resolve(response()); }) : response();
      }
      return Response.json({ backgrounds: [] });
    } });
  const toasts = document.createElement('div'); toasts.id = 'toastRoot'; document.body.appendChild(toasts);
  t.after(() => { for (const toast of toasts.children) clearTimeout(toast._timer); });
  const stage = h.app.adv.stage.stage;
  initVoiceSettings({}, { isHomeOpen: () => stage.classList.contains('is-home') });
  await fetchVoiceProfiles();
  const home = createTitleScreen(h.app.adv.stage.overlay, { documentObj: document, actions: {
    ...h.app.actions, getStatus: async () => ({ modelLoaded: true, hasModel: true, model: { state: 'ok' }, engine: { state: 'unavailable' } }),
  } });
  t.after(() => home.destroy());
  return { ...h, home, stage, requests, toasts, finish: async () => { finishSwitch?.(); await tick(); } };
}

test('home suppresses startup failure by result-time state, including requests sent before home opens', async t => {
  const h = await homeVoiceFixture(t, { defer: true });
  const pending = ensureSessionVoice(storedActive()); await tick();
  assert.equal(h.stage.classList.contains('is-home'), false);
  h.home.open(); await tick(); await h.finish(); await pending;
  assert.deepEqual(h.requests, [1]); assert.equal(h.toasts.children.length, 0);
});

test('home preview success is silent; entering chat verifies the engine without another success toast', async t => {
  const h = await homeVoiceFixture(t, { status: 200 }); h.home.open(); await tick();
  const preview = h.app.actions.previewSession('home_2'); await tick(); await h.finish(); await preview;
  assert.equal(getActiveProfileId(), 2); assert.equal(h.toasts.children.length, 0);
  await h.home.refresh();
  h.home.element.querySelector('.home-continue').click(); await tick();
  assert.equal(h.stage.classList.contains('is-home'), false);
  assert.deepEqual(h.requests, [2, 2]); assert.equal(h.toasts.children.length, 0);
});

test('continuing the same session after silent home failure retries once and reports the failure in ADV', async t => {
  const h = await homeVoiceFixture(t); h.home.open(); await tick();
  const pending = ensureSessionVoice(storedActive()); await tick(); await h.finish(); await pending;
  assert.equal(h.toasts.children.length, 0);
  h.home.element.querySelector('.home-continue').click(); await tick();
  assert.deepEqual(h.requests, [1, 1]); await h.finish();
  assert.equal(h.toasts.children.length, 1); assert.match(h.toasts.children[0].dataset.msg, /音色切换失败: 语音模型尚未加载/);
});

test('home visibility callback suppresses session-settings voice results and uses visibility when results arrive', async t => {
  const h = await homeVoiceFixture(t, { status: 200, defer: true }); h.home.open(); await tick();
  // Session settings saves call this same controller path after closing the modal.
  let pending = ensureSessionVoice(storedActive()); await tick(); await h.finish(); await pending;
  assert.equal(h.toasts.children.length, 0);
  pending = ensureSessionVoice(storedActive()); await tick(); h.home.close(); await h.finish(); await pending;
  assert.equal(h.toasts.children.length, 1); assert.match(h.toasts.children[0].dataset.msg, /已切换音色/);
});

for (const entry of ['startNew', 'startNewWith']) test(`home ${entry} switches a newly selected voice exactly once`, async t => {
  const h = await homeVoiceFixture(t); h.home.open(); await tick(); h.home.close();
  h.app.actions[entry]({ voiceProfileId: 2, advBackground: '', advBgTime: 'auto' }); await tick();
  assert.deepEqual(h.requests, [entry === 'startNew' ? 1 : 2]); await h.finish();
  assert.equal(h.toasts.children.length, 1);
});

test('home entry verifies a saved active voice and skips an unbound session', async t => {
  const h = await homeVoiceFixture(t, { status: 200 }); h.home.open(); await tick();
  const pending = ensureSessionVoice(storedActive()); await tick(); await h.finish(); await pending;
  h.home.close(); h.app.actions.startNew(); await tick();
  assert.deepEqual(h.requests, [1, 1]);
  storedActive().settings.voiceProfileId = null;
  await h.app.actions.loadSession(storedActive().id); await tick();
  assert.deepEqual(h.requests, [1, 1]); assert.equal(h.toasts.children.length, 0);
});

test('home reports a confirmed deleted voice while preserving the explicit binding', async t => {
  const h = await homeVoiceFixture(t, { status: 404 }); h.home.open(); await tick();
  await ensureSessionVoice(storedActive());
  assert.equal(storedActive().settings.voiceProfileId, 1);
  assert.equal(h.toasts.children.length, 1); assert.match(h.toasts.children[0].dataset.msg, /音色已被删除，原选择已保留/);
});

for (const status of [200, 502]) test(`ADV drawer switches keep notifications after leaving home: ${status}`, async t => {
  const h = await homeVoiceFixture(t, { status });
  await h.app.switchSession('home_2'); await tick();
  assert.deepEqual(h.requests, [2]); assert.match(h.toasts.children[0].dataset.msg, status === 200 ? /已切换音色/ : /音色切换失败/);
});

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

test('stored advEnabled false opens in ADV and all session switches reuse the stage', async t => {
  const h = setup(t);
  const original = h.app.adv;
  assert.ok(original);
  h.sessions[0].settings.advEnabled = false; h.app.fullRender();
  assert.equal(h.app.adv, original);
  await h.app.switchSession('s_1');
  assert.equal(h.app.adv, original);
  assert.equal(h.app.adv.player.settings.revealMsPerGrapheme, 100);
  await h.app.switchSession('s_0');
  assert.equal(h.app.adv.player.settings.revealMsPerGrapheme, 0);
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

test('legacy disabled setting cannot cancel a live turn or unmount the only renderer', async t => {
  const h = setup(t); h.app.sendMessage('问题'); await tick();
  await h.stream.requests[0].event('line', { index: 0, zh: '已收到。', ja: '', emotion: 'gentle', act: {} });
  const stage = h.app.adv;
  h.sessions[0].settings.advEnabled = false; h.app.fullRender();
  assert.equal(h.app.conversation.busy, true); assert.equal(h.app.adv, stage);
  h.app.conversation.stop();
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
  const overlay = h.app.adv.stage.stage.querySelector('.adv-log-overlay');
  assert.ok(overlay, 'LOG overlay should be open');
  assert.ok(overlay.querySelectorAll('.adv-log-primary').some(node => node.textContent === 'partial final'));
});

test('ADV input keeps IME Enter native and sends normal Enter', async t => {
  const h = setup(t), input = h.app.adv.textbox.inputElement;
  input.value = '测试';
  input.dispatchEvent({ type: 'keydown', key: 'Enter', isComposing: true });
  input.dispatchEvent({ type: 'keydown', key: 'Enter', keyCode: 229 });
  assert.equal(h.stream.requests.length, 0);
  input.dispatchEvent({ type: 'keydown', key: 'Enter' });
  await tick(); assert.equal(h.stream.requests.length, 1);
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

test('offline voice engine cannot block ADV art or let an old engine completion select a different character', async t => {
  storedState.sessions = [2, 3].map(id => ({ id: `art_${id}`, title: '角色', messages: [],
    settings: { ...DEFAULT_SESSION_SETTINGS, voiceProfileId: id } }));
  storedState.activeId = 'art_2';
  let finishSwitch;
  const portrait = id => ({ enabled: true, defaultCostume: `outfit_${id}`, costumes: [{ id: `outfit_${id}` }],
    sprites: { [`outfit_${id}`]: { smile: { file: `/art_${id}.png`, width: 100, height: 200 } } },
    faces: { smile: { primary: 'gentle' } }, expressionSets: { gentle: [{ id: 'smile' }] } });
  const h = setup(t, { callbacks: { state: storedState, getActive: storedActive, getSession: storedSession, getVoiceProfiles },
    fetch: async (url, options = {}) => {
      if (url === '/api/voice/profiles') return Response.json({ profiles: [{ id: 2, name: '甲' }, { id: 3, name: '乙' }], active_profile_id: null });
      if (url === '/api/voice/switch') {
        if (JSON.parse(options.body).profile_id === 2) return new Promise(resolve => { finishSwitch = resolve; });
        return Response.json({ detail: '引擎离线' }, { status: 503 });
      }
      const id = /\/characters\/(\d+)\/portrait/.exec(url)?.[1];
      return Response.json(id ? portrait(id) : { backgrounds: [] });
    } });
  initVoiceSettings({});
  const pending = ensureSessionVoice(storedActive()); await tick();
  assert.equal(h.app.adv.sprite.getModel().defaultCostume, 'outfit_2');
  await h.app.switchSession('art_3'); await tick();
  assert.equal(storedActive().settings.voiceProfileId, 3);
  assert.equal(h.app.adv.sprite.getModel().defaultCostume, 'outfit_3');
  finishSwitch(Response.json({ profile: '甲' })); await pending; await tick();
  assert.equal(h.app.adv.sprite.getModel().defaultCostume, 'outfit_3');
  assert.equal(storedActive().settings.voiceProfileId, 3);
});

test('home preview switches real session/stage without stealing focus for the hidden input', async t => {
  const h = setup(t);
  h.app.adv.stage.stage.classList.add('is-home');
  const button = document.createElement('button'); h.app.adv.stage.overlay.appendChild(button); button.focus();
  h.sessions[1].messages.push({ role: 'assistant', content: '假会话台词' });
  await h.app.actions.previewSession('s_1');
  assert.equal(h.state.activeId, 's_1'); assert.ok(document.activeElement === button, 'home preview must preserve focus');
  assert.equal(h.app.actions.getActiveId(), 's_1'); assert.equal(h.app.actions.listSessions().find(s => s.id === 's_1').excerpt.zh, '假会话台词');
});

for (const [name, speed] of [['高楯欧丽叶', .88], ['常陆茉子', .90], ['白雪乃爱', 1.05], ['假角色', 1]]) {
  test(`home newWith reuses newChat and character default speed: ${name}`, async t => {
    storedState.sessions = [{ id: 'old_home', settings: { ...DEFAULT_SESSION_SETTINGS, voiceProfileId: 9, ttsSpeed: 1.8 }, messages: [] }];
    storedState.activeId = 'old_home';
    const h = setup(t, { callbacks: { state: storedState, getActive: storedActive, getSession: storedSession,
      getVoiceProfiles: () => [{ id: 7, name }] } });
    const old = structuredClone(storedActive().settings);
    const created = h.app.actions.startNewWith({ voiceProfileId: 7, advBackground: 'fake/sea', advBgTime: 'evening' });
    assert.equal(storedActive(), created); assert.equal(created.settings.voiceProfileId, 7);
    assert.equal(created.settings.advBackground, 'fake/sea'); assert.equal(created.settings.advBgTime, 'evening');
    assert.equal(created.settings.ttsSpeed, speed); assert.deepEqual(storedSession('old_home').settings, old);
    const next = h.app.actions.startNew(); assert.equal(next.settings.voiceProfileId, 7); assert.equal(next.settings.ttsSpeed, speed);
    await tick();
  });
}

test('real startup wiring silences both startup switches and all-session entry retries with an ADV status action', async t => {
  const doc = fakeDocument(); globalThis.document = doc;
  doc.querySelectorAll = selector => doc.body.querySelectorAll(selector);
  const root = doc.createElement('div'); root.id = 'advRoot'; doc.body.appendChild(root);
  const toasts = doc.createElement('div'); toasts.id = 'toastRoot'; doc.body.appendChild(toasts);
  // Fixed times keep 全部会话 in this order; times filled in at load can land a millisecond apart.
  storedState.sessions = [{ id: 'startup_home', title: '假启动会话', updatedAt: 2000, settings: { ...DEFAULT_SESSION_SETTINGS, voiceProfileId: 1 },
    messages: [{ id: 'startup_msg', role: 'user', content: '你好' }] },
  { id: 'other_home', title: '另一个假会话', updatedAt: 1000, settings: { ...DEFAULT_SESSION_SETTINGS }, messages: [] }];
  storedState.activeId = 'startup_home';
  const storage = new Map([['gal2voice.chat.v1', JSON.stringify(storedState)]]);
  globalThis.localStorage = { getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value) };
  let home, defer = true;
  const pending = [], switches = [];
  globalThis.location = { origin: 'http://home.test' };
  globalThis.window = { innerWidth: 1280, location: { search: '', hash: '' },
    sessionStorage: { getItem: () => '', setItem() {} },
    fetch: async (url, options = {}) => {
      if (url === '/api/voice/profiles') return Response.json({ profiles: [{ id: 1, name: '甲', work: 'a' }], active_profile_id: 99 });
      if (url === '/api/voice/switch') {
        switches.push(JSON.parse(options.body).profile_id);
        const failure = () => Response.json({ detail: ENGINE_NOT_LOADED }, { status: 502 });
        return defer ? new Promise(resolve => pending.push(() => resolve(failure()))) : failure();
      }
      return Response.json({ sessions: [], has_more: false, providers: [], backgrounds: [{ key: 'a/room', name: '房间', work: 'a' }] });
    } };
  globalThis.fetch = (...args) => window.fetch(...args);
  await fetchVoiceProfiles();
  const app = initApp({ dom: { advRoot: root }, renderSessionList() {}, showTitle: actions => {
    home = createTitleScreen(app.adv.stage.overlay, { documentObj: doc, actions: {
      ...actions, getStatus: async () => ({ modelLoaded: true, hasModel: true, model: { state: 'ok' }, engine: { state: 'unavailable' } }),
    } }); home.open();
  } });
  t.after(async () => {
    defer = false;
    for (const finish of pending) finish(); await tick();
    home?.destroy(); app.conversation.stop(); app.adv.unmount();
    for (const toast of toasts.children) clearTimeout(toast._timer);
    delete globalThis.document; delete globalThis.location;
  });
  await app.ready; await tick();
  assert.deepEqual(switches, [1], 'startup retries wait for the in-flight model load');
  defer = false;
  for (const finish of pending) finish(); await tick();
  assert.deepEqual(switches, [1, 1]);
  assert.equal(toasts.children.length, 0);
  assert.deepEqual(app.actions.listCharacters(), [{ id: 1, name: '甲', work: 'a' }]);
  assert.deepEqual(app.actions.getBackgrounds(), [{ key: 'a/room', name: '房间', work: 'a' }]);
  defer = false;
  home.element.querySelector('.home-all').click();
  home.element.querySelector('.adv-title-slot').click(); await tick();
  assert.deepEqual(switches, [1, 1, 1]); assert.equal(toasts.children.length, 1);
  assert.equal(toasts.querySelector('.toast-action-btn').textContent, '检查语音状态');
});


test('a startup voice switch that fails before home opens stays quiet', async t => {
  // The switch starts before the session sync, and a stopped engine answers at once.
  const doc = fakeDocument(); globalThis.document = doc;
  doc.querySelectorAll = selector => doc.body.querySelectorAll(selector);
  const root = doc.createElement('div'); root.id = 'advRoot'; doc.body.appendChild(root);
  const toasts = doc.createElement('div'); toasts.id = 'toastRoot'; doc.body.appendChild(toasts);
  storedState.sessions = [{ id: 'early_home', title: '假启动会话', settings: { ...DEFAULT_SESSION_SETTINGS, voiceProfileId: 1 },
    messages: [{ id: 'early_msg', role: 'user', content: '你好' }] }];
  storedState.activeId = 'early_home';
  const storage = new Map([['gal2voice.chat.v1', JSON.stringify(storedState)]]);
  globalThis.localStorage = { getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value) };
  let home, releaseSync;
  const syncGate = new Promise(resolve => { releaseSync = resolve; });
  const switches = [];
  globalThis.location = { origin: 'http://home.test' };
  globalThis.window = { innerWidth: 1280, location: { search: '', hash: '' },
    sessionStorage: { getItem: () => '', setItem() {} },
    fetch: async (url, options = {}) => {
      if (url === '/api/voice/profiles') return Response.json({ profiles: [{ id: 1, name: '甲', work: 'a' }], active_profile_id: 99 });
      if (url === '/api/voice/switch') {
        switches.push(JSON.parse(options.body).profile_id);
        return Response.json({ detail: ENGINE_NOT_LOADED }, { status: 502 });
      }
      if (String(url).startsWith('/api/chat/sessions')) await syncGate;
      return Response.json({ sessions: [], has_more: false, providers: [], backgrounds: [] });
    } };
  globalThis.fetch = (...args) => window.fetch(...args);
  await fetchVoiceProfiles();
  const app = initApp({ dom: { advRoot: root }, renderSessionList() {}, showTitle: actions => {
    home = createTitleScreen(app.adv.stage.overlay, { documentObj: doc, actions: {
      ...actions, getStatus: async () => ({ modelLoaded: true, hasModel: true, model: { state: 'ok' }, engine: { state: 'unavailable' } }),
    } }); home.open();
  } });
  t.after(() => {
    home?.destroy(); app.conversation.stop(); app.adv.unmount();
    for (const toast of toasts.children) clearTimeout(toast._timer);
    delete globalThis.document; delete globalThis.location;
  });
  for (let i = 0; i < 5; i++) await tick();
  assert.deepEqual(switches, [1]); assert.equal(home, undefined);
  assert.equal(toasts.children.length, 0);
  releaseSync(); await app.ready; for (let i = 0; i < 5; i++) await tick();
  assert.deepEqual(switches, [1, 1]); assert.equal(home.isOpen, true);
  assert.equal(toasts.children.length, 0);
});

test('startup sync reloads session history when server has more messages than local copy (refresh keeps server reply)', async t => {
  const doc = fakeDocument();
  globalThis.document = doc;
  doc.querySelectorAll = selector => doc.body.querySelectorAll(selector);
  const root = doc.createElement('div');
  root.id = 'advRoot';
  doc.body.appendChild(root);
  const toasts = doc.createElement('div');
  toasts.id = 'toastRoot';
  doc.body.appendChild(toasts);

  const session = {
    id: 's_refresh',
    title: '晚上的对话',
    createdAt: 1_700_000_000_000,
    updatedAt: 1_700_000_100_000,
    settings: { ...DEFAULT_SESSION_SETTINGS, voiceProfileId: null },
    messages: [{ id: 'm_local_user', role: 'user', content: '今天去哪', ts: 1_700_000_100_000 }],
  };
  const server = {
    id: session.id,
    title: session.title,
    voice_profile_id: null,
    created_at: '2024-06-01 00:00:00',
    updated_at: '2024-06-01 00:00:02',
    message_count: 2,
    last_message: '去江边走走吧',
    settings: { ...session.settings },
  };

  const storage = new Map([['gal2voice.chat.v1', JSON.stringify({
    sessions: [session],
    activeId: session.id,
    global: { ...DEFAULT_GLOBAL },
  })]]);

  globalThis.localStorage = {
    getItem: key => storage.get(key) ?? null,
    setItem: (key, value) => storage.set(key, value),
    removeItem: key => storage.delete(key),
  };
  globalThis.location = { origin: 'http://127.0.0.1:8000' };
  globalThis.window = {
    innerWidth: 1280,
    location: { search: '', hash: '', origin: 'http://127.0.0.1:8000' },
    sessionStorage: { getItem: () => '', setItem() {}, removeItem() {} },
    prompt: () => null,
  };

  const calls = [];
  globalThis.window.fetch = async (url, options = {}) => {
    const target = String(url);
    calls.push({ url: target, method: options.method || 'GET', body: options.body });
    if (target.includes('/api/chat/sessions')) {
      return Response.json({ sessions: [server], has_more: false, count: 1 });
    }
    if (target.includes('/api/chat/history')) {
      return Response.json({
        session_id: session.id,
        has_more: false,
        oldest_id: 10,
        count: 2,
        messages: [
          { id: 10, role: 'user', content_chinese: '今天去哪', content_japanese: '', created_at: '2024-06-01 00:00:00' },
          { id: 11, role: 'assistant', content_chinese: '去江边走走吧', content_japanese: '海辺へ', created_at: '2024-06-01 00:00:02' },
        ],
      });
    }
    if (target.includes('/api/voice/profiles')) return Response.json({ profiles: [], active_profile_id: null });
    if (target.includes('/api/providers')) return Response.json({
      providers: [{ id: 'p1', name: '测试模型', api_key: 'sk-test', chat_model: 'gpt-4o', is_active: true }],
      active_provider: 'p1',
    });
    if (target.includes('/api/backgrounds')) return Response.json({ backgrounds: [] });
    if (target.includes('/api/system/engine-status')) return Response.json({ state: 'ready' });
    return Response.json({});
  };
  globalThis.fetch = (...args) => globalThis.window.fetch(...args);

  const app = initApp({
    dom: { advRoot: root },
    renderSessionList() {},
  });

  t.after(() => {
    try { app.conversation.stop(); app.adv?.unmount(); } catch {}
    for (const toast of toasts.children) clearTimeout(toast._timer);
    delete globalThis.document;
    delete globalThis.window;
    delete globalThis.localStorage;
    delete globalThis.fetch;
    delete globalThis.location;
  });

  await app.ready;
  for (let i = 0; i < 5; i++) await tick();

  // 1. 断言首页最后一句包含服务端已保存的回复
  const homeQuote = doc.querySelector('.home-quote')?.textContent || '';
  assert.ok(homeQuote.includes('去江边走走吧'), `首页最后一句应包含「去江边走走吧」，实际为: "${homeQuote}"`);

  // 2. 点击「接着聊 →」进入对话
  const continueBtn = doc.querySelector('.home-continue');
  assert.ok(continueBtn, '应存在继续对话按钮');
  continueBtn.click();
  for (let i = 0; i < 5; i++) await tick();

  // 3. 断言活动对话包含服务端已保存的回复
  const activeSession = storedActive();
  const shownContents = activeSession.messages.map(m => m.content);
  assert.deepEqual(shownContents, ['今天去哪', '去江边走走吧'], '对话消息列表应包含服务端回复');

  // 4. 打开 LOG，断言 LOG 包含两句
  app.adv.action('log');
  for (let i = 0; i < 5; i++) await tick();
  const logEntries = [...doc.querySelectorAll('.adv-log-primary')].map(el => el.textContent).join(' ');
  assert.ok(logEntries.includes('今天去哪') && logEntries.includes('去江边走走吧'),
    `LOG 历史应包含用户和助手两句，实际为: "${logEntries}"`);

  // 5. 断言 localStorage 中也是两句且包含服务端回复
  const storedJson = JSON.parse(storage.get('gal2voice.chat.v1'));
  const storedSession = storedJson.sessions.find(s => s.id === session.id);
  assert.equal(storedSession.messages.length, 2, 'localStorage 里应包含两条消息');
  assert.equal(storedSession.messages[0].content, '今天去哪');
  assert.equal(storedSession.messages[1].content, '去江边走走吧');
});

test('startup sync resets messages of non-active session when server has more messages and loads on switch', async t => {
  const doc = fakeDocument();
  globalThis.document = doc;
  doc.querySelectorAll = selector => doc.body.querySelectorAll(selector);
  const root = doc.createElement('div');
  root.id = 'advRoot';
  doc.body.appendChild(root);
  const toasts = doc.createElement('div');
  toasts.id = 'toastRoot';
  doc.body.appendChild(toasts);

  const activeSession = {
    id: 's_active',
    title: '当前会话',
    createdAt: 1_700_000_000_000,
    updatedAt: 1_700_000_100_000,
    settings: { ...DEFAULT_SESSION_SETTINGS, voiceProfileId: null },
    messages: [
      { id: 'm_1', role: 'user', content: '你好' },
      { id: 'm_2', role: 'assistant', content: '你好呀' },
    ],
  };
  const otherSession = {
    id: 's_other',
    title: '另一个对话',
    createdAt: 1_700_000_000_000,
    updatedAt: 1_700_000_200_000,
    settings: { ...DEFAULT_SESSION_SETTINGS, voiceProfileId: null },
    messages: [{ id: 'm_local_user2', role: 'user', content: '晚饭吃什么', ts: 1_700_000_200_000 }],
  };

  const serverSessions = [
    {
      id: activeSession.id,
      title: activeSession.title,
      voice_profile_id: null,
      created_at: '2024-06-01 00:00:00',
      updated_at: '2024-06-01 00:00:01',
      message_count: 2,
      last_message: '你好呀',
      settings: { ...activeSession.settings },
    },
    {
      id: otherSession.id,
      title: otherSession.title,
      voice_profile_id: null,
      created_at: '2024-06-01 00:00:00',
      updated_at: '2024-06-01 00:00:03',
      message_count: 2,
      last_message: '吃拉面吧',
      settings: { ...otherSession.settings },
    },
  ];

  const storage = new Map([['gal2voice.chat.v1', JSON.stringify({
    sessions: [activeSession, otherSession],
    activeId: activeSession.id,
    global: { ...DEFAULT_GLOBAL },
  })]]);

  globalThis.localStorage = {
    getItem: key => storage.get(key) ?? null,
    setItem: (key, value) => storage.set(key, value),
    removeItem: key => storage.delete(key),
  };
  globalThis.location = { origin: 'http://127.0.0.1:8000' };
  globalThis.window = {
    innerWidth: 1280,
    location: { search: '', hash: '', origin: 'http://127.0.0.1:8000' },
    sessionStorage: { getItem: () => '', setItem() {}, removeItem() {} },
    prompt: () => null,
  };

  globalThis.window.fetch = async (url) => {
    const target = String(url);
    if (target.includes('/api/chat/sessions')) {
      return Response.json({ sessions: serverSessions, has_more: false, count: 2 });
    }
    if (target.includes('/api/chat/history') && target.includes('session_id=s_other')) {
      return Response.json({
        session_id: otherSession.id,
        has_more: false,
        oldest_id: 20,
        count: 2,
        messages: [
          { id: 20, role: 'user', content_chinese: '晚饭吃什么', content_japanese: '', created_at: '2024-06-01 00:00:02' },
          { id: 21, role: 'assistant', content_chinese: '吃拉面吧', content_japanese: 'ラーメン', created_at: '2024-06-01 00:00:03' },
        ],
      });
    }
    if (target.includes('/api/voice/profiles')) return Response.json({ profiles: [], active_profile_id: null });
    if (target.includes('/api/providers')) return Response.json({
      providers: [{ id: 'p1', name: '测试模型', api_key: 'sk-test', chat_model: 'gpt-4o', is_active: true }],
      active_provider: 'p1',
    });
    if (target.includes('/api/backgrounds')) return Response.json({ backgrounds: [] });
    if (target.includes('/api/system/engine-status')) return Response.json({ state: 'ready' });
    return Response.json({});
  };
  globalThis.fetch = (...args) => globalThis.window.fetch(...args);

  const app = initApp({
    dom: { advRoot: root },
    renderSessionList() {},
  });

  t.after(() => {
    try { app.conversation.stop(); app.adv?.unmount(); } catch {}
    for (const toast of toasts.children) clearTimeout(toast._timer);
    delete globalThis.document;
    delete globalThis.window;
    delete globalThis.localStorage;
    delete globalThis.fetch;
    delete globalThis.location;
  });

  await app.ready;
  for (let i = 0; i < 5; i++) await tick();

  // 1. 验证 actions.listSessions() 中 s_other 的 lastLine 来自服务端
  const sessionsList = app.actions.listSessions();
  const listedOther = sessionsList.find(s => s.id === otherSession.id);
  assert.equal(listedOther.lastLine, '吃拉面吧', '非活动会话在未切换时应展示服务端的 lastLine');

  // 2. 切换到 s_other
  await app.actions.loadSession(otherSession.id);
  for (let i = 0; i < 5; i++) await tick();

  // 3. 断言切换后成功加载历史
  const loadedOther = storedSession(otherSession.id);
  assert.equal(loadedOther.messages.length, 2, '切换后应拉取并保存完整消息');
  assert.equal(loadedOther.messages[0].content, '晚饭吃什么');
  assert.equal(loadedOther.messages[1].content, '吃拉面吧');

  // 4. 断言 localStorage 也是两句
  const storedJson = JSON.parse(storage.get('gal2voice.chat.v1'));
  const storedOther = storedJson.sessions.find(s => s.id === otherSession.id);
  assert.equal(storedOther.messages.length, 2, 'localStorage 里 s_other 应包含两条消息');
  assert.equal(storedOther.messages[1].content, '吃拉面吧');
});

test('startup sync failure retains local messages in memory and storage, and retrying reloads server reply', async t => {
  const doc = fakeDocument();
  globalThis.document = doc;
  doc.querySelectorAll = selector => doc.body.querySelectorAll(selector);
  const root = doc.createElement('div');
  root.id = 'advRoot';
  doc.body.appendChild(root);
  const toasts = doc.createElement('div');
  toasts.id = 'toastRoot';
  doc.body.appendChild(toasts);

  const session = {
    id: 's_fail_then_retry',
    title: '网络抖动的对话',
    createdAt: 1_700_000_000_000,
    updatedAt: 1_700_000_100_000,
    settings: { ...DEFAULT_SESSION_SETTINGS, voiceProfileId: null },
    messages: [{ id: 'm_local_user', role: 'user', content: '今天去哪', ts: 1_700_000_100_000 }],
  };
  const server = {
    id: session.id,
    title: session.title,
    voice_profile_id: null,
    created_at: '2024-06-01 00:00:00',
    updated_at: '2024-06-01 00:00:02',
    message_count: 2,
    last_message: '去江边走走吧',
    settings: { ...session.settings },
  };

  const storage = new Map([['gal2voice.chat.v1', JSON.stringify({
    sessions: [session],
    activeId: session.id,
    global: { ...DEFAULT_GLOBAL },
  })]]);

  globalThis.localStorage = {
    getItem: key => storage.get(key) ?? null,
    setItem: (key, value) => storage.set(key, value),
    removeItem: key => storage.delete(key),
  };
  globalThis.location = { origin: 'http://127.0.0.1:8000' };
  globalThis.window = {
    innerWidth: 1280,
    location: { search: '', hash: '', origin: 'http://127.0.0.1:8000' },
    sessionStorage: { getItem: () => '', setItem() {}, removeItem() {} },
    prompt: () => null,
  };

  let historyShouldFail = true;

  globalThis.window.fetch = async (url) => {
    const target = String(url);
    if (target.includes('/api/chat/sessions')) {
      return Response.json({ sessions: [server], has_more: false, count: 1 });
    }
    if (target.includes('/api/chat/history')) {
      if (historyShouldFail) {
        throw new Error('网络连接超时');
      }
      return Response.json({
        session_id: session.id,
        has_more: false,
        oldest_id: 10,
        count: 2,
        messages: [
          { id: 10, role: 'user', content_chinese: '今天去哪', content_japanese: '', created_at: '2024-06-01 00:00:00' },
          { id: 11, role: 'assistant', content_chinese: '去江边走走吧', content_japanese: '海辺へ', created_at: '2024-06-01 00:00:02' },
        ],
      });
    }
    if (target.includes('/api/voice/profiles')) return Response.json({ profiles: [], active_profile_id: null });
    if (target.includes('/api/providers')) return Response.json({
      providers: [{ id: 'p1', name: '测试模型', api_key: 'sk-test', chat_model: 'gpt-4o', is_active: true }],
      active_provider: 'p1',
    });
    if (target.includes('/api/backgrounds')) return Response.json({ backgrounds: [] });
    if (target.includes('/api/system/engine-status')) return Response.json({ state: 'ready' });
    return Response.json({});
  };
  globalThis.fetch = (...args) => globalThis.window.fetch(...args);

  const app = initApp({
    dom: { advRoot: root },
    renderSessionList() {},
  });

  t.after(() => {
    try { app.conversation.stop(); app.adv?.unmount(); } catch {}
    for (const toast of toasts.children) clearTimeout(toast._timer);
    delete globalThis.document;
    delete globalThis.window;
    delete globalThis.localStorage;
    delete globalThis.fetch;
    delete globalThis.location;
  });

  await app.ready;
  for (let i = 0; i < 5; i++) await tick();

  // 1. 历史请求失败后：内存中本地原消息保留
  const activeSession = storedActive();
  assert.equal(activeSession.messages.length, 1, '历史加载失败时，内存中本地消息应保留');
  assert.equal(activeSession.messages[0].content, '今天去哪');

  // 2. 存下来的状态（localStorage）也还在
  const storedJson = JSON.parse(storage.get('gal2voice.chat.v1'));
  const storedSessionData = storedJson.sessions.find(s => s.id === session.id);
  assert.equal(storedSessionData.messages.length, 1, '历史加载失败时，localStorage 中本地消息应保留');
  assert.equal(storedSessionData.messages[0].content, '今天去哪');

  // 3. 之后再次加载成功（模拟恢复连接后点击会话或继续聊天）
  historyShouldFail = false;
  await app.actions.loadSession(session.id);
  for (let i = 0; i < 5; i++) await tick();

  // 4. 补上服务端多出的回复
  assert.equal(activeSession.messages.length, 2, '重试成功后应补回服务端回复');
  assert.equal(activeSession.messages[0].content, '今天去哪');
  assert.equal(activeSession.messages[1].content, '去江边走走吧');

  const reStoredJson = JSON.parse(storage.get('gal2voice.chat.v1'));
  const reStoredSession = reStoredJson.sessions.find(s => s.id === session.id);
  assert.equal(reStoredSession.messages.length, 2, '重试成功后 localStorage 应保存完整两条消息');
  assert.equal(reStoredSession.messages[1].content, '去江边走走吧');
});

test('startup sync preserves voiceKey and dur on local user message when server adds assistant reply', async t => {
  const doc = fakeDocument();
  globalThis.document = doc;
  doc.querySelectorAll = selector => doc.body.querySelectorAll(selector);
  const root = doc.createElement('div');
  root.id = 'advRoot';
  doc.body.appendChild(root);
  const toasts = doc.createElement('div');
  toasts.id = 'toastRoot';
  doc.body.appendChild(toasts);

  const session = {
    id: 's_voice_preserve',
    title: '录音的对话',
    createdAt: 1_700_000_000_000,
    updatedAt: 1_700_000_100_000,
    settings: { ...DEFAULT_SESSION_SETTINGS, voiceProfileId: null },
    messages: [{
      id: 'm_local_user_voice',
      role: 'user',
      content: '今天去哪',
      voiceKey: 'user_m_local_user_voice',
      dur: 3.5,
      ts: 1_700_000_100_000,
    }],
  };
  const server = {
    id: session.id,
    title: session.title,
    voice_profile_id: null,
    created_at: '2024-06-01 00:00:00',
    updated_at: '2024-06-01 00:00:02',
    message_count: 2,
    last_message: '去江边走走吧',
    settings: { ...session.settings },
  };

  const storage = new Map([['gal2voice.chat.v1', JSON.stringify({
    sessions: [session],
    activeId: session.id,
    global: { ...DEFAULT_GLOBAL },
  })]]);

  globalThis.localStorage = {
    getItem: key => storage.get(key) ?? null,
    setItem: (key, value) => storage.set(key, value),
    removeItem: key => storage.delete(key),
  };
  globalThis.location = { origin: 'http://127.0.0.1:8000' };
  globalThis.window = {
    innerWidth: 1280,
    location: { search: '', hash: '', origin: 'http://127.0.0.1:8000' },
    sessionStorage: { getItem: () => '', setItem() {}, removeItem() {} },
    prompt: () => null,
  };

  globalThis.window.fetch = async (url) => {
    const target = String(url);
    if (target.includes('/api/chat/sessions')) {
      return Response.json({ sessions: [server], has_more: false, count: 1 });
    }
    if (target.includes('/api/chat/history')) {
      return Response.json({
        session_id: session.id,
        has_more: false,
        oldest_id: 10,
        count: 2,
        messages: [
          { id: 10, role: 'user', content_chinese: '今天去哪', content_japanese: '', created_at: '2024-06-01 00:00:00' },
          { id: 11, role: 'assistant', content_chinese: '去江边走走吧', content_japanese: '海辺へ', created_at: '2024-06-01 00:00:02' },
        ],
      });
    }
    if (target.includes('/api/voice/profiles')) return Response.json({ profiles: [], active_profile_id: null });
    if (target.includes('/api/providers')) return Response.json({
      providers: [{ id: 'p1', name: '测试模型', api_key: 'sk-test', chat_model: 'gpt-4o', is_active: true }],
      active_provider: 'p1',
    });
    if (target.includes('/api/backgrounds')) return Response.json({ backgrounds: [] });
    if (target.includes('/api/system/engine-status')) return Response.json({ state: 'ready' });
    return Response.json({});
  };
  globalThis.fetch = (...args) => globalThis.window.fetch(...args);

  const app = initApp({
    dom: { advRoot: root },
    renderSessionList() {},
  });

  t.after(() => {
    try { app.conversation.stop(); app.adv?.unmount(); } catch {}
    for (const toast of toasts.children) clearTimeout(toast._timer);
    delete globalThis.document;
    delete globalThis.window;
    delete globalThis.localStorage;
    delete globalThis.fetch;
    delete globalThis.location;
  });

  await app.ready;
  for (let i = 0; i < 5; i++) await tick();

  // 1. 刷新后两条都在
  const activeSession = storedActive();
  assert.equal(activeSession.messages.length, 2, '刷新后应有两条消息');
  assert.equal(activeSession.messages[0].role, 'user');
  assert.equal(activeSession.messages[0].content, '今天去哪');
  assert.equal(activeSession.messages[1].role, 'assistant');
  assert.equal(activeSession.messages[1].content, '去江边走走吧');

  // 2. 用户消息仍带原来的 voiceKey 和 dur
  assert.equal(activeSession.messages[0].voiceKey, 'user_m_local_user_voice', '用户消息应保留原来的 voiceKey');
  assert.equal(activeSession.messages[0].dur, 3.5, '用户消息应保留原来的 dur');

  // 3. localStorage 中也保存了 voiceKey 和 dur
  const storedJson = JSON.parse(storage.get('gal2voice.chat.v1'));
  const storedSessionData = storedJson.sessions.find(s => s.id === session.id);
  assert.equal(storedSessionData.messages.length, 2);
  assert.equal(storedSessionData.messages[0].voiceKey, 'user_m_local_user_voice');
  assert.equal(storedSessionData.messages[0].dur, 3.5);
});

test('startup sync correctly pairs voiceKey when earlier of duplicate messages had voice', async t => {
  const doc = fakeDocument();
  globalThis.document = doc;
  doc.querySelectorAll = selector => doc.body.querySelectorAll(selector);
  const root = doc.createElement('div');
  root.id = 'advRoot';
  doc.body.appendChild(root);
  const toasts = doc.createElement('div');
  toasts.id = 'toastRoot';
  doc.body.appendChild(toasts);

  const session = {
    id: 's_voice_duplicate',
    title: '重复消息的对话',
    createdAt: 1_700_000_000_000,
    updatedAt: 1_700_000_200_000,
    settings: { ...DEFAULT_SESSION_SETTINGS, voiceProfileId: null },
    messages: [
      {
        id: 'm_user_1',
        role: 'user',
        content: '今天去哪',
        voiceKey: 'user_m_user_1',
        dur: 2.0,
        ts: 1_700_000_100_000,
      },
      {
        id: 'm_assistant_1',
        role: 'assistant',
        content: '去公园吧',
        ts: 1_700_000_150_000,
      },
      {
        id: 'm_user_2',
        role: 'user',
        content: '今天去哪',
        ts: 1_700_000_200_000,
      },
    ],
  };
  const server = {
    id: session.id,
    title: session.title,
    voice_profile_id: null,
    created_at: '2024-06-01 00:00:00',
    updated_at: '2024-06-01 00:00:03',
    message_count: 4,
    last_message: '不是刚去过公园吗',
    settings: { ...session.settings },
  };

  const storage = new Map([['gal2voice.chat.v1', JSON.stringify({
    sessions: [session],
    activeId: session.id,
    global: { ...DEFAULT_GLOBAL },
  })]]);

  globalThis.localStorage = {
    getItem: key => storage.get(key) ?? null,
    setItem: (key, value) => storage.set(key, value),
    removeItem: key => storage.delete(key),
  };
  globalThis.location = { origin: 'http://127.0.0.1:8000' };
  globalThis.window = {
    innerWidth: 1280,
    location: { search: '', hash: '', origin: 'http://127.0.0.1:8000' },
    sessionStorage: { getItem: () => '', setItem() {}, removeItem() {} },
    prompt: () => null,
  };

  globalThis.window.fetch = async (url) => {
    const target = String(url);
    if (target.includes('/api/chat/sessions')) {
      return Response.json({ sessions: [server], has_more: false, count: 1 });
    }
    if (target.includes('/api/chat/history')) {
      return Response.json({
        session_id: session.id,
        has_more: false,
        oldest_id: 10,
        count: 4,
        messages: [
          { id: 10, role: 'user', content_chinese: '今天去哪', content_japanese: '', created_at: '2024-06-01 00:00:00' },
          { id: 11, role: 'assistant', content_chinese: '去公园吧', content_japanese: '', created_at: '2024-06-01 00:00:01' },
          { id: 12, role: 'user', content_chinese: '今天去哪', content_japanese: '', created_at: '2024-06-01 00:00:02' },
          { id: 13, role: 'assistant', content_chinese: '不是刚去过公园吗', content_japanese: '', created_at: '2024-06-01 00:00:03' },
        ],
      });
    }
    if (target.includes('/api/voice/profiles')) return Response.json({ profiles: [], active_profile_id: null });
    if (target.includes('/api/providers')) return Response.json({
      providers: [{ id: 'p1', name: '测试模型', api_key: 'sk-test', chat_model: 'gpt-4o', is_active: true }],
      active_provider: 'p1',
    });
    if (target.includes('/api/backgrounds')) return Response.json({ backgrounds: [] });
    if (target.includes('/api/system/engine-status')) return Response.json({ state: 'ready' });
    return Response.json({});
  };
  globalThis.fetch = (...args) => globalThis.window.fetch(...args);

  const app = initApp({
    dom: { advRoot: root },
    renderSessionList() {},
  });

  t.after(() => {
    try { app.conversation.stop(); app.adv?.unmount(); } catch {}
    for (const toast of toasts.children) clearTimeout(toast._timer);
    delete globalThis.document;
    delete globalThis.window;
    delete globalThis.localStorage;
    delete globalThis.fetch;
    delete globalThis.location;
  });

  await app.ready;
  for (let i = 0; i < 5; i++) await tick();

  const activeSession = storedActive();
  assert.equal(activeSession.messages.length, 4);
  // Turn 1 had voice, Turn 2 did not!
  assert.equal(activeSession.messages[0].voiceKey, 'user_m_user_1', '第一句（发语音的）应获得 voiceKey');
  assert.equal(activeSession.messages[0].dur, 2.0);
  assert.equal(activeSession.messages[2].voiceKey, undefined, '第二句（打字的）不应被错误配上 voiceKey');
});

test('title screen excerpt presents server last_message when multi-turn session is behind server', async t => {
  const doc = fakeDocument();
  globalThis.document = doc;
  doc.querySelectorAll = selector => doc.body.querySelectorAll(selector);
  const root = doc.createElement('div');
  root.id = 'advRoot';
  doc.body.appendChild(root);
  const toasts = doc.createElement('div');
  toasts.id = 'toastRoot';
  doc.body.appendChild(toasts);

  const session = {
    id: 's_multiturn_behind',
    title: '多轮会话',
    createdAt: 1_700_000_000_000,
    updatedAt: 1_700_000_200_000,
    settings: { ...DEFAULT_SESSION_SETTINGS, voiceProfileId: null },
    messages: [
      { id: 'm_1', role: 'user', content: '第一问' },
      { id: 'm_2', role: 'assistant', content: '第一答' },
      { id: 'm_3', role: 'user', content: '第二问' },
    ],
  };
  const server = {
    id: session.id,
    title: session.title,
    voice_profile_id: null,
    created_at: '2024-06-01 00:00:00',
    updated_at: '2024-06-01 00:00:03',
    message_count: 4,
    last_message: '第二答',
    settings: { ...session.settings },
  };

  const storage = new Map([['gal2voice.chat.v1', JSON.stringify({
    sessions: [session],
    activeId: session.id,
    global: { ...DEFAULT_GLOBAL },
  })]]);

  globalThis.localStorage = {
    getItem: key => storage.get(key) ?? null,
    setItem: (key, value) => storage.set(key, value),
    removeItem: key => storage.delete(key),
  };
  globalThis.location = { origin: 'http://127.0.0.1:8000' };
  globalThis.window = {
    innerWidth: 1280,
    location: { search: '', hash: '', origin: 'http://127.0.0.1:8000' },
    sessionStorage: { getItem: () => '', setItem() {}, removeItem() {} },
    prompt: () => null,
  };

  globalThis.window.fetch = async (url) => {
    const target = String(url);
    if (target.includes('/api/chat/sessions')) {
      return Response.json({ sessions: [server], has_more: false, count: 1 });
    }
    if (target.includes('/api/chat/history')) {
      throw new Error('网络连接超时');
    }
    if (target.includes('/api/voice/profiles')) return Response.json({ profiles: [], active_profile_id: null });
    if (target.includes('/api/providers')) return Response.json({
      providers: [{ id: 'p1', name: '测试模型', api_key: 'sk-test', chat_model: 'gpt-4o', is_active: true }],
      active_provider: 'p1',
    });
    if (target.includes('/api/backgrounds')) return Response.json({ backgrounds: [] });
    if (target.includes('/api/system/engine-status')) return Response.json({ state: 'ready' });
    return Response.json({});
  };
  globalThis.fetch = (...args) => globalThis.window.fetch(...args);

  const app = initApp({
    dom: { advRoot: root },
    renderSessionList() {},
  });

  t.after(() => {
    try { app.conversation.stop(); app.adv?.unmount(); } catch {}
    for (const toast of toasts.children) clearTimeout(toast._timer);
    delete globalThis.document;
    delete globalThis.window;
    delete globalThis.localStorage;
    delete globalThis.fetch;
    delete globalThis.location;
  });

  await app.ready;
  for (let i = 0; i < 5; i++) await tick();

  // 1. 验证 actions.listSessions() 中该会话的 excerpt 来自服务端的 lastLine，并且对应最后的 user 提问
  const sessionsList = app.actions.listSessions();
  const listed = sessionsList.find(s => s.id === session.id);
  assert.equal(listed.lastLine, '第二答');
  assert.equal(listed.excerpt.zh, '第二答', '多轮会话落后于服务端时，excerpt 应展示服务端的最新回复');
  assert.equal(listed.excerpt.user, '第二问', '多轮会话落后于服务端时，excerpt.user 应对应触发该回复的第二问');

  // 2. 首页 quote 也是服务端的最新回复
  const homeQuote = doc.querySelector('.home-quote')?.textContent || '';
  assert.ok(homeQuote.includes('第二答'), `首页台词应包含服务端回复「第二答」，实际为: "${homeQuote}"`);
});

test('startup sync preserves voiceKey on message with matching server ID when server has duplicate text', async t => {
  const doc = fakeDocument();
  globalThis.document = doc;
  doc.querySelectorAll = selector => doc.body.querySelectorAll(selector);
  const root = doc.createElement('div');
  root.id = 'advRoot';
  doc.body.appendChild(root);
  const toasts = doc.createElement('div');
  toasts.id = 'toastRoot';
  doc.body.appendChild(toasts);

  const session = {
    id: 's_voice_id_pair',
    title: '录音按ID配对',
    createdAt: 1_700_000_000_000,
    updatedAt: 1_700_000_100_000,
    settings: { ...DEFAULT_SESSION_SETTINGS, voiceProfileId: null },
    messages: [
      {
        id: 'm_10',
        serverMessageId: 10,
        role: 'user',
        content: '今天去哪',
        voiceKey: 'user_m_10',
        dur: 2.5,
        ts: 1_700_000_100_000,
      },
      {
        id: 'm_11',
        serverMessageId: 11,
        role: 'assistant',
        content: '去公园吧',
        ts: 1_700_000_150_000,
      },
    ],
  };
  const server = {
    id: session.id,
    title: session.title,
    voice_profile_id: null,
    created_at: '2024-06-01 00:00:00',
    updated_at: '2024-06-01 00:00:03',
    message_count: 4,
    last_message: '不是刚去过公园吗',
    settings: { ...session.settings },
  };

  const storage = new Map([['gal2voice.chat.v1', JSON.stringify({
    sessions: [session],
    activeId: session.id,
    global: { ...DEFAULT_GLOBAL },
  })]]);

  globalThis.localStorage = {
    getItem: key => storage.get(key) ?? null,
    setItem: (key, value) => storage.set(key, value),
    removeItem: key => storage.delete(key),
  };
  globalThis.location = { origin: 'http://127.0.0.1:8000' };
  globalThis.window = {
    innerWidth: 1280,
    location: { search: '', hash: '', origin: 'http://127.0.0.1:8000' },
    sessionStorage: { getItem: () => '', setItem() {}, removeItem() {} },
    prompt: () => null,
  };

  globalThis.window.fetch = async (url) => {
    const target = String(url);
    if (target.includes('/api/chat/sessions')) {
      return Response.json({ sessions: [server], has_more: false, count: 1 });
    }
    if (target.includes('/api/chat/history')) {
      return Response.json({
        session_id: session.id,
        has_more: false,
        oldest_id: 10,
        count: 4,
        messages: [
          { id: 10, role: 'user', content_chinese: '今天去哪', content_japanese: '', created_at: '2024-06-01 00:00:00' },
          { id: 11, role: 'assistant', content_chinese: '去公园吧', content_japanese: '', created_at: '2024-06-01 00:00:01' },
          { id: 12, role: 'user', content_chinese: '今天去哪', content_japanese: '', created_at: '2024-06-01 00:00:02' },
          { id: 13, role: 'assistant', content_chinese: '不是刚去过公园吗', content_japanese: '', created_at: '2024-06-01 00:00:03' },
        ],
      });
    }
    if (target.includes('/api/voice/profiles')) return Response.json({ profiles: [], active_profile_id: null });
    if (target.includes('/api/providers')) return Response.json({
      providers: [{ id: 'p1', name: '测试模型', api_key: 'sk-test', chat_model: 'gpt-4o', is_active: true }],
      active_provider: 'p1',
    });
    if (target.includes('/api/backgrounds')) return Response.json({ backgrounds: [] });
    if (target.includes('/api/system/engine-status')) return Response.json({ state: 'ready' });
    return Response.json({});
  };
  globalThis.fetch = (...args) => globalThis.window.fetch(...args);

  const app = initApp({
    dom: { advRoot: root },
    renderSessionList() {},
  });

  t.after(() => {
    try { app.conversation.stop(); app.adv?.unmount(); } catch {}
    for (const toast of toasts.children) clearTimeout(toast._timer);
    delete globalThis.document;
    delete globalThis.window;
    delete globalThis.localStorage;
    delete globalThis.fetch;
    delete globalThis.location;
  });

  await app.ready;
  for (let i = 0; i < 5; i++) await tick();

  const activeSession = storedActive();
  assert.equal(activeSession.messages.length, 4);
  const msg10 = activeSession.messages.find(m => m.id === 'm_10');
  const msg12 = activeSession.messages.find(m => m.id === 'm_12');
  assert.equal(msg10?.voiceKey, 'user_m_10', '服务端 ID 为 10 的消息应保留 voiceKey');
  assert.equal(msg10?.dur, 2.5);
  assert.equal(msg12?.voiceKey, undefined, '后续同文的服务端 ID 12 不应被错误配上 voiceKey');
});

test('startup sync retains unsent failed user message at the end of session history and keeps retry available', async t => {
  const doc = fakeDocument();
  globalThis.document = doc;
  doc.querySelectorAll = selector => doc.body.querySelectorAll(selector);
  const root = doc.createElement('div');
  root.id = 'advRoot';
  doc.body.appendChild(root);
  const toasts = doc.createElement('div');
  toasts.id = 'toastRoot';
  doc.body.appendChild(toasts);

  const session = {
    id: 's_failed_retained',
    title: '失败消息保留',
    createdAt: 1_700_000_000_000,
    updatedAt: 1_700_000_100_000,
    settings: { ...DEFAULT_SESSION_SETTINGS, voiceProfileId: null },
    messages: [
      { id: 'm_1', serverMessageId: 1, role: 'user', content: '第一问', ts: 1_700_000_050_000 },
      { id: 'm_2', serverMessageId: 2, role: 'assistant', content: '第一答', ts: 1_700_000_080_000 },
      {
        id: 'm_local_failed',
        role: 'user',
        content: '失败的问题',
        failed: { error: '网络连接失败', code: 'NETWORK_ERROR' },
        ts: 1_700_000_100_000,
      },
    ],
  };
  const server = {
    id: session.id,
    title: session.title,
    voice_profile_id: null,
    created_at: '2024-06-01 00:00:00',
    updated_at: '2024-06-01 00:00:03',
    message_count: 4,
    last_message: '第二答',
    settings: { ...session.settings },
  };

  const storage = new Map([['gal2voice.chat.v1', JSON.stringify({
    sessions: [session],
    activeId: session.id,
    global: { ...DEFAULT_GLOBAL },
  })]]);

  globalThis.localStorage = {
    getItem: key => storage.get(key) ?? null,
    setItem: (key, value) => storage.set(key, value),
    removeItem: key => storage.delete(key),
  };
  globalThis.location = { origin: 'http://127.0.0.1:8000' };
  globalThis.window = {
    innerWidth: 1280,
    location: { search: '', hash: '', origin: 'http://127.0.0.1:8000' },
    sessionStorage: { getItem: () => '', setItem() {}, removeItem() {} },
    prompt: () => null,
  };

  globalThis.window.fetch = async (url) => {
    const target = String(url);
    if (target.includes('/api/chat/sessions')) {
      return Response.json({ sessions: [server], has_more: false, count: 1 });
    }
    if (target.includes('/api/chat/history')) {
      return Response.json({
        session_id: session.id,
        has_more: false,
        oldest_id: 1,
        count: 4,
        messages: [
          { id: 1, role: 'user', content_chinese: '第一问', content_japanese: '', created_at: '2024-06-01 00:00:00' },
          { id: 2, role: 'assistant', content_chinese: '第一答', content_japanese: '', created_at: '2024-06-01 00:00:01' },
          { id: 3, role: 'user', content_chinese: '第二问', content_japanese: '', created_at: '2024-06-01 00:00:02' },
          { id: 4, role: 'assistant', content_chinese: '第二答', content_japanese: '', created_at: '2024-06-01 00:00:03' },
        ],
      });
    }
    if (target.includes('/api/voice/profiles')) return Response.json({ profiles: [], active_profile_id: null });
    if (target.includes('/api/providers')) return Response.json({
      providers: [{ id: 'p1', name: '测试模型', api_key: 'sk-test', chat_model: 'gpt-4o', is_active: true }],
      active_provider: 'p1',
    });
    if (target.includes('/api/backgrounds')) return Response.json({ backgrounds: [] });
    if (target.includes('/api/system/engine-status')) return Response.json({ state: 'ready' });
    return Response.json({});
  };
  globalThis.fetch = (...args) => globalThis.window.fetch(...args);

  const app = initApp({
    dom: { advRoot: root },
    renderSessionList() {},
  });

  t.after(() => {
    try { app.conversation.stop(); app.adv?.unmount(); } catch {}
    for (const toast of toasts.children) clearTimeout(toast._timer);
    delete globalThis.document;
    delete globalThis.window;
    delete globalThis.localStorage;
    delete globalThis.fetch;
    delete globalThis.location;
  });

  await app.ready;
  for (let i = 0; i < 5; i++) await tick();

  const activeSession = storedActive();
  // 1. 刷新后失败问题仍是最后一条、带 failed
  assert.equal(activeSession.messages.length, 5, '会话应包含服务端4条加本地末尾失败的1条');
  const lastMsg = activeSession.messages[4];
  assert.equal(lastMsg.id, 'm_local_failed');
  assert.equal(lastMsg.role, 'user');
  assert.equal(lastMsg.content, '失败的问题');
  assert.deepEqual(lastMsg.failed, { error: '网络连接失败', code: 'NETWORK_ERROR' });

  // 2. 存下来的状态里也在
  const storedJson = JSON.parse(storage.get('gal2voice.chat.v1'));
  const storedSessionData = storedJson.sessions.find(s => s.id === session.id);
  assert.equal(storedSessionData.messages.length, 5);
  assert.equal(storedSessionData.messages[4].id, 'm_local_failed');
  assert.deepEqual(storedSessionData.messages[4].failed, { error: '网络连接失败', code: 'NETWORK_ERROR' });

  // 3. 重试入口可用
  assert.equal(app.conversation.canRetry(lastMsg), true, '末尾失败问题的重试入口应可用');
});

test('startup sync pulls history and retains failed question at end when server adds message after retention', async t => {
  const doc = fakeDocument();
  globalThis.document = doc;
  doc.querySelectorAll = selector => doc.body.querySelectorAll(selector);
  const root = doc.createElement('div');
  root.id = 'advRoot';
  doc.body.appendChild(root);
  const toasts = doc.createElement('div');
  toasts.id = 'toastRoot';
  doc.body.appendChild(toasts);

  const session = {
    id: 's_failed_retained_more',
    title: '失败消息保留后服务端再多一条',
    createdAt: 1_700_000_000_000,
    updatedAt: 1_700_000_100_000,
    settings: { ...DEFAULT_SESSION_SETTINGS, voiceProfileId: null },
    messages: [
      { id: 'm_1', serverMessageId: 1, role: 'user', content: '第一问', ts: 1_700_000_050_000 },
      { id: 'm_2', serverMessageId: 2, role: 'assistant', content: '第一答', ts: 1_700_000_080_000 },
      { id: 'm_3', serverMessageId: 3, role: 'user', content: '第二问', ts: 1_700_000_085_000 },
      { id: 'm_4', serverMessageId: 4, role: 'assistant', content: '第二答', ts: 1_700_000_090_000 },
      {
        id: 'm_local_failed',
        role: 'user',
        content: '失败的问题',
        failed: { error: '网络连接失败', code: 'NETWORK_ERROR' },
        ts: 1_700_000_100_000,
      },
    ],
  };
  const server = {
    id: session.id,
    title: session.title,
    voice_profile_id: null,
    created_at: '2024-06-01 00:00:00',
    updated_at: '2024-06-01 00:00:04',
    message_count: 5,
    last_message: '第三问',
    settings: { ...session.settings },
  };

  const storage = new Map([['gal2voice.chat.v1', JSON.stringify({
    sessions: [session],
    activeId: session.id,
    global: { ...DEFAULT_GLOBAL },
  })]]);

  globalThis.localStorage = {
    getItem: key => storage.get(key) ?? null,
    setItem: (key, value) => storage.set(key, value),
    removeItem: key => storage.delete(key),
  };
  globalThis.location = { origin: 'http://127.0.0.1:8000' };
  globalThis.window = {
    innerWidth: 1280,
    location: { search: '', hash: '', origin: 'http://127.0.0.1:8000' },
    sessionStorage: { getItem: () => '', setItem() {}, removeItem() {} },
    prompt: () => null,
  };

  globalThis.window.fetch = async (url) => {
    const target = String(url);
    if (target.includes('/api/chat/sessions')) {
      return Response.json({ sessions: [server], has_more: false, count: 1 });
    }
    if (target.includes('/api/chat/history')) {
      return Response.json({
        session_id: session.id,
        has_more: false,
        oldest_id: 1,
        count: 5,
        messages: [
          { id: 1, role: 'user', content_chinese: '第一问', content_japanese: '', created_at: '2024-06-01 00:00:00' },
          { id: 2, role: 'assistant', content_chinese: '第一答', content_japanese: '', created_at: '2024-06-01 00:00:01' },
          { id: 3, role: 'user', content_chinese: '第二问', content_japanese: '', created_at: '2024-06-01 00:00:02' },
          { id: 4, role: 'assistant', content_chinese: '第二答', content_japanese: '', created_at: '2024-06-01 00:00:03' },
          { id: 5, role: 'user', content_chinese: '第三问', content_japanese: '', created_at: '2024-06-01 00:00:04' },
        ],
      });
    }
    if (target.includes('/api/voice/profiles')) return Response.json({ profiles: [], active_profile_id: null });
    if (target.includes('/api/providers')) return Response.json({
      providers: [{ id: 'p1', name: '测试模型', api_key: 'sk-test', chat_model: 'gpt-4o', is_active: true }],
      active_provider: 'p1',
    });
    if (target.includes('/api/backgrounds')) return Response.json({ backgrounds: [] });
    if (target.includes('/api/system/engine-status')) return Response.json({ state: 'ready' });
    return Response.json({});
  };
  globalThis.fetch = (...args) => globalThis.window.fetch(...args);

  const app = initApp({
    dom: { advRoot: root },
    renderSessionList() {},
  });

  t.after(() => {
    try { app.conversation.stop(); app.adv?.unmount(); } catch {}
    for (const toast of toasts.children) clearTimeout(toast._timer);
    delete globalThis.document;
    delete globalThis.window;
    delete globalThis.localStorage;
    delete globalThis.fetch;
    delete globalThis.location;
  });

  await app.ready;
  for (let i = 0; i < 5; i++) await tick();

  const activeSession = storedActive();
  // 1. 保留失败提问之后服务端再多 1 条，刷新仍会拉历史且失败提问仍在末尾
  assert.equal(activeSession.messages.length, 6, '会话应包含服务端5条加本地末尾失败的1条');
  const msg5 = activeSession.messages[4];
  assert.equal(msg5.serverMessageId, 5);
  assert.equal(msg5.content, '第三问');
  const lastMsg = activeSession.messages[5];
  assert.equal(lastMsg.id, 'm_local_failed');
  assert.equal(lastMsg.role, 'user');
  assert.equal(lastMsg.content, '失败的问题');
  assert.deepEqual(lastMsg.failed, { error: '网络连接失败', code: 'NETWORK_ERROR' });

  // 2. 存下来的状态里也在
  const storedJson = JSON.parse(storage.get('gal2voice.chat.v1'));
  const storedSessionData = storedJson.sessions.find(s => s.id === session.id);
  assert.equal(storedSessionData.messages.length, 6);
  assert.equal(storedSessionData.messages[5].id, 'm_local_failed');
  assert.deepEqual(storedSessionData.messages[5].failed, { error: '网络连接失败', code: 'NETWORK_ERROR' });

  // 3. 重试入口可用
  assert.equal(app.conversation.canRetry(lastMsg), true, '末尾失败问题的重试入口应可用');
});

test('startup sync discards interrupted partial assistant reply and retains complete server reply without duplication', async t => {
  const doc = fakeDocument();
  globalThis.document = doc;
  doc.querySelectorAll = selector => doc.body.querySelectorAll(selector);
  const root = doc.createElement('div');
  root.id = 'advRoot';
  doc.body.appendChild(root);
  const toasts = doc.createElement('div');
  toasts.id = 'toastRoot';
  doc.body.appendChild(toasts);

  const session = {
    id: 's_interrupted_assistant',
    title: '打断的半截回复',
    createdAt: 1_700_000_000_000,
    updatedAt: 1_700_000_100_000,
    settings: { ...DEFAULT_SESSION_SETTINGS, voiceProfileId: null },
    messages: [
      { id: 'm_1', serverMessageId: 1, role: 'user', content: '今天去哪', ts: 1_700_000_050_000 },
      { id: 'm_stream_interrupted', role: 'assistant', content: '去公园', ts: 1_700_000_080_000 },
    ],
  };
  const server = {
    id: session.id,
    title: session.title,
    voice_profile_id: null,
    created_at: '2024-06-01 00:00:00',
    updated_at: '2024-06-01 00:00:03',
    message_count: 4,
    last_message: '明天下午两点',
    settings: { ...session.settings },
  };

  const storage = new Map([['gal2voice.chat.v1', JSON.stringify({
    sessions: [session],
    activeId: session.id,
    global: { ...DEFAULT_GLOBAL },
  })]]);

  globalThis.localStorage = {
    getItem: key => storage.get(key) ?? null,
    setItem: (key, value) => storage.set(key, value),
    removeItem: key => storage.delete(key),
  };
  globalThis.location = { origin: 'http://127.0.0.1:8000' };
  globalThis.window = {
    innerWidth: 1280,
    location: { search: '', hash: '', origin: 'http://127.0.0.1:8000' },
    sessionStorage: { getItem: () => '', setItem() {}, removeItem() {} },
    prompt: () => null,
  };

  globalThis.window.fetch = async (url) => {
    const target = String(url);
    if (target.includes('/api/chat/sessions')) {
      return Response.json({ sessions: [server], has_more: false, count: 1 });
    }
    if (target.includes('/api/chat/history')) {
      return Response.json({
        session_id: session.id,
        has_more: false,
        oldest_id: 1,
        count: 4,
        messages: [
          { id: 1, role: 'user', content_chinese: '今天去哪', content_japanese: '', created_at: '2024-06-01 00:00:00' },
          { id: 2, role: 'assistant', content_chinese: '去公园散步吧，天气很好。', content_japanese: '', created_at: '2024-06-01 00:00:01' },
          { id: 3, role: 'user', content_chinese: '什么时候去', content_japanese: '', created_at: '2024-06-01 00:00:02' },
          { id: 4, role: 'assistant', content_chinese: '明天下午两点', content_japanese: '', created_at: '2024-06-01 00:00:03' },
        ],
      });
    }
    if (target.includes('/api/voice/profiles')) return Response.json({ profiles: [], active_profile_id: null });
    if (target.includes('/api/providers')) return Response.json({
      providers: [{ id: 'p1', name: '测试模型', api_key: 'sk-test', chat_model: 'gpt-4o', is_active: true }],
      active_provider: 'p1',
    });
    if (target.includes('/api/backgrounds')) return Response.json({ backgrounds: [] });
    if (target.includes('/api/system/engine-status')) return Response.json({ state: 'ready' });
    return Response.json({});
  };
  globalThis.fetch = (...args) => globalThis.window.fetch(...args);

  const app = initApp({
    dom: { advRoot: root },
    renderSessionList() {},
  });

  t.after(() => {
    try { app.conversation.stop(); app.adv?.unmount(); } catch {}
    for (const toast of toasts.children) clearTimeout(toast._timer);
    delete globalThis.document;
    delete globalThis.window;
    delete globalThis.localStorage;
    delete globalThis.fetch;
    delete globalThis.location;
  });

  await app.ready;
  for (let i = 0; i < 5; i++) await tick();

  const activeSession = storedActive();
  assert.equal(activeSession.messages.length, 4);
  assert.equal(activeSession.messages[1].content, '去公园散步吧，天气很好。');
  const interruptedExists = activeSession.messages.some(m => m.id === 'm_stream_interrupted' || m.content === '去公园');
  assert.equal(interruptedExists, false, '被打断的半截助手回复不应与服务端完整回复同时出现');
});




