import assert from 'node:assert/strict';
import test from 'node:test';
// Import before installing a document: don't auto-boot authentication or devices.
import { setup } from './helpers/adv_main.mjs';
import { tick, fakeChatStream } from './helpers/chat_stream.mjs';
import { state as storedState, getActive as storedActive, getSession as storedSession, exportSessionData, DEFAULT_SESSION_SETTINGS, DEFAULT_GLOBAL, GREETING } from '../frontend/src/store.js';
import { mergeChatBackup } from '../frontend/src/chat_backup.js';
import { serverTimestamp } from '../frontend/src/session_data.js';
import { initVoiceSettings, ensureSessionVoice, getVoiceProfiles, fetchVoiceProfiles, getActiveProfileId, openSessionSettings } from '../frontend/src/controllers/voice_settings.js';
import { audioStore } from '../frontend/src/voice.js';
import { createTitleScreen } from '../frontend/src/adv/title.js';
import { initApp } from '../frontend/src/main.js';
import { getErrorMessage } from '../frontend/src/request.js';
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
  assert.equal(h.toasts.children.length, 1);
  assert.match(h.toasts.children[0].dataset.msg, /没有声音/);
  assert.doesNotMatch(h.toasts.children[0].dataset.msg, /重试|语音模型尚未加载/);
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
  assert.deepEqual(h.requests, [2]); assert.match(h.toasts.children[0].dataset.msg, status === 200 ? /已切换音色/ : /没有声音/);
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
  assert.equal(log.element.querySelector('.adv-log-failure')?.textContent, '未收到回复：模型这次没有返回内容。请点「重新发送」再试；仍失败请检查「对话模型」设置。');
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

for (const [label, profile, speed] of [
  ['高楯欧丽叶 without manifest speed', { id: 7, name: '高楯欧丽叶' }, 1],
  ['manifest speed on any name', { id: 7, name: '假角色', default_tts_speed: 1.05 }, 1.05],
  ['missing speed field', { id: 7, name: '假角色' }, 1],
  ['null speed field', { id: 7, name: '假角色', default_tts_speed: null }, 1],
]) {
  test(`home newWith reuses newChat and character default speed: ${label}`, async t => {
    storedState.sessions = [{ id: 'old_home', settings: { ...DEFAULT_SESSION_SETTINGS, voiceProfileId: 9, ttsSpeed: 1.8 }, messages: [] }];
    storedState.activeId = 'old_home';
    const h = setup(t, { callbacks: { state: storedState, getActive: storedActive, getSession: storedSession,
      getVoiceProfiles: () => [profile] } });
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
  // Fixed times keep 全部会话 in this order; times filled in at load can land a millisecond apart.
  const sessions = [{ id: 'startup_home', title: '假启动会话', updatedAt: 2000, settings: { ...DEFAULT_SESSION_SETTINGS, voiceProfileId: 1 },
    messages: [{ id: 'startup_msg', role: 'user', content: '你好' }] },
  { id: 'other_home', title: '另一个假会话', updatedAt: 1000, settings: { ...DEFAULT_SESSION_SETTINGS }, messages: [] }];
  let home, defer = true;
  const pending = [], switches = [];
  const { app, toasts } = await bootHomeVoiceApp(t, {
    sessions, activeId: 'startup_home', assignHome: created => { home = created; },
    beforeCleanup: async () => { defer = false; for (const finish of pending) finish(); await tick(); },
    fetchImpl: async (url, options = {}) => {
      if (url === '/api/voice/profiles') return Response.json({ profiles: [{ id: 1, name: '甲', work: 'a' }], active_profile_id: 99 });
      if (url === '/api/voice/switch') {
        switches.push(JSON.parse(options.body).profile_id);
        const failure = () => Response.json({ detail: ENGINE_NOT_LOADED }, { status: 502 });
        return defer ? new Promise(resolve => pending.push(() => resolve(failure()))) : failure();
      }
      return Response.json({ sessions: [], has_more: false, providers: [], backgrounds: [{ key: 'a/room', name: '房间', work: 'a' }] });
    },
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
  assert.equal(toasts.querySelector('.toast-action-btn').textContent, '检查运行状态');
});


test('a startup voice switch that fails before home opens stays quiet', async t => {
  // The switch starts before the session sync, and a stopped engine answers at once.
  const sessions = [{ id: 'early_home', title: '假启动会话', settings: { ...DEFAULT_SESSION_SETTINGS, voiceProfileId: 1 },
    messages: [{ id: 'early_msg', role: 'user', content: '你好' }] }];
  let home, releaseSync;
  const syncGate = new Promise(resolve => { releaseSync = resolve; });
  const switches = [];
  const { app, toasts } = await bootHomeVoiceApp(t, {
    sessions, activeId: 'early_home', assignHome: created => { home = created; },
    fetchImpl: async (url, options = {}) => {
      if (url === '/api/voice/profiles') return Response.json({ profiles: [{ id: 1, name: '甲', work: 'a' }], active_profile_id: 99 });
      if (url === '/api/voice/switch') {
        switches.push(JSON.parse(options.body).profile_id);
        return Response.json({ detail: ENGINE_NOT_LOADED }, { status: 502 });
      }
      if (String(url).startsWith('/api/chat/sessions')) await syncGate;
      return Response.json({ sessions: [], has_more: false, providers: [], backgrounds: [] });
    },
  });
  for (let i = 0; i < 5; i++) await tick();
  assert.deepEqual(switches, [1]); assert.equal(home, undefined);
  assert.equal(toasts.children.length, 0);
  releaseSync(); await app.ready; for (let i = 0; i < 5; i++) await tick();
  assert.deepEqual(switches, [1, 1]); assert.equal(home.isOpen, true);
  assert.equal(toasts.children.length, 0);
});

test('startup sync reloads session history when server has more messages than local copy (refresh keeps server reply)', async t => {
  const { session, doc, storage, app } = await bootActiveHistory(t, {
    id: 's_refresh', title: '晚上的对话', updatedAt: 1_700_000_100_000,
    messages: [{ id: 'm_local_user', role: 'user', content: '今天去哪', ts: 1_700_000_100_000 }],
    updated_at: '2024-06-01 00:00:02', message_count: 2, last_message: '去江边走走吧',
    historyMessages: [
      srv(10, 'user', '今天去哪'),
      srv(11, 'assistant', '去江边走走吧', '海辺へ', '2024-06-01 00:00:02'),
    ],
  });

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
  const activeSession = localChatSession({
    id: 's_active',
    title: '当前会话',
    updatedAt: 1_700_000_100_000,
    messages: [
      { id: 'm_1', role: 'user', content: '你好' },
      { id: 'm_2', role: 'assistant', content: '你好呀' },
    ],
  });
  const otherSession = localChatSession({
    id: 's_other',
    title: '另一个对话',
    updatedAt: 1_700_000_200_000,
    messages: [{ id: 'm_local_user2', role: 'user', content: '晚饭吃什么', ts: 1_700_000_200_000 }],
  });
  const serverSessions = [
    chatServer(activeSession, {
      updated_at: '2024-06-01 00:00:01',
      message_count: 2,
      last_message: '你好呀',
    }),
    chatServer(otherSession, {
      updated_at: '2024-06-01 00:00:03',
      message_count: 2,
      last_message: '吃拉面吧',
    }),
  ];
  const { storage, app } = await bootSyncedChat(t, {
    sessions: [activeSession, otherSession],
    activeId: activeSession.id,
    serverSessions,
    history(target) {
      if (!target.includes('session_id=s_other')) return undefined;
      return historyPayload(otherSession, {
        oldest_id: 20,
        count: 2,
        messages: [
          { id: 20, role: 'user', content_chinese: '晚饭吃什么', content_japanese: '', created_at: '2024-06-01 00:00:02' },
          { id: 21, role: 'assistant', content_chinese: '吃拉面吧', content_japanese: 'ラーメン', created_at: '2024-06-01 00:00:03' },
        ],
      });
    },
  });

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
  let historyShouldFail = true;
  const { session, storage, app } = await bootActiveHistory(t, {
    id: 's_fail_then_retry', title: '网络抖动的对话', updatedAt: 1_700_000_100_000,
    messages: [{ id: 'm_local_user', role: 'user', content: '今天去哪', ts: 1_700_000_100_000 }],
    updated_at: '2024-06-01 00:00:02', message_count: 2, last_message: '去江边走走吧',
    prepareHistory(built) {
      return () => {
        if (historyShouldFail) throw new Error('网络连接超时');
        return historyPayload(built, {
          oldest_id: 10, count: 2,
          messages: [
            srv(10, 'user', '今天去哪'),
            srv(11, 'assistant', '去江边走走吧', '海辺へ', '2024-06-01 00:00:02'),
          ],
        });
      };
    },
  });

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

test('startup sync correctly pairs voiceKey when earlier of duplicate messages had voice', async t => {
  const { session, storage } = await bootActiveHistory(t, {
    id: 's_voice_duplicate', title: '重复消息的对话', updatedAt: 1_700_000_200_000,
    messages: [
      localMsg('m_user_1', 'user', '今天去哪', { voiceKey: 'user_m_user_1', dur: 2.0, ts: 1_700_000_100_000 }),
      localMsg('m_assistant_1', 'assistant', '去公园吧', { ts: 1_700_000_150_000 }),
      localMsg('m_user_2', 'user', '今天去哪', { ts: 1_700_000_200_000 }),
    ],
    updated_at: '2024-06-01 00:00:03', message_count: 4, last_message: '不是刚去过公园吗',
    historyMessages: [
      srv(10, 'user', '今天去哪'),
      srv(11, 'assistant', '去公园吧', '', '2024-06-01 00:00:01'),
      srv(12, 'user', '今天去哪', '', '2024-06-01 00:00:02'),
      srv(13, 'assistant', '不是刚去过公园吗', '', '2024-06-01 00:00:03'),
    ],
  });

  const activeSession = storedActive();
  assert.equal(activeSession.messages.length, 4);
  // Turn 1 had voice, Turn 2 did not!
  assert.equal(activeSession.messages[0].voiceKey, 'user_m_user_1', '第一句（发语音的）应获得 voiceKey');
  assert.equal(activeSession.messages[0].dur, 2.0);
  assert.equal(activeSession.messages[2].voiceKey, undefined, '第二句（打字的）不应被错误配上 voiceKey');
  const storedMsgs = JSON.parse(storage.get('gal2voice.chat.v1')).sessions.find(s => s.id === session.id).messages;
  assert.equal(storedMsgs[0].voiceKey, 'user_m_user_1');
  assert.equal(storedMsgs[0].dur, 2.0);
  assert.equal(storedMsgs[2].voiceKey, undefined);
});

test('title screen excerpt presents server last_message when multi-turn session is behind server', async t => {
  const { session, doc, app } = await bootActiveHistory(t, {
    id: 's_multiturn_behind', title: '多轮会话', updatedAt: 1_700_000_200_000,
    messages: [
      { id: 'm_1', role: 'user', content: '第一问' },
      { id: 'm_2', role: 'assistant', content: '第一答' },
      { id: 'm_3', role: 'user', content: '第二问' },
    ],
    updated_at: '2024-06-01 00:00:03', message_count: 4, last_message: '第二答',
    history() { throw new Error('网络连接超时'); },
  });

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
  await bootActiveHistory(t, {
    id: 's_voice_id_pair', title: '录音按ID配对', updatedAt: 1_700_000_100_000,
    messages: [
      storedLine('m_10', 10, 'user', '今天去哪', { voiceKey: 'user_m_10', dur: 2.5, ts: 1_700_000_100_000 }),
      storedLine('m_11', 11, 'assistant', '去公园吧', { ts: 1_700_000_150_000 }),
    ],
    updated_at: '2024-06-01 00:00:03', message_count: 4, last_message: '不是刚去过公园吗',
    historyMessages: [
      srv(10, 'user', '今天去哪'),
      srv(11, 'assistant', '去公园吧', '', '2024-06-01 00:00:01'),
      srv(12, 'user', '今天去哪', '', '2024-06-01 00:00:02'),
      srv(13, 'assistant', '不是刚去过公园吗', '', '2024-06-01 00:00:03'),
    ],
  });

  const activeSession = storedActive();
  assert.equal(activeSession.messages.length, 4);
  const msg10 = activeSession.messages.find(m => m.id === 'm_10');
  const msg12 = activeSession.messages.find(m => m.id === 'm_12');
  assert.equal(msg10?.voiceKey, 'user_m_10', '服务端 ID 为 10 的消息应保留 voiceKey');
  assert.equal(msg10?.dur, 2.5);
  assert.equal(msg12?.voiceKey, undefined, '后续同文的服务端 ID 12 不应被错误配上 voiceKey');
});

test('startup sync pulls history and retains failed question at end when server adds message after retention', async t => {
  const { session, storage, app } = await bootActiveHistory(t, {
    id: 's_failed_retained_more', title: '失败消息保留后服务端再多一条', updatedAt: 1_700_000_100_000,
    messages: [
      { id: 'm_1', serverMessageId: 1, role: 'user', content: '第一问', ts: 1_700_000_050_000 },
      { id: 'm_2', serverMessageId: 2, role: 'assistant', content: '第一答', ts: 1_700_000_080_000 },
      { id: 'm_3', serverMessageId: 3, role: 'user', content: '第二问', ts: 1_700_000_085_000 },
      { id: 'm_4', serverMessageId: 4, role: 'assistant', content: '第二答', ts: 1_700_000_090_000 },
      localMsg('m_local_failed', 'user', '失败的问题', { failed: { error: '网络连接失败', code: 'NETWORK_ERROR' }, ts: 1_700_000_100_000 }),
    ],
    updated_at: '2024-06-01 00:00:04', message_count: 5, last_message: '第三问',
    historyMessages: qaHistory(['第一问', '第一答', '第二问', '第二答', '第三问']),
  });

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
  await bootActiveHistory(t, {
    id: 's_interrupted_assistant', title: '打断的半截回复', updatedAt: 1_700_000_100_000,
    messages: [
      { id: 'm_1', serverMessageId: 1, role: 'user', content: '今天去哪', ts: 1_700_000_050_000 },
      { id: 'm_stream_interrupted', role: 'assistant', content: '去公园', ts: 1_700_000_080_000 },
    ],
    updated_at: '2024-06-01 00:00:03', message_count: 4, last_message: '明天下午两点',
    historyMessages: qaHistory(['今天去哪', '去公园散步吧，天气很好。', '什么时候去', '明天下午两点']),
  });

  const activeSession = storedActive();
  assert.equal(activeSession.messages.length, 4);
  assert.equal(activeSession.messages[1].content, '去公园散步吧，天气很好。');
  const interruptedExists = activeSession.messages.some(m => m.id === 'm_stream_interrupted' || m.content === '去公园');
  assert.equal(interruptedExists, false, '被打断的半截助手回复不应与服务端完整回复同时出现');
});

test('startup sync keeps the cached failed question when it is retried while history is still loading', async t => {
  const { doc, root, toasts } = mountAdvDocument();

  const session = localChatSession({
    id: 's_retry_during_history',
    title: '历史未返回时重试',
    updatedAt: 1_700_000_100_000,
    settings: { ...DEFAULT_SESSION_SETTINGS, voiceProfileId: 1 },
    messages: [
      { id: 'm_1', serverMessageId: 1, role: 'user', content: '第一问', ts: 1_700_000_050_000 },
      { id: 'm_2', serverMessageId: 2, role: 'assistant', content: '第一答', ts: 1_700_000_080_000 },
      localMsg('m_local_failed', 'user', '失败的问题', { failed: { error: '网络连接失败', code: 'NETWORK_ERROR' }, ts: 1_700_000_100_000 }),
    ],
  });
  const server = chatServer(session, {
    voice_profile_id: 1,
    updated_at: '2024-06-01 00:00:03',
    message_count: 4,
    last_message: '第二答',
  });

  const storage = installChatStorage(storedSessionSnapshot(session));
  installAdvHost();

  let releaseHistory = () => {};
  const historyGate = new Promise(resolve => {
    releaseHistory = () => { releaseHistory = () => {}; resolve(); };
  });
  let markHistoryHung;
  const historyHung = new Promise(resolve => { markHistoryHung = resolve; });
  const streamRequests = [];

  globalThis.window.fetch = async (url, options = {}) => {
    const target = String(url);
    if (target.includes('/api/chat/sessions')) {
      return Response.json({ sessions: [server], has_more: false, count: 1 });
    }
    if (target.includes('/api/chat/history')) {
      markHistoryHung();
      await historyGate;
      return Response.json(historyPayload(session, {
        oldest_id: 1, count: 4, messages: qaHistory(['第一问', '第一答', '第二问', '第二答']),
      }));
    }
    if (target.includes('/api/chat/stream')) return openChatStream(target, options, streamRequests, { events: true });
    return startupApiFallback(target);
  };
  globalThis.fetch = (...args) => globalThis.window.fetch(...args);

  let app;
  t.after(async () => {
    releaseHistory();
    try { app?.conversation.stop(); app?.adv?.unmount(); } catch { /* already stopped */ }
    for (const toast of toasts.children) clearTimeout(toast._timer);
    delete globalThis.document;
    delete globalThis.window;
    delete globalThis.localStorage;
    delete globalThis.fetch;
    delete globalThis.location;
  });

  app = initApp({
    dom: { advRoot: root },
    renderSessionList() {},
  });

  await historyHung;
  await tick();

  const question = storedActive().messages.at(-1);
  assert.equal(question.id, 'm_local_failed');
  app.adv.action('log');
  const retryBtn = [...doc.querySelectorAll('.adv-error-btn')].find(button => button.textContent === '重新发送');
  assert.ok(retryBtn, '历史还在加载时，缓存里的失败问题应能从记录里重试');
  retryBtn.click();
  for (let i = 0; i < 5 && streamRequests.length === 0; i++) await tick();
  assert.equal(streamRequests.length, 1, '重试应向 /api/chat/stream 发出请求');
  assert.equal(streamRequests[0].prompt, '失败的问题');
  assert.equal(question.failed, undefined, '重试会清掉失败标记，但问题本身还在');
  assert.equal(storedActive().messages.at(-1), question);

  releaseHistory();
  await app.ready;
  for (let i = 0; i < 5; i++) await tick();

  const activeSession = storedActive();
  assert.equal(activeSession.messages.length, 3, '加载期间重试后，过期的历史不应替换本地消息');
  assert.equal(activeSession.messages.at(-1), question, '正在重试的问题应仍是原来那一条');
  assert.equal(activeSession.messages.some(m => m.content === '第二问' || m.content === '第二答'), false);
  const storedJson = JSON.parse(storage.get('gal2voice.chat.v1'));
  const storedSessionData = storedJson.sessions.find(s => s.id === session.id);
  assert.equal(storedSessionData.messages.length, 3);
  assert.equal(storedSessionData.messages.at(-1).id, 'm_local_failed');
  assert.equal(app.conversation.busy, true, '重试应仍在生成回复');
  await streamRequests[0].event('line', { index: 0, zh: '重试后的回复', ja: '', emotion: 'gentle', act: {} });
  assert.equal(app.adv.player.state, 'waiting_audio', '放行过期历史后，重试的台词应继续显示');
  assert.equal(storedActive().messages.at(-1), question);
});


function mountAdvDocument() {
  const doc = fakeDocument();
  globalThis.document = doc;
  doc.querySelectorAll = selector => doc.body.querySelectorAll(selector);
  const root = doc.createElement('div');
  root.id = 'advRoot';
  doc.body.appendChild(root);
  const toasts = doc.createElement('div');
  toasts.id = 'toastRoot';
  doc.body.appendChild(toasts);
  return { doc, root, toasts };
}

function installChatStorage(snapshot) {
  const storage = new Map([['gal2voice.chat.v1', JSON.stringify(snapshot)]]);
  globalThis.localStorage = {
    getItem: key => storage.get(key) ?? null,
    setItem: (key, value) => storage.set(key, value),
    removeItem: key => storage.delete(key),
  };
  return storage;
}

function installAdvHost() {
  globalThis.location = { origin: 'http://127.0.0.1:8000' };
  globalThis.window = {
    innerWidth: 1280,
    location: { search: '', hash: '', origin: 'http://127.0.0.1:8000' },
    sessionStorage: { getItem: () => '', setItem() {}, removeItem() {} },
    prompt: () => null,
  };
}

function assignAdvWindow(fetchImpl) {
  globalThis.window = {
    innerWidth: 1280,
    location: { search: '', hash: '', origin: 'http://127.0.0.1:8000' },
    sessionStorage: { getItem: () => '', setItem() {}, removeItem() {} },
    prompt: () => null,
    fetch: fetchImpl,
  };
  globalThis.fetch = (...args) => globalThis.window.fetch(...args);
}

function installAdvWindow(fetchImpl) {
  globalThis.location = { origin: 'http://127.0.0.1:8000' };
  assignAdvWindow(fetchImpl);
}

function startupApiFallback(target, { providers = 'demo', engineStatus = true } = {}) {
  if (target.includes('/api/voice/profiles')) return Response.json({ profiles: [], active_profile_id: null });
  if (target.includes('/api/providers')) {
    if (providers === 'demo') {
      return Response.json({
        providers: [{ id: 'p1', name: '测试模型', api_key: 'sk-test', chat_model: 'gpt-4o', is_active: true }],
        active_provider: 'p1',
      });
    }
    return Response.json({ providers: [], active_provider: null });
  }
  if (target.includes('/api/backgrounds')) return Response.json({ backgrounds: [] });
  if (engineStatus && target.includes('/api/system/engine-status')) return Response.json({ state: 'ready' });
  return Response.json({});
}

async function bootAdvApp(t, { root, toasts, onCleanup } = {}) {
  const app = initApp({ dom: { advRoot: root }, renderSessionList() {} });
  t.after(() => {
    try { app.conversation.stop(); app.adv?.unmount(); } catch {}
    for (const toast of toasts.children) clearTimeout(toast._timer);
    if (onCleanup) onCleanup();
    delete globalThis.document;
    delete globalThis.window;
    delete globalThis.localStorage;
    delete globalThis.fetch;
    delete globalThis.location;
  });
  await app.ready;
  for (let i = 0; i < 5; i++) await tick();
  return app;
}

function storedSessionSnapshot(session) {
  return { sessions: [session], activeId: session.id, global: { ...DEFAULT_GLOBAL } };
}

function localChatSession({
  id, title, createdAt = 1_700_000_000_000, updatedAt, settings, messages, ...extra
}) {
  return {
    id,
    title,
    createdAt,
    updatedAt,
    ...extra,
    settings: settings ?? { ...DEFAULT_SESSION_SETTINGS, voiceProfileId: null },
    messages,
  };
}

function chatServer(session, {
  voice_profile_id = null,
  created_at = '2024-06-01 00:00:00',
  updated_at,
  message_count,
  last_message,
} = {}) {
  return {
    id: session.id,
    title: session.title,
    voice_profile_id,
    created_at,
    updated_at,
    message_count,
    last_message,
    settings: { ...session.settings },
  };
}

function historyPayload(sessionOrId, { has_more = false, oldest_id, count, messages }) {
  return {
    session_id: typeof sessionOrId === 'string' ? sessionOrId : sessionOrId.id,
    has_more,
    oldest_id,
    count,
    messages,
  };
}

function openChatStream(target, options, streamRequests, { events = false } = {}) {
  let output;
  let started = false;
  const body = new ReadableStream({
    start(controller) { output = controller; started = true; },
    cancel() { try { output?.close(); } catch {} },
  });
  options.signal?.addEventListener('abort', () => {
    try { output?.close(); } catch {}
  }, { once: true });
  const request = { url: target, prompt: JSON.parse(options.body).prompt };
  if (events) {
    const encoder = new TextEncoder();
    request.event = async (type, payload) => {
      for (let i = 0; i < 10 && !started; i++) await tick();
      output.enqueue(encoder.encode(`event: ${type}\ndata: ${JSON.stringify(payload)}\n\n`));
      await tick();
    };
  }
  streamRequests.push(request);
  return new Response(body, { headers: { 'Content-Type': 'text/event-stream' } });
}

// Document, stored sessions, startup fetch, then initApp. mode "window" installs the fetch
// with location; the default matches installAdvHost plus window.fetch.
async function bootSyncedChat(t, {
  sessions,
  activeId,
  serverSessions,
  count,
  history,
  fallback,
  routes,
  mode = 'host',
  beforeBoot,
  onCleanup,
} = {}) {
  const { doc, root, toasts } = mountAdvDocument();
  const storage = installChatStorage({
    sessions,
    activeId,
    global: { ...DEFAULT_GLOBAL },
  });
  const fetchImpl = async (url, options = {}) => {
    const target = String(url);
    if (routes) {
      const handled = await routes(target, options);
      if (handled !== undefined) return handled;
    }
    if (target.includes('/api/chat/sessions')) {
      return Response.json({ sessions: serverSessions, has_more: false, count: count ?? serverSessions.length });
    }
    if (history && target.includes('/api/chat/history')) {
      const payload = typeof history === 'function' ? await history(target, options) : history;
      if (payload === undefined) return startupApiFallback(target, fallback);
      return payload instanceof Response ? payload : Response.json(payload);
    }
    return startupApiFallback(target, fallback);
  };
  if (mode === 'window') installAdvWindow(fetchImpl);
  else {
    installAdvHost();
    globalThis.window.fetch = fetchImpl;
    globalThis.fetch = (...args) => globalThis.window.fetch(...args);
  }
  if (beforeBoot) beforeBoot();
  const app = await bootAdvApp(t, { root, toasts, onCleanup });
  return { doc, root, toasts, storage, app };
}

// Real startup: localStorage, then initApp's default boot sync against the session list.
async function bootSessionSync(t, { session, server, postHandler = null }) {
  const { doc, root, toasts } = mountAdvDocument();
  const storage = installChatStorage(storedSessionSnapshot(session));
  const calls = [];
  installAdvWindow(async (url, options = {}) => {
      const target = String(url);
      calls.push({ url: target, method: options.method || 'GET', body: options.body });
      if (target.includes('/api/chat/sessions') && (options.method || 'GET') === 'POST') {
        if (postHandler) return postHandler(options);
        return Response.json({ status: 'ok' });
      }
      if (target.includes('/api/chat/sessions')) {
        return Response.json({ sessions: [server], has_more: false, count: 1 });
      }
      return startupApiFallback(target, { providers: 'empty', engineStatus: false });
    });
  await bootAdvApp(t, { root, toasts });
  const live = storedState.sessions.find(item => item.id === session.id);
  const stored = JSON.parse(storage.get('gal2voice.chat.v1')).sessions[0];
  const saves = calls.filter(call => call.method === 'POST' && call.url.includes('/api/chat/sessions'))
    .map(call => JSON.parse(call.body));
  return { live, stored, saves };
}

function srv(id, role, content_chinese, content_japanese = '', created_at = '2024-06-01 00:00:00') {
  return { id, role, content_chinese, content_japanese, created_at };
}

function qaHistory(lines) {
  return lines.map((content_chinese, idx) => srv(
    idx + 1, idx % 2 ? 'assistant' : 'user', content_chinese, '', `2024-06-01 00:00:0${idx}`,
  ));
}

function localMsg(id, role, content, extra) {
  return { id, role, content, ...extra };
}

function storedLine(id, serverMessageId, role, content, extra) {
  return { id, serverMessageId, role, content, ...extra };
}

function twoTurnSession({ id, title, updatedAt, lastLine, user, assistant }) {
  return localChatSession({
    id, title, updatedAt, ...(lastLine != null ? { lastLine } : {}),
    messages: [
      { id: user.id, serverMessageId: user.serverMessageId, role: 'user', content: user.content, ts: user.ts },
      { id: assistant.id, serverMessageId: assistant.serverMessageId, role: 'assistant', content: assistant.content, ts: assistant.ts },
    ],
  });
}

function fastapiDetail(type, field, msg, ctx) {
  const item = { type, loc: ['body', field], msg };
  if (ctx) item.ctx = ctx;
  return { detail: [item] };
}

function playerSettings(advPlayerName, advWindowOpacity, temperature) {
  return { ...DEFAULT_SESSION_SETTINGS, voiceProfileId: null, advPlayerName, advWindowOpacity, temperature };
}

function settingsSyncFixture({
  id, updatedAt, unconfirmedSettingsVersion, messages,
  localName, localOpacity, localTemp,
  serverUpdatedAt, serverName, serverOpacity, serverTemp,
}) {
  const session = {
    id, title: '晚上的对话', createdAt: 1_700_000_000_000, updatedAt,
    ...(unconfirmedSettingsVersion != null ? { unconfirmedSettingsVersion } : {}),
    settings: playerSettings(localName, localOpacity, localTemp),
    messages: messages ?? [{ id: 'm_keep', role: 'user', content: '只改了设置', ts: 1 }],
  };
  const server = {
    id: session.id, title: session.title, voice_profile_id: null,
    created_at: '2024-01-01 00:00:00', updated_at: serverUpdatedAt,
    message_count: session.messages.length, last_message: session.messages.at(-1).content,
    settings: playerSettings(serverName, serverOpacity, serverTemp),
  };
  return { session, server };
}

function emptySessionHistory(target) {
  const sid = new URL(String(target), 'http://127.0.0.1:8000').searchParams.get('session_id') || '';
  return historyPayload(sid, { oldest_id: null, count: 0, messages: [] });
}

function numberedWindow(startId, length, kind, assistantIf, overrides = {}) {
  return Array.from({ length }, (_, idx) => {
    const id = startId + idx;
    if (overrides[id]) return overrides[id];
    const isAssistant = assistantIf(id);
    const text = isAssistant ? `服务端回复 #${id}` : `历史提问 #${id}`;
    const role = isAssistant ? 'assistant' : 'user';
    if (kind === 'local') {
      return { id: `m_${id}`, serverMessageId: id, role, content: text, ts: 1_700_000_000_000 + idx * 1000 };
    }
    return { id, role, content_chinese: text, content_japanese: '', created_at: '2024-06-01 00:00:00' };
  });
}

// One local session, its server row, then the same startup boot as bootSyncedChat.
async function bootActiveHistory(t, spec) {
  const session = localChatSession({
    id: spec.id,
    title: spec.title,
    updatedAt: spec.updatedAt,
    ...spec.extra,
    messages: spec.messages,
  });
  const server = chatServer(session, {
    updated_at: spec.updated_at,
    message_count: spec.message_count,
    last_message: spec.last_message,
  });
  let history = spec.history;
  if (spec.prepareHistory) history = spec.prepareHistory(session);
  else if (spec.historyMessages) {
    history = historyPayload(session, {
      has_more: spec.has_more,
      oldest_id: spec.historyMessages[0].id,
      count: spec.historyMessages.length,
      messages: spec.historyMessages,
    });
  }
  const booted = await bootSyncedChat(t, {
    sessions: spec.extraSessions ? [session, ...spec.extraSessions] : [session],
    activeId: session.id,
    serverSessions: [server],
    mode: spec.mode,
    fallback: spec.fallback,
    routes: spec.routes,
    beforeBoot: spec.beforeBoot,
    onCleanup: spec.onCleanup,
    history,
  });
  return { session, server, ...booted };
}

async function bootHomeVoiceApp(t, { sessions, activeId, fetchImpl, assignHome, beforeCleanup }) {
  const { doc, root, toasts } = mountAdvDocument();
  storedState.sessions = sessions;
  storedState.activeId = activeId;
  const storage = new Map([['gal2voice.chat.v1', JSON.stringify(storedState)]]);
  globalThis.localStorage = { getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value) };
  globalThis.location = { origin: 'http://home.test' };
  globalThis.window = { innerWidth: 1280, location: { search: '', hash: '' },
    sessionStorage: { getItem: () => '', setItem() {} }, fetch: fetchImpl };
  globalThis.fetch = (...args) => window.fetch(...args);
  await fetchVoiceProfiles();
  let home;
  const app = initApp({ dom: { advRoot: root }, renderSessionList() {}, showTitle: actions => {
    home = createTitleScreen(app.adv.stage.overlay, { documentObj: doc, actions: {
      ...actions, getStatus: async () => ({ modelLoaded: true, hasModel: true, model: { state: 'ok' }, engine: { state: 'unavailable' } }),
    } });
    assignHome(home);
    home.open();
  } });
  t.after(async () => {
    if (beforeCleanup) await beforeCleanup();
    home?.destroy(); app.conversation.stop(); app.adv.unmount();
    for (const toast of toasts.children) clearTimeout(toast._timer);
    delete globalThis.document; delete globalThis.location;
  });
  return { doc, root, toasts, storage, app };
}

test('startup sync keeps newer local session settings and posts them', async t => {
  const { session, server } = settingsSyncFixture({
    id: 's_settings_local', updatedAt: 1_800_000_000_000, unconfirmedSettingsVersion: 1,
    localName: '小明', localOpacity: 40, localTemp: 0.2,
    serverUpdatedAt: '2024-01-01 00:00:01', serverName: '我', serverOpacity: 75, serverTemp: 0.7,
  });
  const { live, stored, saves } = await bootSessionSync(t, { session, server });
  assert.equal(live.settings.advPlayerName, '小明');
  assert.equal(live.settings.advWindowOpacity, 40);
  assert.equal(live.settings.temperature, 0.2);
  assert.equal(live.updatedAt, 1_800_000_000_000);
  assert.equal(live.unconfirmedSettingsVersion, undefined, '补发成功后内存中的未确认标记必须清除');
  assert.equal(stored.settings.advPlayerName, '小明');
  assert.equal(stored.settings.advWindowOpacity, 40);
  assert.equal(stored.settings.temperature, 0.2);
  assert.equal(stored.updatedAt, 1_800_000_000_000);
  assert.equal(stored.unconfirmedSettingsVersion, undefined, '补发成功后 localStorage 中的未确认标记必须清除');
  assert.equal(saves.length, 1);
  assert.equal(saves[0].id, session.id);
  assert.equal(saves[0].settings.advPlayerName, '小明');
  assert.equal(saves[0].settings.advWindowOpacity, 40);
  assert.equal(saves[0].settings.temperature, 0.2);
});

test('startup sync applies newer server session settings', async t => {
  const { session, server } = settingsSyncFixture({
    id: 's_settings_server', updatedAt: 1_700_000_000_000,
    localName: '我', localOpacity: 75, localTemp: 0.7,
    serverUpdatedAt: '2024-06-01 00:00:02', serverName: '小明', serverOpacity: 40, serverTemp: 0.2,
  });
  const { live, stored, saves } = await bootSessionSync(t, { session, server });
  const serverUpdatedAt = serverTimestamp(server.updated_at);
  assert.equal(live.settings.advPlayerName, '小明');
  assert.equal(live.settings.advWindowOpacity, 40);
  assert.equal(live.settings.temperature, 0.2);
  assert.equal(live.updatedAt, serverUpdatedAt);
  assert.equal(stored.settings.advPlayerName, '小明');
  assert.equal(stored.settings.advWindowOpacity, 40);
  assert.equal(stored.settings.temperature, 0.2);
  assert.equal(stored.updatedAt, serverUpdatedAt);
  assert.equal(saves.length, 0);
});

test('startup sync applies server session settings when local session has newer reply timestamp but no unconfirmed settings', async t => {
  const { session, server } = settingsSyncFixture({
    id: 's_reply_newer_than_server', updatedAt: 1_800_000_000_000,
    localName: '我', localOpacity: 75, localTemp: 0.7,
    serverUpdatedAt: '2024-01-01 00:00:01', serverName: '小明', serverOpacity: 40, serverTemp: 0.2,
    messages: [
      { id: 'm_user', role: 'user', content: '你好', ts: 1_700_000_000_000 },
      { id: 'm_assistant', role: 'assistant', content: '你好！', ts: 1_800_000_000_000 },
    ],
  });
  const { live, stored, saves } = await bootSessionSync(t, { session, server });
  const serverUpdatedAt = serverTimestamp(server.updated_at);
  assert.equal(live.settings.advPlayerName, '小明', '服务端新设置必须胜出');
  assert.equal(live.settings.advWindowOpacity, 40);
  assert.equal(live.settings.temperature, 0.2);
  assert.equal(live.updatedAt, serverUpdatedAt);
  assert.equal(stored.settings.advPlayerName, '小明', 'localStorage 必须更新为服务端新设置');
  assert.equal(stored.settings.advWindowOpacity, 40);
  assert.equal(stored.settings.temperature, 0.2);
  assert.equal(stored.updatedAt, serverUpdatedAt);
  assert.equal(saves.length, 0, '严禁把本机旧设置补发覆盖到服务端');
});

test('startup sync retains unconfirmed settings flag in storage when follow-up post fails', async t => {
  const { session, server } = settingsSyncFixture({
    id: 's_settings_fail', updatedAt: 1_800_000_000_000, unconfirmedSettingsVersion: 1,
    localName: '小明', localOpacity: 40, localTemp: 0.2,
    serverUpdatedAt: '2024-01-01 00:00:01', serverName: '我', serverOpacity: 75, serverTemp: 0.7,
  });
  const { live, stored, saves } = await bootSessionSync(t, {
    session,
    server,
    postHandler: () => Response.json({ message: '服务不可用' }, { status: 503 }),
  });
  assert.equal(live.settings.advPlayerName, '小明', '补发失败时仍保留本地设置');
  assert.equal(live.unconfirmedSettingsVersion, 1, '补发失败后内存中保留未确认标记');
  assert.equal(stored.settings.advPlayerName, '小明', '补发失败时 localStorage 仍保留本地设置');
  assert.equal(stored.unconfirmedSettingsVersion, 1, '补发失败后 localStorage 中保留未确认标记，以便下次启动重试');
  assert.equal(saves.length, 1);
});

test('startup sync unconfirmed version is preserved and newer settings kept if edited while in-flight boot post resolves', async t => {
  const { doc, root, toasts } = mountAdvDocument();

  const session = {
    id: 's_version_race',
    title: '对话',
    createdAt: 1_700_000_000_000,
    updatedAt: 1_800_000_000_000,
    unconfirmedSettingsVersion: 1,
    settings: {
      ...DEFAULT_SESSION_SETTINGS,
      voiceProfileId: 101,
      advPlayerName: '小明',
      temperature: 0.2,
    },
    messages: [{ id: 'm_1', role: 'user', content: '测试', ts: 1 }],
  };

  const server = {
    id: session.id,
    title: session.title,
    voice_profile_id: 101,
    created_at: '2024-01-01 00:00:00',
    updated_at: '2024-01-01 00:00:01',
    message_count: 1,
    last_message: '测试',
    settings: {
      ...DEFAULT_SESSION_SETTINGS,
      voiceProfileId: 101,
      advPlayerName: '我',
      temperature: 0.7,
    },
  };

  const storage = installChatStorage(storedSessionSnapshot(session));

  let resolveBootPost;
  const bootPostPromise = new Promise(resolve => { resolveBootPost = resolve; });
  let resolvePromptPut;
  const promptPutPromise = new Promise(resolve => { resolvePromptPut = resolve; });

  const calls = [];
  installAdvWindow(async (url, options = {}) => {
      const target = String(url);
      calls.push({ url: target, method: options.method || 'GET', body: options.body });
      if (target.includes('/api/chat/sessions') && (options.method || 'GET') === 'POST') {
        await bootPostPromise;
        return Response.json({ status: 'ok' });
      }
      if (target.includes('/api/chat/sessions')) {
        return Response.json({ sessions: [server], has_more: false, count: 1 });
      }
      if (target.includes('/api/characters/101/system-prompt') && (options.method || 'GET') === 'PUT') {
        await promptPutPromise;
        return Response.json({ status: 'ok', manifest_path: 'characters/alice/manifest.json' });
      }
      if (target.includes('/api/voice/profiles')) {
        return Response.json({
          profiles: [{ id: 101, name: 'Alice', system_prompt: 'old prompt' }],
          active_profile_id: 101,
        });
      }
      return startupApiFallback(target, { providers: 'empty', engineStatus: false });
    });

  const dom = {
    advRoot: root,
    sVoice: doc.createElement('select'),
    sSave: doc.createElement('button'),
    sSystem: doc.createElement('textarea'),
    sTemp: doc.createElement('input'),
    sTopP: doc.createElement('input'),
    sFreq: doc.createElement('input'),
    sPres: doc.createElement('input'),
    sCtx: doc.createElement('input'),
    sMaxTokens: doc.createElement('input'),
    sAiAdaptiveVoice: doc.createElement('select'),
    sTtsSpeed: doc.createElement('input'),
    sTtsTopK: doc.createElement('input'),
    sTtsTopP: doc.createElement('input'),
    sTtsTemp: doc.createElement('input'),
    sessionModal: doc.createElement('div'),
  };

  const app = initApp({ dom, renderSessionList() {} });
  let app2;
  t.after(() => {
    try { app.conversation.stop(); app.adv?.unmount(); } catch {}
    try { app2?.conversation.stop(); app2?.adv?.unmount(); } catch {}
    for (const toast of toasts.children) clearTimeout(toast._timer);
    delete globalThis.document;
    delete globalThis.window;
    delete globalThis.localStorage;
    delete globalThis.fetch;
    delete globalThis.location;
  });

  await app.ready;
  await tick();

  // 此时启动补发的 POST 还在挂起中；用户在设置里保存新参数并改动角色提示词
  await fetchVoiceProfiles();
  await openSessionSettings();
  dom.sVoice.value = '101';
  dom.sTemp.value = '0.9';
  dom.sSystem.value = 'new prompt text';
  dom.sSave.click();
  await tick();

  // handleSaveSessionSettings 正在 await saveCharacterPrompt
  // 此时旧的启动补发 POST 返回
  resolveBootPost();
  await tick();
  await tick();

  const live = storedState.sessions.find(item => item.id === session.id);
  const stored = JSON.parse(storage.get('gal2voice.chat.v1')).sessions[0];

  assert.equal(live.unconfirmedSettingsVersion, 2, '旧补发返回不得清除更新的未确认版本');
  assert.equal(stored.unconfirmedSettingsVersion, 2, 'localStorage 必须保留未确认版本 2');

  // 此时模拟刷新页面：从 localStorage 恢复会话并再次执行启动同步
  globalThis.window.fetch = async (url, options = {}) => {
    const target = String(url);
    if (target.includes('/api/chat/sessions') && (options.method || 'GET') === 'POST') {
      return Response.json({ status: 'ok' });
    }
    if (target.includes('/api/chat/sessions')) {
      return Response.json({ sessions: [server], has_more: false, count: 1 });
    }
    if (target.includes('/api/voice/profiles')) {
      return Response.json({
        profiles: [{ id: 101, name: 'Alice', system_prompt: 'old prompt' }],
        active_profile_id: 101,
      });
    }
    return startupApiFallback(target, { providers: 'empty', engineStatus: false });
  };
  app2 = initApp({ dom, renderSessionList() {} });
  await app2.ready;
  await tick();

  const refreshedLive = storedState.sessions.find(item => item.id === session.id);
  assert.equal(refreshedLive.settings.temperature, 0.9, '刷新后启动同步必须保留本机新保存的参数');

  // 释放挂起的人设请求
  resolvePromptPut();
  await tick();
});

test('ADV player input: textarea has maxLength 4000 and sending > 4000 chars triggers onBlocked toast while retaining draft', async t => {
  const toasts = [];
  const h = setup(t, { callbacks: { showToast: (msg, type) => toasts.push({ msg, type }) } });
  h.app.adv.textbox.enterInput({ canSend: true });
  const textarea = h.dom.advRoot.querySelector('.adv-player-textarea');
  assert.ok(textarea);
  assert.equal(textarea.maxLength, 4000);

  const longText = '长'.repeat(4005);
  textarea.value = longText;
  const sendBtn = h.dom.advRoot.querySelector('.adv-player-send-btn');
  sendBtn.click();
  await tick();

  assert.equal(h.sessions[0].messages.length, 0);
  assert.equal(textarea.value, longText);
  assert.ok(toasts.some(item => item.msg === '单次发送内容不能超过 4000 字，请删减后再发送。'));
});

test('getErrorMessage: FastAPI 422 prompt validation errors return specific character length / empty guidance', () => {
  const longError = fastapiDetail('string_too_long', 'prompt', 'String should have at most 4000 characters', { max_length: 4000 });
  assert.equal(getErrorMessage(longError, 'HTTP 422'), '单次发送内容不能超过 4000 字，请删减后再发送。');

  const shortError = fastapiDetail('string_too_short', 'prompt', 'String should have at least 1 characters', { min_length: 1 });
  assert.equal(getErrorMessage(shortError, 'HTTP 422'), '请输入要发送的对话内容。');

  const otherError = fastapiDetail('greater_than_equal', 'temperature', 'Input should be greater than or equal to 0');
  assert.equal(getErrorMessage(otherError, 'HTTP 422'), '配置不完整或数值超出范围，请检查填写内容后重新保存。');

  const systemPromptError = fastapiDetail('string_too_long', 'system_prompt', 'String should have at most 4000 characters', { max_length: 4000 });
  assert.equal(getErrorMessage(systemPromptError, 'HTTP 422'), '配置不完整或数值超出范围，请检查填写内容后重新保存。');
});

test('startup sync marks unanswered user prompt as failed when pruned by server and preserves retry and sync baseline', async t => {
  const remSession = localChatSession({
    id: 's_unpersisted_rem', title: '未落盘本地会话', updatedAt: 1_700_000_100_000,
    messages: [localMsg('m_rem_unanswered', 'user', '新会话未收到回复提问', { ts: 1_700_000_100_000 })],
  });
  const streamRequests = [];
  const { session, server, doc, root, storage, app } = await bootActiveHistory(t, {
    id: 's_unanswered_pruned', title: '未获回复会话', updatedAt: 1_700_000_100_000,
    messages: [
      { id: 'm_1', serverMessageId: 1, role: 'user', content: '第一问', ts: 1_700_000_050_000 },
      { id: 'm_2', serverMessageId: 2, role: 'assistant', content: '第一答', ts: 1_700_000_080_000 },
      localMsg('m_unanswered', 'user', '首字返回前刷新的提问', { ts: 1_700_000_100_000 }),
    ],
    updated_at: '2024-06-01 00:00:02',
    message_count: 2, // 服务端物理清理了孤立提问，故 message_count 小于本地 messages.length (3)
    last_message: '第一答',
    extraSessions: [remSession],
    routes(target, options) {
      if (target.includes('/api/chat/stream')) return openChatStream(target, options, streamRequests, { events: true });
    },
  });
  let app2;
  t.after(() => {
    try { app2?.conversation.stop(); app2?.adv?.unmount(); } catch {}
  });

  // 1. 断言已在服务端落盘会话中，因 message_count < local.messages.length 被自动补标 failed
  const activeSession = storedActive();
  const lastMsg = activeSession.messages.at(-1);
  assert.equal(lastMsg.id, 'm_unanswered');
  assert.deepEqual(lastMsg.failed, { error: '对话已中断，未收到回复', code: 'cancelled' });
  assert.equal(app.conversation.canRetry(lastMsg), true, '补标 failed 后 canRetry 必须为 true');

  // 2. 断言未在服务端落盘的残留本地会话 remSession，末尾提问也被自动补标 failed
  const remInState = storedState.sessions.find(s => s.id === remSession.id);
  assert.ok(remInState, '残留本地会话必须被保留在会话列表中');
  const lastRem = remInState.messages.at(-1);
  assert.deepEqual(lastRem.failed, { error: '对话已中断，未收到回复', code: 'cancelled' });
  storedState.activeId = remSession.id; assert.equal(app.conversation.canRetry(lastRem), true); storedState.activeId = session.id;

  // 3. 断言 localStorage 已同步持久化补标的 failed
  const storedJson = JSON.parse(storage.get('gal2voice.chat.v1'));
  const storedSession = storedJson.sessions.find(s => s.id === session.id);
  assert.deepEqual(storedSession.messages.at(-1).failed, { error: '对话已中断，未收到回复', code: 'cancelled' });
  const storedRem = storedJson.sessions.find(s => s.id === remSession.id);
  assert.deepEqual(storedRem.messages.at(-1).failed, { error: '对话已中断，未收到回复', code: 'cancelled' });

  // 4. 打开 LOG 面板，断言渲染出错误信息和「重新发送」按钮，且点击后触发重试
  app.adv.action('log');
  const log = app.adv.backlog;
  assert.ok(log, 'LOG 面板应已打开');
  const failureEl = log.element.querySelector('.adv-log-failure');
  assert.ok(failureEl, 'LOG 面板应展示未收到回复提示');
  assert.match(failureEl.textContent, /未收到回复：对话已中断，未收到回复/);
  const retryBtn = [...doc.querySelectorAll('.adv-error-btn')].find(b => b.textContent === '重新发送');
  assert.ok(retryBtn, 'LOG 面板中必须渲染「重新发送」按钮');
  retryBtn.click();
  for (let i = 0; i < 5 && streamRequests.length === 0; i++) await tick();
  assert.equal(streamRequests.length, 1, '点击「重新发送」必须向 /api/chat/stream 发送重发请求');
  assert.equal(streamRequests[0].prompt, '首字返回前刷新的提问');
  app.conversation.stop();

  // 5. 验证服务端多出新消息（例如 message_count 变为 3，比已上云消息条数 2 多 1 条）时：
  // 此时本地有 2 条已上云消息 + 1 条失败提问。
  // 若未修，localCount 会被算为 3，3 > 3 为 false，从而跳过历史拉取；
  // 修复后，因已补标 failed，localCount 正确算为 2，3 > 2 触发 loadSessionHistory，
  // 且 loadSessionHistory 会将该失败提问保留在末尾。
  let historyFetched = false;
  server.message_count = 3;
  server.last_message = '其他页面新增的第三问';
  globalThis.window.fetch = async (url) => {
    const target = String(url);
    if (target.includes('/api/chat/sessions')) {
      return Response.json({ sessions: [server], has_more: false, count: 1 });
    }
    if (target.includes('/api/chat/history')) {
      historyFetched = true;
      return Response.json(historyPayload(session, {
        oldest_id: 1,
        count: 3,
        messages: qaHistory(['第一问', '第一答', '其他页面新增的第三问']),
      }));
    }
    return startupApiFallback(target);
  };

  app2 = initApp({
    dom: { advRoot: root },
    renderSessionList() {},
  });
  await app2.ready;
  for (let i = 0; i < 5; i++) await tick();

  assert.equal(historyFetched, true, '服务端比已上云消息多出新消息时必须触发 loadSessionHistory');
  const finalSession = storedActive();
  assert.equal(finalSession.messages.length, 4, '应包含服务端 3 条历史 + 本地末尾保留的 1 条失败提问');
  assert.equal(finalSession.messages[2].content, '其他页面新增的第三问');
  const finalLastMsg = finalSession.messages[3];
  assert.equal(finalLastMsg.id, 'm_unanswered');
  assert.deepEqual(finalLastMsg.failed, { error: '对话已中断，未收到回复', code: 'cancelled' });
  assert.equal(app2.conversation.canRetry(finalLastMsg), true, '历史更新后末尾失败提问仍可重试');
});

test('startup sync does not mark user prompt failed when server count covers local messages', async t => {
  // server.message_count >= local.messages.length (1 >= 1)
  await bootActiveHistory(t, {
    id: 's_equal_count', title: '已落盘提问', updatedAt: 1_700_000_100_000,
    messages: [{ id: 'm_1', role: 'user', content: '提问', ts: 1_700_000_050_000 }],
    updated_at: '2024-06-01 00:00:02', message_count: 1, last_message: '提问',
    mode: 'window', fallback: { providers: 'empty' },
  });

  const activeSession = storedActive();
  const lastMsg = activeSession.messages.at(-1);
  assert.equal(lastMsg.failed, undefined, '服务端消息数不小于本地时不得误标 failed');
});

test('unbound session syncs active voice profile on turnstart and updates stage without cancelling the live turn', async t => {
  const { doc, root, toasts } = mountAdvDocument();

  const session = {
    id: 's_unbound_follow',
    title: '跟随当前音色',
    createdAt: 1_700_000_000_000,
    updatedAt: 1_700_000_100_000,
    settings: { ...DEFAULT_SESSION_SETTINGS, voiceProfileId: null, advTextIntervalMs: 0 },
    messages: [],
  };
  const server = {
    id: session.id,
    title: session.title,
    voice_profile_id: null,
    created_at: '2024-06-01 00:00:00',
    updated_at: '2024-06-01 00:00:00',
    message_count: 0,
    last_message: '',
    settings: { ...session.settings },
  };
  const portrait = id => ({
    enabled: true,
    defaultCostume: `outfit_${id}`,
    costumes: [{ id: `outfit_${id}` }],
    sprites: { [`outfit_${id}`]: { smile: { file: `/art_${id}.png`, width: 100, height: 200 } } },
    faces: { smile: { primary: 'gentle' } },
    expressionSets: { gentle: [{ id: 'smile' }] },
  });

  const storage = installChatStorage(storedSessionSnapshot(session));
  globalThis.location = { origin: 'http://127.0.0.1:8000' };
  let activeProfile = 1;
  const portraitUrls = [];
  const stream = fakeChatStream();
  assignAdvWindow(async (url) => {
      const target = String(url);
      if (target.includes('/api/chat/sessions')) {
        return Response.json({ sessions: [server], has_more: false, count: 1 });
      }
      if (target.includes('/api/voice/profiles')) {
        return Response.json({
          profiles: [{ id: 1, name: '角色A', work: 'a' }, { id: 2, name: '角色B', work: 'b' }],
          active_profile_id: activeProfile,
        });
      }
      const portraitMatch = /\/api\/characters\/(\d+)\/portrait/.exec(target);
      if (portraitMatch) {
        portraitUrls.push(`/api/characters/${portraitMatch[1]}/portrait`);
        return Response.json(portrait(portraitMatch[1]));
      }
      return startupApiFallback(target, { providers: 'empty' });
    });

  await fetchVoiceProfiles();
  const app = initApp({
    dom: { advRoot: root },
    renderSessionList() {},
    showTitle() {},
    fetch: stream.fetch,
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

  assert.equal(getActiveProfileId(), 1);
  assert.ok(portraitUrls.includes('/api/characters/1/portrait'), '初始舞台应请求角色 1 的立绘');

  // 模拟在 Telegram 切换后端当前角色为 2（角色 B）
  activeProfile = 2;
  app.sendMessage('你好');
  for (let i = 0; i < 5; i++) await tick();

  assert.equal(app.conversation.busy, true, '同步后端当前音色不得取消正在进行的对话回合');
  const req = stream.requests[0];
  assert.ok(req, '应已发起流式对话请求');
  await req.event('line', { index: 0, zh: '我是角色B。', ja: '私はBです。', emotion: 'gentle', act: {} });
  await req.event('line_audio', { line: 0, status: 'skipped', chunks: [] });
  await req.onEnd('我是角色B。');
  for (let i = 0; i < 5; i++) await tick();

  assert.equal(app.conversation.busy, false);
  const lastMsg = storedActive().messages.at(-1);
  assert.equal(lastMsg.role, 'assistant');
  assert.equal(lastMsg.content, '我是角色B。');
  assert.equal(getActiveProfileId(), 2);
  assert.equal(app.adv.stage.textbox.querySelector('.adv-name-text')?.textContent, '角色B');
  assert.ok(portraitUrls.includes('/api/characters/2/portrait'), '舞台应请求角色 2 的立绘');
});

test('chat stream request omits fixed tts sliders in adaptive voice mode and transmits them in fixed mode', async (t) => {
  const h = setup(t);
  const session = h.getActive();
  assert.equal(session.settings.aiAdaptiveVoice, true);
  h.app.sendMessage('你好！');
  await tick();
  assert.equal(h.stream.requests.length, 1);
  const autoReqBody = JSON.parse(h.stream.requests[0].options.body);
  assert.equal(autoReqBody.ai_adaptive_voice, true);
  assert.deepEqual(autoReqBody.tts_options, { ai_adaptive_voice: true });

  // Switch to fixed mode and set custom sliders
  session.settings.aiAdaptiveVoice = false;
  session.settings.ttsSpeed = 1.25;
  session.settings.ttsTopK = 20;
  session.settings.ttsTopP = 0.85;
  session.settings.ttsTemperature = 0.95;

  await h.stream.requests[0].onEnd('你好！');
  h.app.sendMessage('第二句');
  await tick();
  assert.equal(h.stream.requests.length, 2);
  const fixedReqBody = JSON.parse(h.stream.requests[1].options.body);
  assert.equal(fixedReqBody.ai_adaptive_voice, false);
  assert.deepEqual(fixedReqBody.tts_options, {
    speed: 1.25,
    top_k: 20,
    top_p: 0.85,
    temperature: 0.95,
    ai_adaptive_voice: false,
  });

  // Switch back to adaptive voice mode and verify sliders are omitted again
  session.settings.aiAdaptiveVoice = true;
  await h.stream.requests[1].onEnd('第二句');
  h.app.sendMessage('第三句');
  await tick();
  assert.equal(h.stream.requests.length, 3);
  const backReqBody = JSON.parse(h.stream.requests[2].options.body);
  assert.equal(backReqBody.ai_adaptive_voice, true);
  assert.deepEqual(backReqBody.tts_options, { ai_adaptive_voice: true });
});

test('startup sync clears cached messages for both active and inactive sessions when server message_count is 0', async t => {
  const tg1001 = twoTurnSession({
    id: 'tg_1001', title: 'Telegram 会话 1', updatedAt: 1_700_000_200_000, lastLine: '旧回复 1',
    user: { id: 'm_1', serverMessageId: 1, content: '旧提问 1', ts: 1_700_000_100_000 },
    assistant: { id: 'm_2', serverMessageId: 2, content: '旧回复 1', ts: 1_700_000_200_000 },
  });
  const tg1002 = twoTurnSession({
    id: 'tg_1002', title: 'Telegram 会话 2', updatedAt: 1_700_000_150_000, lastLine: '旧回复 2',
    user: { id: 'm_3', serverMessageId: 3, content: '旧提问 2', ts: 1_700_000_120_000 },
    assistant: { id: 'm_4', serverMessageId: 4, content: '旧回复 2', ts: 1_700_000_150_000 },
  });
  const serverSessions = [
    chatServer(tg1001, { updated_at: '2024-06-01 00:00:10', message_count: 0, last_message: '' }),
    chatServer(tg1002, { updated_at: '2024-06-01 00:00:09', message_count: 0, last_message: '' }),
  ];
  const { doc, storage, app } = await bootSyncedChat(t, {
    sessions: [tg1001, tg1002],
    activeId: 'tg_1001',
    serverSessions,
    mode: 'window',
    fallback: { providers: 'empty' },
    history: emptySessionHistory,
  });

  assert.deepEqual(storedSession('tg_1001').messages, [], '激活会话 tg_1001 内存中的旧消息应已清空');
  assert.equal(storedSession('tg_1001').lastLine, '');
  assert.deepEqual(storedSession('tg_1002').messages, [], '非激活会话 tg_1002 内存中的旧消息应已清空');
  assert.equal(storedSession('tg_1002').lastLine, '');

  const storedJson = JSON.parse(storage.get('gal2voice.chat.v1'));
  const stored1001 = storedJson.sessions.find(s => s.id === 'tg_1001');
  const stored1002 = storedJson.sessions.find(s => s.id === 'tg_1002');
  assert.deepEqual(stored1001.messages, [], 'localStorage 中 tg_1001 的旧消息应已清空');
  assert.deepEqual(stored1002.messages, [], 'localStorage 中 tg_1002 的旧消息应已清空');

  app.adv.action('log');
  for (let i = 0; i < 3; i++) await tick();
  assert.equal(doc.querySelectorAll('.adv-log-primary').length, 0, 'LOG 回看中不应残留已清空的旧消息');

  const mergedBackup = mergeChatBackup({
    format: 'galgame2voice.chat',
    version: 1,
    sessions: [
      { id: 'tg_1001', title: 'Telegram 会话 1', messages: [], settings: { ...DEFAULT_SESSION_SETTINGS } },
      { id: 'tg_1002', title: 'Telegram 会话 2', messages: [], settings: { ...DEFAULT_SESSION_SETTINGS } },
    ],
  }, storedJson);
  assert.equal(mergedBackup.browserCopies, 0, '完整聊天备份不应为已清空会话派生浏览器副本');
});

test('switching to a session cleared on server empties cached messages via loadSessionHistory', async t => {
  const activeSession = twoTurnSession({
    id: 's_active_keep', title: '当前对话', updatedAt: 1_700_000_300_000,
    user: { id: 'm_1', serverMessageId: 1, content: '保留问', ts: 1_700_000_100_000 },
    assistant: { id: 'm_2', serverMessageId: 2, content: '保留答', ts: 1_700_000_200_000 },
  });
  const tgLess = twoTurnSession({
    id: 'tg_less_cleared', title: '服务端少于本地后清空', updatedAt: 1_700_000_200_000, lastLine: '旧答 A',
    user: { id: 'm_10', serverMessageId: 10, content: '旧问 A', ts: 1_700_000_100_000 },
    assistant: { id: 'm_11', serverMessageId: 11, content: '旧答 A', ts: 1_700_000_200_000 },
  });
  const tgMore = twoTurnSession({
    id: 'tg_more_cleared', title: '运行期清空对话', updatedAt: 1_700_000_150_000, lastLine: '旧答 B',
    user: { id: 'm_20', serverMessageId: 20, content: '旧问 B', ts: 1_700_000_100_000 },
    assistant: { id: 'm_21', serverMessageId: 21, content: '旧答 B', ts: 1_700_000_150_000 },
  });
  const serverSessions = [
    chatServer(activeSession, { updated_at: '2024-06-01 00:00:10', message_count: 2, last_message: '保留答' }),
    chatServer(tgLess, { updated_at: '2024-06-01 00:00:08', message_count: 1, last_message: '过渡消息' }),
    chatServer(tgMore, { updated_at: '2024-06-01 00:00:07', message_count: 3, last_message: '过渡消息' }),
  ];
  const { storage, app } = await bootSyncedChat(t, {
    sessions: [activeSession, tgLess, tgMore],
    activeId: activeSession.id,
    serverSessions,
    mode: 'window',
    fallback: { providers: 'empty' },
    // 会话已在运行期被清空，返回空历史列表
    history: emptySessionHistory,
  });

  await app.switchSession('tg_more_cleared');
  for (let i = 0; i < 5; i++) await tick();
  assert.deepEqual(storedSession('tg_more_cleared').messages, [], '切换到运行期已清空的会话后 messages 应被清空');
  assert.equal(storedSession('tg_more_cleared').lastLine, '');

  await app.switchSession('tg_less_cleared');
  for (let i = 0; i < 5; i++) await tick();
  assert.deepEqual(storedSession('tg_less_cleared').messages, [], '服务端条数少于本地并在切换前清空的会话在切换后 messages 应被清空');
  assert.equal(storedSession('tg_less_cleared').lastLine, '');

  const storedJson = JSON.parse(storage.get('gal2voice.chat.v1'));
  assert.deepEqual(storedJson.sessions.find(s => s.id === 'tg_more_cleared').messages, []);
  assert.deepEqual(storedJson.sessions.find(s => s.id === 'tg_less_cleared').messages, []);
});

test('startup sync with server message_count 0 preserves local starter GREETING and unanswered user prompt for retry', async t => {
  const starterSession = localChatSession({
    id: 's_local_greeting',
    title: '新对话',
    updatedAt: 1_700_000_100_000,
    messages: [
      { id: 'm_starter_greet', role: 'assistant', content: GREETING, noVoice: true, ts: 1_700_000_050_000 },
    ],
  });
  const unansweredOnlySession = localChatSession({
    id: 's_unanswered_zero',
    title: '未收到回复的提问',
    updatedAt: 1_700_000_300_000,
    messages: [
      { id: 'm_local_pending', role: 'user', content: '尚未收到回复的提问', ts: 1_700_000_300_000 },
    ],
  });
  const clearedWithTailPending = localChatSession({
    id: 's_cleared_with_pending',
    title: '清空旧历史但保留末尾未发提问',
    updatedAt: 1_700_000_250_000,
    messages: [
      { id: 'm_old_1', serverMessageId: 1, role: 'user', content: '已删旧问', ts: 1_700_000_100_000 },
      { id: 'm_old_2', serverMessageId: 2, role: 'assistant', content: '已删旧答', ts: 1_700_000_150_000 },
      { id: 'm_tail_pending', role: 'user', content: '末尾待重发提问', ts: 1_700_000_250_000 },
    ],
  });
  const serverSessions = [unansweredOnlySession, clearedWithTailPending, starterSession].map((s, idx) => chatServer(s, {
    updated_at: `2024-06-01 00:00:0${9 - idx}`,
    message_count: 0,
    last_message: '',
  }));
  const streamRequests = [];
  const { doc, app } = await bootSyncedChat(t, {
    sessions: [unansweredOnlySession, clearedWithTailPending, starterSession],
    activeId: unansweredOnlySession.id,
    serverSessions,
    mode: 'window',
    history: emptySessionHistory,
    routes(target, options) {
      if (target.includes('/api/chat/stream')) return openChatStream(target, options, streamRequests);
    },
  });

  // (a) 仅含初始本地 GREETING 的新会话欢迎语不被误删
  const liveStarter = storedSession('s_local_greeting');
  assert.equal(liveStarter.messages.length, 1, '新会话初始本地 GREETING 不得被误删');
  assert.equal(liveStarter.messages[0].content, GREETING);
  assert.equal(liveStarter.messages[0].noVoice, true);

  // (b1) 服务端 message_count: 0 但本地有一条未收到回复的用户提问时，补标 failed 并保留供重试
  const liveUnanswered = storedSession('s_unanswered_zero');
  assert.equal(liveUnanswered.messages.length, 1, '未收到回复的用户提问应保留在会话中');
  const pendingMsg = liveUnanswered.messages[0];
  assert.equal(pendingMsg.id, 'm_local_pending');
  assert.deepEqual(pendingMsg.failed, { error: '对话已中断，未收到回复', code: 'cancelled' });
  assert.equal(app.conversation.canRetry(pendingMsg), true);

  // 打开 LOG 点击「重新发送」可正常触发重发
  app.adv.action('log');
  const retryBtn = [...doc.querySelectorAll('.adv-error-btn')].find(b => b.textContent === '重新发送');
  assert.ok(retryBtn, 'LOG 中应渲染「重新发送」按钮');
  retryBtn.click();
  for (let i = 0; i < 5 && streamRequests.length === 0; i++) await tick();
  assert.equal(streamRequests.length, 1);
  assert.equal(streamRequests[0].prompt, '尚未收到回复的提问');
  app.conversation.stop();

  // (b2) 曾有已删旧历史 + 末尾未收到回复提问时，旧历史被清理而末尾失败提问保留
  const liveClearedTail = storedSession('s_cleared_with_pending');
  assert.equal(liveClearedTail.messages.length, 1, '已删旧消息应被清理，仅保留末尾未上云失败提问');
  assert.equal(liveClearedTail.messages[0].id, 'm_tail_pending');
  assert.deepEqual(liveClearedTail.messages[0].failed, { error: '对话已中断，未收到回复', code: 'cancelled' });

  // 切换到仅含欢迎语的新会话后，欢迎语同样保留
  await app.switchSession('s_local_greeting');
  for (let i = 0; i < 5; i++) await tick();
  assert.equal(storedSession('s_local_greeting').messages.length, 1);
  assert.equal(storedSession('s_local_greeting').messages[0].content, GREETING);
});

for (const totalServerMessages of [150, 101]) {
  test(`startup sync preserves unanswered local prompt and enables retry when server has ${totalServerMessages} messages (>100)`, async t => {
    const oldestWindowId = totalServerMessages - 100 + 1;
    const assistantIf = id => (totalServerMessages - id) % 2 === 0;
    const serverWindow = numberedWindow(oldestWindowId, 100, 'server', assistantIf);
    const unansweredId = `m_local_unanswered_${totalServerMessages}`;
    const unansweredPrompt = '刷新前刚发出的长会话提问';
    let historyFetches = 0;
    const streamRequests = [];
    const { session, doc, storage, app } = await bootActiveHistory(t, {
      id: `s_long_unanswered_${totalServerMessages}`,
      title: '超百条长会话',
      updatedAt: 1_700_000_999_000,
      extra: { hasMoreHistory: true, oldestMessageId: oldestWindowId },
      messages: [
        ...numberedWindow(oldestWindowId, 100, 'local', assistantIf),
        localMsg(unansweredId, 'user', unansweredPrompt, {
          voiceKey: `user_${unansweredId}`, dur: 1.6, ts: 1_700_000_999_000,
        }),
      ],
      updated_at: '2024-06-01 00:10:00',
      message_count: totalServerMessages,
      last_message: `服务端回复 #${totalServerMessages}`,
      mode: 'window',
      prepareHistory(built) {
        return () => {
          historyFetches += 1;
          return historyPayload(built, {
            has_more: true, oldest_id: oldestWindowId, count: serverWindow.length, messages: serverWindow,
          });
        };
      },
      routes(target, options) {
        if (target.includes('/api/chat/stream')) return openChatStream(target, options, streamRequests);
      },
    });

    assert.equal(historyFetches, 1, `服务端 message_count=${totalServerMessages} 时应拉取最近 100 条历史做核对`);
    const activeSession = storedActive();
    assert.equal(activeSession.hasMoreHistory, true);
    assert.equal(activeSession.oldestMessageId, oldestWindowId);
    assert.equal(activeSession.messages.length, 101, '应保留最近 100 条服务端消息 + 末尾 1 条未获回复本地提问');

    const last = activeSession.messages.at(-1);
    assert.equal(last.id, unansweredId);
    assert.equal(last.content, unansweredPrompt);
    assert.equal(last.voiceKey, `user_${unansweredId}`);
    assert.equal(Boolean(last.failed), true, '未获回复本地提问必须标为 failed');
    assert.deepEqual(last.failed, { error: '对话已中断，未收到回复', code: 'cancelled' });
    assert.equal(app.conversation.canRetry(last), true, '末尾失败提问必须支持重试');

    const storedJson = JSON.parse(storage.get('gal2voice.chat.v1'));
    const storedSess = storedJson.sessions.find(s => s.id === session.id);
    assert.equal(storedSess.messages.length, 101);
    assert.deepEqual(storedSess.messages.at(-1).failed, { error: '对话已中断，未收到回复', code: 'cancelled' });

    app.adv.action('log');
    const retryBtn = [...doc.querySelectorAll('.adv-error-btn')].find(b => b.textContent === '重新发送');
    assert.ok(retryBtn, 'LOG 面板中必须渲染「重新发送」按钮');
    retryBtn.click();
    for (let i = 0; i < 5 && streamRequests.length === 0; i++) await tick();
    assert.equal(streamRequests.length, 1, '点击「重新发送」应向 /api/chat/stream 发起重试请求');
    assert.equal(streamRequests[0].prompt, unansweredPrompt);
    app.conversation.stop();
  });
}

test('startup sync in >100 message session merges answered local prompt without duplication or false failure', async t => {
  const answeredPrompt = '刷新期间已完成回复的提问';
  const answeredReply = '服务端已生成的第152条回复';
  const { app } = await bootActiveHistory(t, {
    id: 's_long_already_answered', title: '超百条已完成问答会话', updatedAt: 1_700_000_999_000,
    extra: { hasMoreHistory: true, oldestMessageId: 51 },
    messages: [
      ...numberedWindow(51, 100, 'local', id => id % 2 === 0),
      localMsg('m_local_already_answered', 'user', answeredPrompt, {
        voiceKey: 'user_m_local_already_answered', dur: 2.4, ts: 1_700_000_999_000,
      }),
    ],
    updated_at: '2024-06-01 00:10:02', message_count: 152, last_message: answeredReply,
    mode: 'window', has_more: true,
    historyMessages: numberedWindow(53, 100, 'server', id => id % 2 === 0, {
      151: srv(151, 'user', answeredPrompt, '', '2024-06-01 00:10:01'),
      152: srv(152, 'assistant', answeredReply, '返答152', '2024-06-01 00:10:02'),
    }),
  });

  const activeSession = storedActive();
  assert.equal(activeSession.messages.length, 100, '服务端已完成该轮问答时不应重复追加本地提问');
  assert.equal(activeSession.messages.filter(m => m.content === answeredPrompt).length, 1);

  const pairedUser = activeSession.messages.at(-2);
  assert.equal(pairedUser.id, 'm_151');
  assert.equal(pairedUser.serverMessageId, 151);
  assert.equal(pairedUser.failed, undefined, '已完成问答的提问不得误标 failed');
  assert.equal(pairedUser.voiceKey, 'user_m_local_already_answered', '已完成问答的提问应保留本地录音元数据');
  assert.equal(pairedUser.dur, 2.4);

  const assistantReply = activeSession.messages.at(-1);
  assert.equal(assistantReply.id, 'm_152');
  assert.equal(assistantReply.role, 'assistant');
  assert.equal(assistantReply.content, answeredReply);
  assert.equal(assistantReply.failed, undefined);
  assert.equal(app.conversation.canRetry(pairedUser), false);
  assert.equal(app.conversation.canRetry(assistantReply), false);
});

test('startup sync merges answered local prompt even when previously marked failed without duplication', async t => {
  const newPrompt = '你好';
  const newReply = '我在';
  const fakeAudioBlob = { url: 'blob:http://localhost/fake_user_voice', dur: 1.5 };
  const { app } = await bootActiveHistory(t, {
    id: 's_failed_prompt_reconcile', title: '断连失败提问配对会话', updatedAt: 1_700_000_999_000,
    messages: [
      { id: 'm_10', serverMessageId: 10, role: 'assistant', content: '之前历史消息', ts: 1_700_000_000_000 },
      localMsg('m_local_failed_prompt', 'user', newPrompt, {
        failed: { error: '对话已中断，未收到回复', code: 'cancelled' },
        voiceKey: 'user_m_local_failed_prompt', dur: 1.5, ts: 1_700_000_999_000,
      }),
    ],
    updated_at: '2024-06-01 00:10:02', message_count: 3, last_message: newReply,
    mode: 'window',
    historyMessages: [
      srv(10, 'assistant', '之前历史消息'),
      srv(11, 'user', newPrompt, '', '2024-06-01 00:10:01'),
      srv(12, 'assistant', newReply, '返答12', '2024-06-01 00:10:02'),
    ],
    beforeBoot() { audioStore.set('m_local_failed_prompt', fakeAudioBlob); },
    onCleanup() { audioStore.delete('m_local_failed_prompt'); audioStore.delete('m_11'); },
  });

  const activeSession = storedActive();
  assert.equal(activeSession.messages.filter(m => m.content === newPrompt).length, 1, '提问在服务端已保存时只应出现一次');
  assert.equal(activeSession.messages.length, 3, '会话应包含历史消息、新提问与新回复共3条');

  const pairedUser = activeSession.messages[1];
  assert.equal(pairedUser.id, 'm_11');
  assert.equal(pairedUser.serverMessageId, 11);
  assert.equal(pairedUser.failed, undefined, '已成功配对服务端的提问不应带有 failed');
  assert.equal(pairedUser.voiceKey, 'user_m_local_failed_prompt', '应保留本地录音元数据');
  assert.equal(pairedUser.dur, 1.5);
  assert.equal(audioStore.get('m_11'), fakeAudioBlob, '配对后应将本地录音 blob 映射同步至服务端消息 ID');

  const assistantReply = activeSession.messages[2];
  assert.equal(assistantReply.id, 'm_12');
  assert.equal(assistantReply.role, 'assistant');
  assert.equal(assistantReply.content, newReply);
  assert.equal(assistantReply.failed, undefined);
  assert.equal(app.conversation.canRetry(pairedUser), false);
  assert.equal(app.conversation.canRetry(assistantReply), false);
});

test('startup sync preserves failed local prompt at end when content matches earlier history but server has no new messages', async t => {
  const repeatedPrompt = '重复历史提问';
  const { app } = await bootActiveHistory(t, {
    id: 's_failed_prompt_same_as_history', title: '历史同文未发送成功会话', updatedAt: 1_700_000_999_000,
    messages: [
      { id: 'm_1', serverMessageId: 1, role: 'user', content: repeatedPrompt, ts: 1_700_000_000_000 },
      { id: 'm_2', serverMessageId: 2, role: 'assistant', content: '历史回复', ts: 1_700_000_001_000 },
      localMsg('m_local_failed_tail', 'user', repeatedPrompt, {
        failed: { error: '网络超时，发送失败', code: 'timeout' },
        voiceKey: 'user_m_local_failed_tail', dur: 2.0, ts: 1_700_000_999_000,
      }),
    ],
    updated_at: '2024-06-01 00:00:01', message_count: 2, last_message: '历史回复',
    mode: 'window',
    historyMessages: [
      srv(1, 'user', repeatedPrompt),
      srv(2, 'assistant', '历史回复', '', '2024-06-01 00:00:01'),
    ],
  });

  const activeSession = storedActive();
  assert.equal(activeSession.messages.length, 3, '应保留历史2条 + 末尾1条未发送成功的失败提问');
  const tailMsg = activeSession.messages.at(-1);
  assert.equal(tailMsg.id, 'm_local_failed_tail');
  assert.equal(tailMsg.content, repeatedPrompt);
  assert.ok(tailMsg.failed, '未入库的新提问必须保持 failed 状态');
  assert.equal(app.conversation.canRetry(tailMsg), true, '末尾失败提问必须支持重试');
});

test('startup sync reconciles multiple unpersisted failed prompts correctly when only a subset was persisted on server', async t => {
  const { app } = await bootActiveHistory(t, {
    id: 's_multi_failed_prompts', title: '多条失败提问部分落库会话', updatedAt: 1_700_000_999_000,
    messages: [
      { id: 'm_10', serverMessageId: 10, role: 'assistant', content: '之前历史消息', ts: 1_700_000_000_000 },
      localMsg('m_local_failed_1', 'user', '第一条提问已落库', {
        failed: { error: '流式断开', code: 'cancelled' },
        voiceKey: 'user_m_local_failed_1', dur: 1.2, ts: 1_700_000_998_000,
      }),
      localMsg('m_local_failed_2', 'user', '第二条提问未落库', {
        failed: { error: '网络中断', code: 'timeout' },
        voiceKey: 'user_m_local_failed_2', dur: 2.1, ts: 1_700_000_999_000,
      }),
    ],
    updated_at: '2024-06-01 00:10:02', message_count: 3, last_message: '第一条回复',
    mode: 'window',
    historyMessages: [
      srv(10, 'assistant', '之前历史消息'),
      srv(11, 'user', '第一条提问已落库', '', '2024-06-01 00:10:01'),
      srv(12, 'assistant', '第一条回复', '返答12', '2024-06-01 00:10:02'),
    ],
  });

  const activeSession = storedActive();
  assert.equal(activeSession.messages.length, 4, '应包含历史1条 + 新问答2条 + 末尾1条未落库失败提问');

  const pairedUser = activeSession.messages[1];
  assert.equal(pairedUser.id, 'm_11');
  assert.equal(pairedUser.serverMessageId, 11);
  assert.equal(pairedUser.content, '第一条提问已落库');
  assert.equal(pairedUser.failed, undefined);
  assert.equal(pairedUser.voiceKey, 'user_m_local_failed_1');

  const assistantReply = activeSession.messages[2];
  assert.equal(assistantReply.id, 'm_12');
  assert.equal(assistantReply.content, '第一条回复');
  assert.equal(assistantReply.failed, undefined);

  const tailMsg = activeSession.messages[3];
  assert.equal(tailMsg.id, 'm_local_failed_2');
  assert.equal(tailMsg.content, '第二条提问未落库');
  assert.ok(tailMsg.failed, '未落库的第二条提问应保留在末尾且保持 failed 状态');
  assert.equal(app.conversation.canRetry(tailMsg), true, '末尾未落库失败提问必须支持重试');
});

test('deleting the active session loads history for the newly active fallback session', async t => {
  const { root, toasts } = mountAdvDocument();
  installChatStorage();
  globalThis.location = { origin: 'http://home.test', search: '', hash: '' };
  const fetchedHistoryIds = [];
  let deleteSessionCb = null;

  const fetchImpl = async (url, options = {}) => {
    const target = String(url);
    const method = (options.method || 'GET').toUpperCase();
    if (target.includes('/api/chat/sessions/s_first') && method === 'DELETE') {
      return Response.json({ status: 'deleted' });
    }
    if (target.startsWith('/api/chat/history')) {
      const sid = new URL(target, 'http://local').searchParams.get('session_id');
      fetchedHistoryIds.push(sid);
      if (sid === 's_first') {
        return Response.json({
          session_id: 's_first',
          messages: [{ id: 1, role: 'user', content_chinese: '第一个会话的消息', created_at: '2024-01-01 00:00:01' }],
          has_more: false,
          oldest_id: 1,
        });
      }
      if (sid === 's_second') {
        return Response.json({
          session_id: 's_second',
          messages: [
            { id: 10, role: 'user', content_chinese: '第二个会话的问题', created_at: '2024-01-01 00:00:02' },
            { id: 11, role: 'assistant', content_chinese: '第二个会话的历史回复', created_at: '2024-01-01 00:00:03' },
          ],
          has_more: false,
          oldest_id: 10,
        });
      }
    }
    if (target.includes('/api/chat/sessions')) {
      return Response.json({
        sessions: [
          { id: 's_first', title: '会话一', updated_at: '2024-01-01 00:00:03', message_count: 1, last_message: '第一个会话的消息', settings: {} },
          { id: 's_second', title: '会话二', updated_at: '2024-01-01 00:00:02', message_count: 2, last_message: '第二个会话的历史回复', settings: {} },
        ],
        has_more: false,
        count: 2,
      });
    }
    return startupApiFallback(target, { providers: 'empty' });
  };
  globalThis.window = {
    innerWidth: 1280,
    location: globalThis.location,
    sessionStorage: { getItem: () => '', setItem() {} },
    fetch: fetchImpl,
  };
  globalThis.fetch = (...args) => window.fetch(...args);

  const sessionListEl = document.createElement('div');
  const app = initApp({
    dom: { advRoot: root, sessionList: sessionListEl },
    renderSessionList(_el, { onDelete }) { deleteSessionCb = onDelete; },
    showTitle() {},
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
  assert.equal(storedActive().id, 's_first');
  assert.deepEqual(fetchedHistoryIds, ['s_first']);
  assert.equal(typeof deleteSessionCb, 'function');

  await deleteSessionCb('s_first');
  await tick();

  const nextActive = storedActive();
  assert.equal(nextActive.id, 's_second');
  assert.deepEqual(fetchedHistoryIds, ['s_first', 's_second'], '删除当前会话后必须为自动切到的会话拉取服务端历史记录');
  assert.equal(nextActive.messages.length, 2, '自动切到的会话不应保持空白');
  assert.equal(nextActive.messages[1].content, '第二个会话的历史回复');
});

test('stale startup sync does not open the title screen over a session started by the user', async t => {
  const { root, toasts } = mountAdvDocument();
  installChatStorage();
  globalThis.location = { origin: 'http://home.test', search: '', hash: '' };

  let resolveSessions;
  const sessionsPending = new Promise(resolve => { resolveSessions = resolve; });
  let titleShown = 0;

  const fetchImpl = async (url, options = {}) => {
    const target = String(url);
    const method = (options.method || 'GET').toUpperCase();
    if (target.includes('/api/chat/sessions') && method === 'GET' && !target.includes('/messages')) {
      return await sessionsPending;
    }
    return startupApiFallback(target, { providers: 'empty' });
  };
  globalThis.window = {
    innerWidth: 1280,
    location: globalThis.location,
    sessionStorage: { getItem: () => '', setItem() {} },
    fetch: fetchImpl,
  };
  globalThis.fetch = (...args) => window.fetch(...args);

  const newChatBtn = document.createElement('button');
  const app = initApp({
    dom: { advRoot: root, newChatBtn },
    renderSessionList() {},
    showTitle() { titleShown += 1; },
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

  await tick();
  // User starts a new conversation before startup sync finishes.
  newChatBtn.click();
  const createdId = storedActive().id;

  resolveSessions(Response.json({
    sessions: [
      { id: 's_server_old', title: '旧会话', updated_at: '2024-01-01 00:00:01', message_count: 1, last_message: '旧消息', settings: {} },
    ],
    has_more: false,
    count: 1,
  }));
  await app.ready;
  await tick();

  assert.equal(storedActive().id, createdId, '过期的启动同步不能覆盖用户新建的会话');
  assert.equal(titleShown, 0, '过期的启动同步不应再弹出首页遮罩盖住用户当前对话');
});

test('complete chat backup preserves unconfirmed reading settings when backend save fails', async t => {
  const serverUpdated = '2024-06-01 00:00:03';
  const serverUpdatedAt = serverTimestamp(serverUpdated);
  const session = twoTurnSession({
    id: 's_backup_settings', title: '设置备份测试', updatedAt: serverUpdatedAt,
    user: { id: 'm_1', serverMessageId: 1, content: '你好', ts: serverUpdatedAt - 2000 },
    assistant: { id: 'm_2', serverMessageId: 2, content: '欢迎回来', ts: serverUpdatedAt - 1000 },
  });
  const serverSessions = [
    chatServer(session, { updated_at: serverUpdated, message_count: 2, last_message: '欢迎回来' }),
  ];
  const { app } = await bootSyncedChat(t, {
    sessions: [session],
    activeId: session.id,
    serverSessions,
    mode: 'window',
    fallback: { providers: 'empty' },
    routes: (url, options = {}) => {
      if (url.includes('/api/chat/sessions') && (options.method || 'GET') === 'POST') {
        return Response.json({ detail: '数据库暂时繁忙' }, { status: 500 });
      }
    },
  });

  app.adv.configPanel.open();
  app.adv.configPanel.controls.advPlayerName.value = '旅人';
  assert.equal(await app.adv.configPanel.save(), false, '后端返回 500 时保存应保持未确认');

  const merged = mergeChatBackup({
    format: 'galgame2voice.chat',
    version: 1,
    sessions: [{
      id: session.id,
      title: session.title,
      createdAt: session.createdAt,
      updatedAt: serverUpdatedAt,
      settings: { ...DEFAULT_SESSION_SETTINGS },
      messages: session.messages,
    }],
  }, JSON.parse(exportSessionData()));
  const exported = JSON.parse(merged.content).sessions.find(s => s.id === session.id);
  assert.equal(exported.settings.advPlayerName, '旅人', '完整备份应保留尚未同步到后端的较新本地设置');
});


test('audio error toast points to character settings when message instructs 角色与会话', async t => {
  const toasts = [];
  const calls = [];
  const h = setup(t, {
    callbacks: {
      showToast: (msg, type, opts) => toasts.push({ msg, type, opts }),
      openGlobalSettings: tab => calls.push(['global', tab]),
      openSessionSettings: () => calls.push(['session']),
    },
  });

  const turn1 = h.app.sendMessage('你好');
  await tick();
  const req1 = h.stream.requests.at(-1);
  await req1.event('audio_chunk_error', {
    error: '尚未配置角色包或参考音频，请在「设置中心 → 角色与会话」导入角色包或创建自定义音色后重试。',
  });
  await req1.onEnd('你好！');
  await turn1;

  assert.equal(toasts.length, 1);
  assert.equal(toasts[0].opts.actionLabel, '去选择角色');
  toasts[0].opts.onAction();
  assert.deepEqual(calls, [['session']]);

  toasts.length = 0;
  calls.length = 0;
  const turn2 = h.app.sendMessage('再说一句');
  await tick();
  const req2 = h.stream.requests.at(-1);
  await req2.event('audio_chunk_error', {
    error: '语音引擎尚未启动，请在「设置中心 → 运行状态」启动引擎。',
  });
  await req2.onEnd('好的');
  await turn2;

  assert.equal(toasts.length, 1);
  assert.equal(toasts[0].opts.actionLabel, '检查运行状态');
  toasts[0].opts.onAction();
  assert.deepEqual(calls, [['global', 'dashboard']]);
});

test('title getStatus includes directory_configured from sovits-directory when engine is not ready', async t => {
  const h = setup(t, {
    fetch: async url => {
      if (url === '/api/system/engine-status') return Response.json({ state: 'unavailable', ready: false, message: '未启动', base_url: 'http://127.0.0.1:9880', source: 'default' });
      if (url === '/api/system/sovits-directory') return Response.json({ directory: 'D:/GPT-SoVITS', valid: true, runtime_available: true });
      return Response.json({});
    },
  });
  const status = await h.app.actions.getStatus();
  assert.equal(status.engine.state, 'unavailable');
  assert.equal(status.engine.directory_configured, true);
});

for (const [name, engineReply] of [
  ['a remote engine address', () => Response.json({ state: 'unavailable', ready: false, message: '连不上', base_url: 'http://192.168.1.50:9880', source: 'db' })],
  ['an engine status the server rejects', () => Response.json({ detail: '语音引擎服务地址无效。请在「语音服务」检查并保存服务地址。' }, { status: 400 })],
]) {
  test(`title getStatus does not read the local engine folder for ${name}`, async t => {
    let folderReads = 0;
    const h = setup(t, {
      fetch: async url => {
        if (url === '/api/system/engine-status') return engineReply();
        if (url === '/api/system/sovits-directory') { folderReads += 1; return Response.json({ directory: 'D:/GPT-SoVITS', valid: true }); }
        return Response.json({});
      },
    });
    const status = await h.app.actions.getStatus();
    assert.equal(folderReads, 0);
    assert.equal(status.engine.directory_configured, undefined);
  });
}

test('title screen text-only choice persists voiceMode=false and suppresses audioError toasts', async t => {
  const toasts = [];
  let saved = 0;
  const h = setup(t, {
    callbacks: {
      showToast: (msg, type, opts) => toasts.push({ msg, type, opts }),
      saveState: () => { saved += 1; },
    },
  });
  const home = createTitleScreen(h.app.adv.stage.overlay, {
    documentObj: document,
    actions: {
      ...h.app.actions,
      getStatus: async () => ({
        modelLoaded: true,
        hasModel: false,
        model: { state: 'error', label: '未配置对话模型' },
        engine: { state: 'unavailable' },
      }),
    },
  });
  t.after(() => home.destroy());
  home.open();
  await tick();

  assert.equal(h.state.global.voiceMode, true);
  home.element.querySelector('.home-text-only').click();
  assert.equal(h.state.global.voiceMode, false, '点击「暂时只用文字」应关闭全局语音模式');
  assert.ok(saved >= 1, '关闭语音模式后应持久化保存状态');

  home.close();
  home.open();
  await tick();
  assert.equal(
    home.element.querySelector('.home-text-only').children[0].textContent,
    '✓ 暂时只用文字',
    '重新打开首页后应记住只用文字选择',
  );
  home.close();

  const turn = h.app.sendMessage('你好');
  await tick();
  const req = h.stream.requests.at(-1);
  await req.event('audio_chunk_error', {
    error: '语音引擎尚未启动，请在「设置中心 → 运行状态」启动引擎。',
  });
  await req.onEnd('你好！');
  await turn;

  assert.equal(toasts.length, 0, '关闭语音模式后收到回复不应再弹出语音错误提示');
});

test('entering a session from home with voice turned off still loads its character voice', async t => {
  storedState.sessions = [2, 3].map(id => ({ id: `home_${id}`, title: '角色', messages: [],
    settings: { ...DEFAULT_SESSION_SETTINGS, voiceProfileId: id } }));
  storedState.activeId = 'home_2';
  storedState.global.voiceMode = false;
  t.after(() => { storedState.global.voiceMode = true; });
  const switches = [];
  const h = setup(t, { callbacks: { state: storedState, getActive: storedActive, getSession: storedSession, getVoiceProfiles },
    fetch: async (url, options = {}) => {
      if (url === '/api/voice/profiles') return Response.json({ profiles: [{ id: 2, name: '甲' }, { id: 3, name: '乙' }], active_profile_id: null });
      if (url === '/api/voice/switch') { switches.push(JSON.parse(options.body).profile_id); return Response.json({ profile: '乙' }); }
      return Response.json({ backgrounds: [] });
    } });
  initVoiceSettings({});
  await h.app.actions.loadSession('home_3');
  for (let i = 0; i < 20 && !switches.length; i += 1) await tick();
  assert.deepEqual(switches, [3]);
});
