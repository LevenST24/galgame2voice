import assert from 'node:assert/strict';
import test from 'node:test';
// Import before installing a document: don't auto-boot authentication or devices.
import { setup } from './helpers/adv_main.mjs';
import { tick } from './helpers/chat_stream.mjs';
import { state as storedState, getActive as storedActive, getSession as storedSession, DEFAULT_SESSION_SETTINGS } from '../frontend/src/store.js';
import { initVoiceSettings, ensureSessionVoice, getVoiceProfiles } from '../frontend/src/controllers/voice_settings.js';

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
