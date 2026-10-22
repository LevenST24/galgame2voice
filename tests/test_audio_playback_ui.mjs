import assert from 'node:assert/strict';
import { test, beforeEach, afterEach } from 'node:test';
import { playAiVoice, playUserVoice, playSingleAudio, synthesizeAiVoice, stopCurrentVoice, getCurrentVoice, initAudioPlayerUi, toggleVoiceMode } from '../frontend/src/controllers/audio_player_ui.js';
import { streamAudioController } from '../frontend/src/audio_player.js';
import { state, loadState } from '../frontend/src/store.js';
import { clearMemAudioCache } from '../frontend/src/cache.js';

const tick = () => new Promise(resolve => setTimeout(resolve, 0));
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return { promise, resolve }; };
const blob = () => new Blob(['audio']);
const control = () => ({ loading: false, playing: false, setLoading(v) { this.loading = v; }, setPlaying(v) { this.playing = v; }, setProgress() {} });
const message = id => ({ id, role: 'assistant', japanese: 'こんにちは', audioUrls: [`/${id}.wav`] });
const originals = { playChunks: streamAudioController.playChunks, interrupt: streamAudioController.interrupt };
let played, synthesized;
beforeEach(() => {
  globalThis.document = { getElementById: () => null };
  globalThis.window = {};
  globalThis.localStorage = { getItem: () => null, setItem() {} };
  played = []; synthesized = [];
  streamAudioController.playChunks = async (chunks, ctl, id) => { played.push({ id, chunks }); };
  globalThis.fetch = async (url, options) => { if (url.includes('synthesize')) synthesized.push(JSON.parse(options.body)); return new Response(blob()); };
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
test('confirmed missing audio regenerates once, using the initiating voice', async () => {
  globalThis.fetch = async (url, options) => {
    if (url === '/expired.wav') return new Response('', { status: 404 });
    synthesized.push(JSON.parse(options.body)); return new Response(blob());
  };
  const msg = message('expired'); await playAiVoice(msg, control());
  assert.equal(synthesized.length, 1); assert.equal(synthesized[0].voice_profile_id, 1);
  assert.deepEqual(msg.audioUrls, ['msg_expired']);
});
test('translation completing after cancellation cannot synthesize with the next character', async () => {
  const gate = deferred(); initAudioPlayerUi({}, { resolveJapanese: () => gate.promise });
  const pending = synthesizeAiVoice({ id: 'translate', role: 'assistant' }, control());
  await tick(); stopCurrentVoice(); state.activeId = 'second';
  gate.resolve('こんにちは'); await pending;
  assert.equal(synthesized.length, 0); assert.equal(getCurrentVoice(), null);
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
