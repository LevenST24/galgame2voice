import assert from 'node:assert/strict';
import test from 'node:test';
import { createLinePlayer } from '../frontend/src/adv/line_player.js';
import { fakeClock, microtasks } from './helpers/adv_dom.mjs';

function setup({ startNotification = false } = {}) {
  const clock = fakeClock();
  const revealed = [];
  let start, end;
  const voice = { supportsStartNotification: startNotification,
    play(_index, _chunks, options = {}) { start = options.onStart; return new Promise(resolve => { end = resolve; }); },
    stop() { end?.('interrupted'); },
  };
  const player = createLinePlayer({ clock, voice,
    settings: { revealMsPerGrapheme: 0, autoMode: 'voice-priority', autoBaseMs: 1200, autoPerCharMs: 0 },
    render: { reveal(text) { revealed.push(text); } } });
  player.startTurn('turn');
  player.pushLine('turn', { index: 0, zh: '第一句', ja: '最初の言葉' });
  player.pushLine('turn', { index: 1, zh: '第二句', ja: '次の言葉' });
  return { player, clock, revealed, started: () => start?.(), ended: result => end?.(result) };
}

test('voice-priority holds both subtitles past 1.5 seconds until sound actually starts', () => {
  const h = setup({ startNotification: true });
  h.clock.tick(30000);
  assert.deepEqual(h.revealed, []);
  assert.equal(h.player.state, 'waiting_audio');
  h.player.setLineAudio('turn', 0, { status: 'ready', chunks: ['/synthetic.wav'] });
  h.clock.tick(5000);
  assert.deepEqual(h.revealed, []);
  h.started();
  assert.deepEqual(h.revealed, ['第一句']);
});

test('finishing without audio releases a synchronized sentence, including after an overlay', () => {
  for (const paused of [false, true]) {
    const h = setup({ startNotification: true });
    if (paused) h.player.suspend('settings');
    h.player.settle('turn', { ok: true });
    if (paused) h.player.resume('settings');
    assert.deepEqual(h.revealed, ['第一句']);
  }
});

test('normal reading mode still previews text after its finite wait', () => {
  const h = setup();
  h.player.setSettings({ autoMode: 'normal' });
  h.player.startTurn('normal');
  h.player.pushLine('normal', { index: 0, zh: '先看文字' });
  h.clock.tick(1500);
  assert.deepEqual(h.revealed, ['先看文字']);
});

test('voice-priority AUTO keeps the current sentence while its audio is still being generated', () => {
  const { player, clock } = setup();
  player.setAuto(true);
  clock.tick(1500);
  clock.tick(10000);
  assert.equal(player.currentIndex, 0);
  assert.equal(player.state, 'waiting_audio');
});

test('download and decode finish before the sentence typewriter begins', () => {
  const h = setup({ startNotification: true });
  h.player.setLineAudio('turn', 0, { status: 'ready', chunks: ['/synthetic.wav'] });
  assert.equal(h.player.state, 'waiting_audio');
  assert.deepEqual(h.revealed, []);
  h.started();
  assert.deepEqual(h.revealed, ['第一句']);
});

test('late audio still owns the sentence until playback ends in voice-priority AUTO', async () => {
  const h = setup(); h.player.setAuto(true); h.clock.tick(1500);
  h.player.setLineAudio('turn', 0, { status: 'ready', chunks: ['/synthetic.wav'] });
  h.clock.tick(10000); assert.equal(h.player.currentIndex, 0);
  h.ended('ended'); await microtasks(); h.clock.tick(1200);
  assert.equal(h.player.currentIndex, 1);
});

test('playback failure before sound starts still releases the text', async () => {
  const h = setup({ startNotification: true });
  h.player.setLineAudio('turn', 0, { status: 'ready', chunks: ['/synthetic.wav'] });
  h.ended('failed'); await microtasks();
  assert.deepEqual(h.revealed, ['第一句']);
  assert.equal(h.player.state, 'waiting_advance');
});

test('a start callback from an interrupted turn cannot reveal or move the new turn', async () => {
  const h = setup({ startNotification: true });
  h.player.setLineAudio('turn', 0, { status: 'ready', chunks: ['/synthetic.wav'] });
  h.player.startTurn('new');
  h.player.pushLine('new', { index: 0, zh: '新一轮', ja: '' });
  const before = [...h.revealed];
  h.started(); h.ended('interrupted'); await microtasks();
  assert.deepEqual(h.revealed, before);
  assert.equal(h.player.state, 'waiting_audio');
});

for (const status of ['failed', 'skipped']) test(`${status} audio releases a pending AUTO sentence`, () => {
  const { player, clock } = setup();
  player.setAuto(true); clock.tick(1500);
  player.setLineAudio('turn', 0, { status, chunks: [] });
  clock.tick(1200);
  assert.equal(player.currentIndex, 1);
});

test('turn completion without an audio event releases the pending AUTO wait', () => {
  const { player, clock } = setup();
  player.setAuto(true); clock.tick(1500);
  player.settle('turn', { ok: true }); clock.tick(1200);
  assert.equal(player.currentIndex, 1);
});

test('turning voice off releases a pending AUTO wait', () => {
  const { player, clock } = setup();
  player.setAuto(true); clock.tick(1500);
  player.setSettings({ voiceEnabled: false }); clock.tick(1200);
  assert.equal(player.currentIndex, 1);
});

test('disabling voice while settings are open releases the sentence on return', () => {
  const h = setup({ startNotification: true });
  h.player.suspend('settings');
  h.player.setSettings({ voiceEnabled: false });
  assert.deepEqual(h.revealed, [], 'the settings overlay must not reveal a sentence behind it');
  h.player.resume('settings');
  assert.deepEqual(h.revealed, ['第一句']);
  assert.equal(h.player.state, 'waiting_advance');
  h.player.setLineAudio('turn', 0, { status: 'ready', chunks: ['/synthetic.wav'] });
  assert.deepEqual(h.revealed, ['第一句']);
});

test('reenabling voice before returning still waits for the audio', () => {
  const h = setup({ startNotification: true });
  h.player.suspend('settings');
  h.player.setSettings({ voiceEnabled: false });
  h.player.setSettings({ voiceEnabled: true });
  h.player.resume('settings');
  assert.equal(h.player.state, 'waiting_audio');
  assert.deepEqual(h.revealed, []);
  h.player.setLineAudio('turn', 0, { status: 'ready', chunks: ['/synthetic.wav'] });
  h.started();
  assert.deepEqual(h.revealed, ['第一句']);
});

test('manual advance can reveal text and then move on while audio is pending', () => {
  const { player, clock } = setup();
  player.setAuto(true); clock.tick(1500); player.advance();
  assert.equal(player.state, 'waiting_advance');
  player.advance();
  assert.equal(player.currentIndex, 1);
});

test('successive voiced sentences wait for their own playback start and end before AUTO continues', async () => {
  const h = setup({ startNotification: true });
  h.player.setAuto(true);
  for (let index = 0; index < 2; index++) {
    assert.equal(h.player.currentIndex, index);
    h.clock.tick(10000);
    assert.equal(h.revealed.length, index);
    h.player.setLineAudio('turn', index, { status: 'ready', chunks: [`/sentence-${index}.wav`] });
    if (index === 1) h.player.settle('turn', { ok: true });
    h.clock.tick(5000);
    assert.equal(h.revealed.length, index, 'download or final reply cannot release text before sound');
    h.started();
    assert.equal(h.revealed.length, index + 1);
    h.clock.tick(30000);
    assert.equal(h.player.currentIndex, index, 'AUTO must not outrun a long spoken sentence');
    h.ended('ended');
    await microtasks();
    h.clock.tick(1200);
  }
  assert.equal(h.player.state, 'input');
});
