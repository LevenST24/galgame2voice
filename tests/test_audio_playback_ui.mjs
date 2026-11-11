import assert from 'node:assert/strict';
import { test, beforeEach, afterEach } from 'node:test';
import { playAiVoice, playUserVoice, playSingleAudio, stopCurrentVoice, getCurrentVoice, initAudioPlayerUi, toggleVoiceMode } from '../frontend/src/controllers/audio_player_ui.js';
import { streamAudioController } from '../frontend/src/audio_player.js';
import { state, loadState } from '../frontend/src/store.js';
import { clearMemAudioCache } from '../frontend/src/cache.js';

const tick = () => new Promise(resolve => setTimeout(resolve, 0));
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return { promise, resolve }; };
const blob = () => new Blob(['audio']);
const control = () => ({ loading: false, playing: false, setLoading(v) { this.loading = v; }, setPlaying(v) { this.playing = v; }, setProgress() {} });
const message = id => ({ id, role: 'assistant', japanese: 'こんにちは', audioUrls: [`/${id}.wav`] });
const originals = { playChunks: streamAudioController.playChunks, interrupt: streamAudioController.interrupt };
let played;
beforeEach(() => {
  globalThis.document = { getElementById: () => null };
  globalThis.window = {};
  globalThis.localStorage = { getItem: () => null, setItem() {} };
  played = [];
  streamAudioController.playChunks = async (chunks, ctl, id) => { played.push({ id, chunks }); };
  globalThis.fetch = async () => new Response(blob());
  globalThis.Audio = class { async play() {} pause() {} };
  state.sessions = [{ id: 'first', settings: { voiceProfileId: 1 }, messages: [] }, { id: 'second', settings: { voiceProfileId: 2 }, messages: [] }];
  state.activeId = 'first';
  initAudioPlayerUi({}, {});
});
afterEach(() => {
  stopCurrentVoice(); clearMemAudioCache(); delete globalThis.caches;
  streamAudioController.playChunks = originals.playChunks;
});
test('a slow old message cannot take playback back from the newer click', async () => {
  const gate = deferred();
  globalThis.fetch = async url => url === '/old.wav' ? gate.promise : new Response(blob());
  const oldCtl = control(); const old = playAiVoice(message('old'), oldCtl);
  await tick(); await playAiVoice(message('new'), control());
  gate.resolve(new Response(blob())); await old;
  assert.deepEqual(played.map(p => p.id), ['new']); assert.equal(oldCtl.loading, false);
});
test('switching sessions cancels preparation without replay or synthesis', async () => {
  let signal; const gate = deferred();
  globalThis.fetch = async (_url, options) => { signal = options.signal; return gate.promise; };
  const ctl = control(); const pending = playAiVoice(message('old'), ctl);
  await tick(); stopCurrentVoice(); state.activeId = 'second';
  assert.equal(ctl.loading, false); assert.equal(signal.aborted, true);
  gate.resolve(new Response(blob())); await pending;
  assert.equal(played.length, 0); assert.equal(getCurrentVoice(), null);
});
test('clicking a loading message again cancels its request', async () => {
  let requests = 0;
  globalThis.fetch = async () => { requests++; return new Promise(() => {}); };
  const msg = message('cancel'); const ctl = control();
  const pending = playAiVoice(msg, ctl); await tick(); await playAiVoice(msg, ctl); await pending;
  assert.equal(requests, 1); assert.equal(ctl.loading, false);
});
test('transient network failures preserve URLs and never trigger synthesis', async () => {
  const msg = message('offline'); let requests = 0;
  globalThis.fetch = async () => { requests++; throw new TypeError('Failed to fetch'); };
  await playAiVoice(msg, control());
  assert.deepEqual(msg.audioUrls, ['/offline.wav']); assert.equal(requests, 1);
});
const MISSING_AUDIO_GUIDANCE = '该条历史语音文件已过期或不存在，可在历史台词（LOG）中重新生成单句语音。';
const captureToasts = () => {
  const toasts = [];
  const root = { querySelectorAll: () => [], appendChild: el => toasts.push(el) };
  globalThis.document = {
    getElementById: id => id === 'toastRoot' ? root : null,
    createElement: () => ({ dataset: {}, children: [], append(...children) { this.children.push(...children); },
      addEventListener() {}, remove() {}, classList: { add() {} } }),
  };
  return toasts;
};
test('expired history audio shows regeneration guidance instead of synthesizing', async () => {
  const toasts = captureToasts(); const requested = [];
  globalThis.fetch = async url => { requested.push(url); return new Response('', { status: 404 }); };
  const msg = message('expired'); const ctl = control(); ctl.setPlaying(true);
  await playAiVoice(msg, ctl);
  assert.deepEqual(requested, ['/expired.wav']); assert.deepEqual(msg.audioUrls, ['/expired.wav']);
  assert.equal(ctl.playing, false); assert.equal(ctl.loading, false);
  clearTimeout(toasts[0]._timer); assert.equal(toasts[0].dataset.msg, MISSING_AUDIO_GUIDANCE);
});
test('a message without stored audio shows regeneration guidance and makes no request', async () => {
  const toasts = captureToasts(); let requests = 0;
  globalThis.fetch = async () => { requests++; return new Response(blob()); };
  const ctl = control(); ctl.setPlaying(true);
  await playAiVoice({ id: 'no_urls', role: 'assistant', japanese: 'こんにちは', audioUrls: [] }, ctl);
  assert.equal(requests, 0); assert.equal(ctl.playing, false);
  clearTimeout(toasts[0]._timer); assert.equal(toasts[0].dataset.msg, MISSING_AUDIO_GUIDANCE);
});
test('cancelled user-recording cache reads cannot steal playback', async () => {
  const gate = deferred(); window.caches = globalThis.caches = { open: () => gate.promise };
  let plays = 0; globalThis.Audio = class { async play() { plays++; } pause() {} };
  const pending = playUserVoice({ id: 'recording', role: 'user', voiceKey: 'user_recording' }, control());
  await tick(); stopCurrentVoice(); gate.resolve({ match: async () => new Response(blob()) }); await pending;
  assert.equal(plays, 0);
});
test('the auto-play toggle is saved immediately so a reload keeps the choice', () => {
  const saved = new Map();
  globalThis.localStorage = { getItem: key => saved.get(key) ?? null, setItem: (key, value) => saved.set(key, value), removeItem: key => saved.delete(key) };
  loadState(); // start from this test's storage, as a freshly opened page would
  initAudioPlayerUi({}, {});
  state.global.voiceMode = true;
  toggleVoiceMode();
  const stored = JSON.parse(saved.get('gal2voice.chat.v1'));
  assert.equal(stored.global.voiceMode, false);
  toggleVoiceMode();
  assert.equal(JSON.parse(saved.get('gal2voice.chat.v1')).global.voiceMode, true);
});
test('a new browser starts with voice on, and a saved choice to turn it off is kept', () => {
  const saved = new Map();
  globalThis.localStorage = { getItem: key => saved.get(key) ?? null, setItem: (key, value) => saved.set(key, value), removeItem: key => saved.delete(key) };
  loadState();
  assert.equal(state.global.voiceMode, true);
  saved.clear();
  saved.set('gal2voice.chat.v1', JSON.stringify({ sessions: [], activeId: null, global: { voiceMode: false } }));
  loadState();
  assert.equal(state.global.voiceMode, false);
});
test('a rejected browser play releases ownership so the next click can retry', async () => {
  const ctl = control();
  const audio = { async play() { throw new Error('NotAllowedError'); }, pause() {} };
  playSingleAudio(() => audio, 'single', ctl); await tick();
  assert.equal(getCurrentVoice(), null); assert.equal(ctl.playing, false);
});
test('playUserVoice resets playing state on controller when recording is missing or expired', async () => {
  const ctl = control();
  ctl.setPlaying(true);
  window.caches = globalThis.caches = { open: async () => ({ match: async () => null }) };
  await playUserVoice({ id: 'missing_rec', role: 'user', voiceKey: 'expired_key' }, ctl);
  assert.equal(ctl.playing, false);
});
