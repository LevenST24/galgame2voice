import assert from 'node:assert/strict';
import { test, beforeEach, afterEach } from 'node:test';
import { playVoice, playUserVoice, playSingleAudio, stopCurrentVoice, getCurrentVoice, initAudioPlayerUi, toggleVoiceMode } from '../frontend/src/controllers/audio_player_ui.js';
import { audioStore } from '../frontend/src/voice.js';
import { state, loadState } from '../frontend/src/store.js';
import { clearMemAudioCache } from '../frontend/src/cache.js';

const tick = () => new Promise(resolve => setTimeout(resolve, 0));
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return { promise, resolve }; };
const blob = () => new Blob(['audio']);
const control = () => ({ loading: false, playing: false, setLoading(v) { this.loading = v; }, setPlaying(v) { this.playing = v; }, setProgress() {} });
beforeEach(() => {
  globalThis.document = { getElementById: () => null };
  globalThis.window = {};
  globalThis.localStorage = { getItem: () => null, setItem() {} };
  globalThis.fetch = async () => new Response(blob());
  globalThis.Audio = class { async play() {} pause() {} };
  state.sessions = [{ id: 'first', settings: { voiceProfileId: 1 }, messages: [] }, { id: 'second', settings: { voiceProfileId: 2 }, messages: [] }];
  state.activeId = 'first';
  initAudioPlayerUi({}, {});
});
afterEach(() => {
  stopCurrentVoice(); clearMemAudioCache(); delete globalThis.caches;
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
test('playVoice forwards only a user recording and ignores an assistant message', async () => {
  const fetches = [];
  globalThis.fetch = async url => { fetches.push(String(url)); return new Response(blob()); };
  let plays = 0;
  globalThis.Audio = class { async play() { plays++; } pause() {} };
  const assistantCtl = control();
  const assistant = playVoice({ id: 'a1', role: 'assistant', japanese: 'こんにちは', audioUrls: ['/a1.wav'] }, assistantCtl);
  assert.equal(assistant, undefined);
  await tick();
  assert.equal(fetches.length, 0);
  assert.equal(assistantCtl.loading, false);
  assert.equal(assistantCtl.playing, false);
  assert.equal(getCurrentVoice(), null);
  audioStore.set('u1', { url: 'mem:u1', dur: 1 });
  try {
    const userCtl = control();
    await playVoice({ id: 'u1', role: 'user' }, userCtl);
    assert.equal(plays, 1);
    assert.equal(userCtl.playing, true);
    assert.equal(getCurrentVoice()?.msgId, 'u1');
  } finally {
    audioStore.delete('u1');
  }
});
