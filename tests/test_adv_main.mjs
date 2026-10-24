import assert from 'node:assert/strict';
import test from 'node:test';
// Import before installing a document: don't auto-boot authentication or devices.
import { setup } from './helpers/adv_main.mjs';
import { tick } from './helpers/chat_stream.mjs';
import { state as storedState, getActive as storedActive, getSession as storedSession, DEFAULT_SESSION_SETTINGS } from '../frontend/src/store.js';
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

test('home preview success is silent; continuing the previewed session does not switch again', async t => {
  const h = await homeVoiceFixture(t, { status: 200 }); h.home.open(); await tick();
  const preview = h.app.actions.previewSession('home_2'); await tick(); await h.finish(); await preview;
  assert.equal(getActiveProfileId(), 2); assert.equal(h.toasts.children.length, 0);
  await h.home.refresh();
  h.home.element.querySelector('.home-continue').click(); await tick();
  assert.equal(h.stage.classList.contains('is-home'), false);
  assert.deepEqual(h.requests, [2]); assert.equal(h.toasts.children.length, 0);
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

test('home entry skips switching when voice is already active or session is unbound', async t => {
  const h = await homeVoiceFixture(t, { status: 200 }); h.home.open(); await tick();
  const pending = ensureSessionVoice(storedActive()); await tick(); await h.finish(); await pending;
  h.home.close(); h.app.actions.startNew(); await tick();
  assert.deepEqual(h.requests, [1]);
  storedActive().settings.voiceProfileId = null;
  await h.app.actions.loadSession(storedActive().id); await tick();
  assert.deepEqual(h.requests, [1]); assert.equal(h.toasts.children.length, 0);
});

test('home still reports a confirmed deleted voice binding and clears it', async t => {
  const h = await homeVoiceFixture(t, { status: 404 }); h.home.open(); await tick();
  await ensureSessionVoice(storedActive());
  assert.equal(storedActive().settings.voiceProfileId, null);
  assert.equal(h.toasts.children.length, 1); assert.match(h.toasts.children[0].dataset.msg, /音色已被删除，已解除失效绑定/);
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
  t.after(() => {
    home?.destroy(); app.conversation.stop(); app.adv.unmount();
    for (const toast of toasts.children) clearTimeout(toast._timer);
    delete globalThis.document; delete globalThis.location;
  });
  await app.ready; await tick();
  assert.deepEqual(switches, [1, 1]);
  for (const finish of pending) finish(); await tick();
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
