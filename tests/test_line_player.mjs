import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createLinePlayer, DEFAULT_SETTINGS } from '../frontend/src/adv/line_player.js';
import { countGraphemes, countAutoGraphemes, splitGraphemes } from '../frontend/src/adv/graphemes.js';

test('settle retains undisplayed hidden queue until unhide activates it', () => {
  const player = createLinePlayer({ clock: createFakeClock(), settings: { voiceEnabled: false, revealMsPerGrapheme: 0 } });
  player.startTurn('hidden'); player.hide();
  player.pushLine('hidden', { index: 0, zh: '等我', ja: '', act: {} }); player.settle('hidden', { ok: true });
  assert.equal(player.state, 'waiting_line'); assert.equal(player.currentIndex, null);
  player.unhide(); assert.equal(player.currentIndex, 0);
});

test('named pause reasons cannot resume one another', () => {
  const player = createLinePlayer({ clock: createFakeClock() }); player.startTurn('paused');
  player.suspend('overlay'); player.suspend('dialog'); player.resume('overlay');
  assert.equal(player.state, 'suspended'); player.resume('dialog'); assert.equal(player.state, 'waiting_line');
});

function createFakeClock(initialTime = 0) {
  let currentTime = initialTime;
  let nextTimerId = 1;
  const timers = new Map();

  return {
    now() {
      return currentTime;
    },
    setTimeout(fn, ms) {
      const id = nextTimerId++;
      timers.set(id, { fn, dueTime: currentTime + Math.max(0, ms) });
      return id;
    },
    clearTimeout(id) {
      timers.delete(id);
    },
    tick(ms) {
      currentTime += ms;
      while (true) {
        let earliest = null;
        for (const [id, t] of timers.entries()) {
          if (t.dueTime <= currentTime) {
            if (!earliest || t.dueTime < earliest.dueTime || (t.dueTime === earliest.dueTime && id < earliest.id)) {
              earliest = { id, ...t };
            }
          }
        }
        if (!earliest) break;
        timers.delete(earliest.id);
        earliest.fn();
      }
    },
    get activeTimerCount() {
      return timers.size;
    },
    get pendingDueTimes() {
      return Array.from(timers.values()).map(t => t.dueTime);
    }
  };
}

function createFakeVoice() {
  const calls = [];
  const pending = [];
  return {
    calls,
    pending,
    play(lineIndex, chunks) {
      calls.push({ type: 'play', lineIndex, chunks });
      let resolve;
      const promise = new Promise(r => { resolve = r; });
      pending.push({ lineIndex, chunks, resolve, promise });
      return promise;
    },
    stop(options) {
      calls.push({ type: 'stop', options });
      while (pending.length > 0) {
        const item = pending.shift();
        item.resolve('interrupted');
      }
    },
    resolveLast(result = 'ended') {
      const item = pending.shift();
      if (item) item.resolve(result);
    }
  };
}

function createRecordingRender() {
  const calls = [];
  return {
    calls,
    thinking(on) { calls.push(['thinking', on]); },
    activate(line, options) { calls.push(['activate', line, options]); },
    reveal(pageText, options) { calls.push(['reveal', pageText, options]); },
    completeReveal() { calls.push(['completeReveal']); },
    glyph(on) { calls.push(['glyph', on]); },
    enterInput(options) { calls.push(['enterInput', options]); },
    showError(error) { calls.push(['showError', error]); },
    logOnly(line) { calls.push(['logOnly', line]); },
  };
}

function setup(options = {}) {
  const clock = createFakeClock();
  const voice = createFakeVoice();
  const render = createRecordingRender();
  const events = [];

  const player = createLinePlayer({
    clock,
    voice,
    render,
    ...options,
  });

  for (const ev of ['state', 'activate', 'logged', 'input']) {
    player.on(ev, (...args) => events.push([ev, ...args]));
  }

  return { clock, voice, render, player, events };
}

const sampleLine0 = { index: 0, zh: '你好，世界！', ja: 'こんにちは、世界！', emotion: 'gentle', act: {} };
const sampleLine1 = { index: 1, zh: '今天天气真好。', ja: 'いい天気ですね。', emotion: 'happy', act: {} };

async function flushMicrotasks() {
  await new Promise(r => setImmediate(r));
}

// -------------------------------------------------------------
// 1. Grapheme counting and fallbacks
// -------------------------------------------------------------
test('grapheme counting: emoji, combining marks, decorative marks, and fallback', () => {
  // Emoji with skin tone
  assert.equal(countGraphemes('👍🏽'), 1);
  // ZWJ family emoji
  assert.equal(countGraphemes('👨‍👩‍👧‍👦'), 1);
  // Combining mark (e + acute accent)
  assert.equal(countGraphemes('e\u0301'), 1);

  // Fallback without Intl.Segmenter via option
  assert.equal(countGraphemes('👍🏽', { useSegmenter: false }), 2);
  assert.equal(countGraphemes('e\u0301', { useSegmenter: false }), 2);

  // Runtime fallback when Intl.Segmenter is absent
  const originalSegmenter = Intl.Segmenter;
  try {
    delete Intl.Segmenter;
    assert.equal(countGraphemes('👍🏽'), 2);
    assert.equal(countGraphemes('e\u0301'), 2);
    assert.equal(countAutoGraphemes('「你好」♪ '), 2);
  } finally {
    Intl.Segmenter = originalSegmenter;
  }

  // AUTO graphemes count excludes decorative marks and whitespace
  const decorativeText = '「（【♪～〜】）」';
  assert.equal(countAutoGraphemes(decorativeText), 0);
  assert.equal(countAutoGraphemes('「你好」♪ '), 2);
  assert.equal(countGraphemes('「你好」♪'), 5);

  // Fallback AUTO graphemes
  assert.equal(countAutoGraphemes('「你好」♪ ', { useSegmenter: false }), 2);
});

// -------------------------------------------------------------
// 2. Section 4.1 Table coverage
// -------------------------------------------------------------
test('Table 4.1 Row 1: turn starts, no line yet -> thinking ellipsis', () => {
  const { player, render, events } = setup();
  assert.equal(player.state, 'idle');

  player.startTurn('turn-1');
  assert.equal(player.state, 'waiting_line');
  assert.deepEqual(events, [['state', 'waiting_line']]);
  assert.deepEqual(render.calls, [['glyph', false], ['thinking', true]]);
});

test('Table 4.1 Row 2 & 3: next line available, audio ready -> plays voice and reveals', async () => {
  const { player, clock, voice, render, events } = setup();
  player.startTurn('turn-1');
  render.calls.length = 0;
  events.length = 0;

  // Audio arrived early / ready
  player.setLineAudio('turn-1', 0, { status: 'ready', chunks: [{ index: 0, line: 0, audio_url: '/a.wav', sentence: 's' }] });
  player.pushLine('turn-1', sampleLine0);

  // Thinking turned off, line activated, voice played, reveal started
  assert.equal(player.currentIndex, 0);
  assert.equal(player.page, 0);
  assert.equal(player.state, 'revealing');
  assert.equal(voice.calls.length, 1);
  assert.equal(voice.calls[0].type, 'play');
  assert.equal(voice.calls[0].lineIndex, 0);

  // Check render calls: thinking(false), activate, glyph(false), reveal
  assert.deepEqual(render.calls[0], ['thinking', false]);
  assert.deepEqual(render.calls[1], ['activate', sampleLine0, { skipping: false, reducedMotion: false }]);
  assert.deepEqual(render.calls[2], ['glyph', false]);
  assert.equal(render.calls[3][0], 'reveal');
  assert.equal(render.calls[3][1], sampleLine0.zh);

  // Let reveal finish
  const duration = countGraphemes(sampleLine0.zh) * 30;
  clock.tick(duration);

  assert.equal(player.state, 'waiting_advance');
  assert.deepEqual(render.calls.at(-2), ['completeReveal']);
  assert.deepEqual(render.calls.at(-1), ['glyph', true]);
});

test('Table 4.1 Row 4: audio skipped or failed -> reveal without waiting, no voice', () => {
  const { player, clock, voice, render } = setup();
  player.startTurn('turn-1');
  player.setLineAudio('turn-1', 0, { status: 'failed', chunks: [] });
  player.pushLine('turn-1', sampleLine0);

  assert.equal(player.state, 'revealing');
  assert.equal(voice.calls.length, 0); // no voice played
});

test('Table 4.1 Row 5: audio absent for 1500ms -> reveals, late audio plays while line is current', async () => {
  const { player, clock, voice, render } = setup();
  player.startTurn('turn-1');
  player.pushLine('turn-1', sampleLine0);

  // Audio unknown -> waiting_audio
  assert.equal(player.state, 'waiting_audio');
  clock.tick(1499);
  assert.equal(player.state, 'waiting_audio');

  // At 1500ms, reveal begins
  clock.tick(1);
  assert.equal(player.state, 'revealing');
  assert.equal(voice.calls.length, 0);

  // Late audio arrives while still on line 0 -> plays voice
  player.setLineAudio('turn-1', 0, { status: 'ready', chunks: [{ index: 0, line: 0, audio_url: '/a.wav' }] });
  assert.equal(voice.calls.length, 1);
  assert.equal(voice.calls[0].type, 'play');
});

test('Table 4.1 Row 5: late audio arriving after moving to next line is ignored', async () => {
  const { player, clock, voice } = setup();
  player.startTurn('turn-1');
  player.pushLine('turn-1', sampleLine0);
  player.pushLine('turn-1', sampleLine1);

  // Wait out audio for line 0
  clock.tick(1500); // reveals line 0
  clock.tick(1000); // finishes reveal
  player.advance(); // moves to line 1

  assert.equal(player.currentIndex, 1);
  voice.calls.length = 0;

  // Late audio for line 0 arrives now
  player.setLineAudio('turn-1', 0, { status: 'ready', chunks: [{ index: 0, line: 0, audio_url: '/old.wav' }] });
  assert.equal(voice.calls.length, 0, 'late audio for past line is ignored');
});

test('Table 4.1 Row 6: advance during reveal completes page without stopping voice', async () => {
  const { player, clock, voice, render } = setup();
  player.startTurn('turn-1');
  player.setLineAudio('turn-1', 0, { status: 'ready', chunks: [{ audio_url: '/a.wav' }] });
  player.pushLine('turn-1', sampleLine0);

  assert.equal(player.state, 'revealing');
  clock.tick(50); // halfway through reveal

  player.advance();
  assert.equal(player.state, 'waiting_advance');
  assert.ok(render.calls.some(([name]) => name === 'completeReveal'));
  assert.equal(voice.calls.filter(c => c.type === 'stop').length, 0, 'voice not stopped');
});

test('Table 4.1 Row 7: advance after reveal moves to next line (single page)', async () => {
  const { player, clock, voice } = setup();
  player.startTurn('turn-1');
  player.setLineAudio('turn-1', 0, { status: 'ready', chunks: [{ audio_url: '/a.wav' }] });
  player.pushLine('turn-1', sampleLine0);
  player.pushLine('turn-1', sampleLine1);

  clock.tick(500); // complete reveal
  assert.equal(player.state, 'waiting_advance');

  player.advance();
  assert.equal(player.currentIndex, 1);
  assert.equal(voice.calls.filter(c => c.type === 'stop').length, 1, 'interruptVoice stops line 0 voice');
});

test('Table 4.1 Row 8: last line exhausted but generation open -> waiting_line with thinking(true)', () => {
  const { player, clock, render } = setup();
  player.startTurn('turn-1');
  player.setLineAudio('turn-1', 0, { status: 'skipped' });
  player.pushLine('turn-1', sampleLine0);

  clock.tick(500); // complete reveal
  render.calls.length = 0;

  player.advance();
  assert.equal(player.state, 'waiting_line');
  assert.deepEqual(render.calls.at(-1), ['thinking', true]);
});

test('Table 4.1 Row 9: last line exhausted and turn settled -> enter player input mode', () => {
  const { player, clock, render, events } = setup();
  player.startTurn('turn-1');
  player.setLineAudio('turn-1', 0, { status: 'skipped' });
  player.pushLine('turn-1', sampleLine0);

  // Server finishes early
  player.settle('turn-1', { ok: true });
  // Settle does not jump over unread line!
  assert.equal(player.state, 'revealing');

  clock.tick(500); // complete reveal
  assert.equal(player.state, 'waiting_advance');

  player.advance();
  assert.equal(player.state, 'input');
  assert.deepEqual(render.calls.at(-1), ['enterInput', { canSend: true }]);
  assert.ok(events.some(([ev, payload]) => ev === 'input' && payload.canSend === true));
});

test('Table 4.1 Row 10: reset cancels owned timers and goes to idle', () => {
  const { player, clock, voice } = setup();
  player.startTurn('turn-1');
  player.pushLine('turn-1', sampleLine0); // waiting_audio timer active
  assert.ok(clock.activeTimerCount > 0);

  player.reset();
  assert.equal(player.state, 'idle');
  assert.equal(clock.activeTimerCount, 0);

  // Stale turn events ignored
  player.pushLine('turn-1', sampleLine1);
  assert.equal(player.state, 'idle');
});

// -------------------------------------------------------------
// 3. Multi-page lines and pagination
// -------------------------------------------------------------
test('multi-page line: advances through pages without stopping voice', () => {
  const paginate = (line, text) => [text.slice(0, 3), text.slice(3)];
  const { player, clock, voice, render } = setup({ paginate });

  player.startTurn('turn-1');
  player.setLineAudio('turn-1', 0, { status: 'ready', chunks: [{ audio_url: '/a.wav' }] });
  player.pushLine('turn-1', sampleLine0);

  assert.equal(player.page, 0);
  clock.tick(3 * 30); // finish page 0
  assert.equal(player.state, 'waiting_advance');

  // Advance to page 1
  player.advance();
  assert.equal(player.page, 1);
  assert.equal(voice.calls.filter(c => c.type === 'stop').length, 0, 'page advance does not stop voice');

  clock.tick(3 * 30); // finish page 1
  assert.equal(player.state, 'waiting_advance');

  // Advance past final page
  player.advance();
  assert.equal(voice.calls.filter(c => c.type === 'stop').length, 1, 'final page advance stops voice');
});

// -------------------------------------------------------------
// 4. Interrupt voice on and off (single pending advance)
// -------------------------------------------------------------
test('interruptVoice: false waits for voice and collapses multiple advances', async () => {
  const { player, clock, voice } = setup({ settings: { interruptVoice: false } });

  player.startTurn('turn-1');
  player.setLineAudio('turn-1', 0, { status: 'ready', chunks: [{ audio_url: '/a.wav' }] });
  player.pushLine('turn-1', sampleLine0);
  player.pushLine('turn-1', sampleLine1);

  clock.tick(500); // finish reveal
  assert.equal(player.state, 'waiting_advance');

  // Advance clicked while voice is playing
  player.advance();
  assert.equal(player.currentIndex, 0, 'still on line 0 waiting for voice');
  assert.equal(voice.calls.filter(c => c.type === 'stop').length, 0);

  // Repeated clicks do not stack
  player.advance();
  player.advance();
  assert.equal(player.currentIndex, 0);

  // Voice finishes
  voice.resolveLast('ended');
  await flushMicrotasks();

  // Advances to line 1 once!
  assert.equal(player.currentIndex, 1);
});

// -------------------------------------------------------------
// 5. AUTO timing, formula, clamps, modes, and voice-off
// -------------------------------------------------------------
test('AUTO formula clamps between 1200ms and 6000ms', () => {
  // Base 1500, perChar 45
  // Count 0 -> 1500
  // With custom small base: clamp to 1200
  const clock = createFakeClock();
  const player = createLinePlayer({
    clock,
    voice: createFakeVoice(),
    settings: { autoBaseMs: 500, autoPerCharMs: 10, revealMsPerGrapheme: 0 },
  });
  player.setAuto(true);
  player.startTurn('t');
  player.setLineAudio('t', 0, { status: 'skipped' });
  player.pushLine('t', { index: 0, zh: '' }); // count 0 -> 500 clamped to 1200
  // Reveal is instant (revealMsPerGrapheme: 0)
  assert.equal(player.state, 'waiting_advance');
  assert.deepEqual(clock.pendingDueTimes, [1200]);

  // Clamp to 6000
  const player2 = createLinePlayer({
    clock,
    voice: createFakeVoice(),
    settings: { autoBaseMs: 5000, autoPerCharMs: 100, revealMsPerGrapheme: 0 },
  });
  player2.setAuto(true);
  player2.startTurn('t2');
  player2.setLineAudio('t2', 0, { status: 'skipped' });
  player2.pushLine('t2', { index: 0, zh: '一二三四五六七八九十' }); // 5000 + 1000 = 6000
  assert.deepEqual(clock.pendingDueTimes.slice(-1), [6000]);
});

test('AUTO in voice-priority mode waits for voice completion before delay', async () => {
  const { player, clock, voice } = setup({
    settings: { autoMode: 'voice-priority', autoBaseMs: 2000, autoPerCharMs: 0, revealMsPerGrapheme: 0 }
  });
  player.setAuto(true);
  player.startTurn('t');
  player.setLineAudio('t', 0, { status: 'ready', chunks: [{ audio_url: '/a.wav' }] });
  player.pushLine('t', sampleLine0);
  player.pushLine('t', sampleLine1);

  // Reveal is instant
  assert.equal(player.state, 'waiting_advance');
  // Since voice is playing, AUTO timer must not be set yet!
  assert.equal(clock.activeTimerCount, 0);

  // At 1000ms, voice ends
  clock.tick(1000);
  voice.resolveLast('ended');
  await flushMicrotasks();

  // Now AUTO timer is set for 2000ms
  assert.equal(clock.activeTimerCount, 1);
  assert.deepEqual(clock.pendingDueTimes, [3000]); // 1000 + 2000

  // Tick 2000ms -> advances to line 1
  clock.tick(2000);
  assert.equal(player.currentIndex, 1);
});

test('AUTO in normal mode starts delay at reveal end, and voice-off case works', () => {
  const { player, clock } = setup({
    settings: { autoMode: 'normal', autoBaseMs: 2000, autoPerCharMs: 0, revealMsPerGrapheme: 0 }
  });
  player.setAuto(true);
  player.startTurn('t');
  player.setLineAudio('t', 0, { status: 'ready', chunks: [{ audio_url: '/a.wav' }] });
  player.pushLine('t', sampleLine0);
  player.pushLine('t', sampleLine1);

  // Reveal is instant, AUTO timer starts immediately
  assert.equal(clock.activeTimerCount, 1);
  assert.deepEqual(clock.pendingDueTimes, [2000]);

  // Voice off case
  const { player: pVoiceOff, clock: cVoiceOff } = setup({
    settings: { voiceEnabled: false, autoBaseMs: 1500, autoPerCharMs: 0, revealMsPerGrapheme: 0 }
  });
  pVoiceOff.setAuto(true);
  pVoiceOff.startTurn('t');
  pVoiceOff.setLineAudio('t', 0, { status: 'ready', chunks: [{ audio_url: '/a.wav' }] });
  pVoiceOff.pushLine('t', sampleLine0);
  assert.equal(cVoiceOff.activeTimerCount, 1);
});

// -------------------------------------------------------------
// 6. SKIP pacing and voice suppression
// -------------------------------------------------------------
test('SKIP advances every skipIntervalMs, suppresses voice, and stops on input', () => {
  const { player, clock, voice, render } = setup({ settings: { skipIntervalMs: 80 } });
  player.startTurn('turn-1');
  player.setLineAudio('turn-1', 0, { status: 'ready', chunks: [{ audio_url: '/a.wav' }] });
  player.setLineAudio('turn-1', 1, { status: 'ready', chunks: [{ audio_url: '/b.wav' }] });
  player.pushLine('turn-1', sampleLine0);
  player.pushLine('turn-1', sampleLine1);
  player.settle('turn-1', { ok: true });

  player.setSkip(true);
  assert.equal(voice.calls.filter(c => c.type === 'stop').length, 1);

  // Line 0 was revealed instantly, skip timer scheduled for 80ms
  assert.equal(player.currentIndex, 0);
  clock.tick(80);

  // Advanced to line 1
  assert.equal(player.currentIndex, 1);
  assert.ok(render.calls.some(([name, line, opt]) => name === 'activate' && line === sampleLine1 && opt.skipping === true));

  clock.tick(80);
  // Settled -> enters input and turns off skip
  assert.equal(player.state, 'input');
  assert.equal(player.skip, false);
});

// -------------------------------------------------------------
// 7. Hide/unhide and suspend/resume
// -------------------------------------------------------------
test('hide/unhide pauses AUTO and restores full D without advancing', () => {
  const { player, clock } = setup({ settings: { autoBaseMs: 2000, autoPerCharMs: 0, revealMsPerGrapheme: 0 } });
  player.setAuto(true);
  player.startTurn('t');
  player.setLineAudio('t', 0, { status: 'skipped' });
  player.pushLine('t', sampleLine0);

  assert.equal(clock.activeTimerCount, 1);
  clock.tick(1000); // 1000ms elapsed of 2000ms

  player.hide();
  assert.equal(player.hidden, true);
  assert.equal(clock.activeTimerCount, 0, 'AUTO paused while hidden');

  player.unhide();
  assert.equal(player.hidden, false);
  // Restores full D (2000ms from current time 1000 -> 3000)
  assert.deepEqual(clock.pendingDueTimes, [3000]);
});

test('suspend/resume preserves line/page, stops voice, and restores full D', () => {
  const { player, clock, voice } = setup({ settings: { autoBaseMs: 2000, autoPerCharMs: 0, revealMsPerGrapheme: 0 } });
  player.setAuto(true);
  player.startTurn('t');
  player.setLineAudio('t', 0, { status: 'ready', chunks: [{ audio_url: '/a.wav' }] });
  player.pushLine('t', sampleLine0);

  player.suspend();
  assert.equal(player.state, 'suspended');
  assert.equal(voice.calls.filter(c => c.type === 'stop').length, 1);
  assert.equal(clock.activeTimerCount, 0);

  player.resume();
  assert.equal(player.state, 'waiting_advance');
  assert.equal(player.currentIndex, 0);
  assert.equal(clock.activeTimerCount, 1);
});

// -------------------------------------------------------------
// 8. Player edit mid-reply
// -------------------------------------------------------------
test('beginPlayerEdit cancels auto/skip, logs remaining lines, and routes future lines to logOnly', () => {
  const { player, render, events } = setup();
  player.setAuto(true);
  player.setSkip(true);
  player.startTurn('turn-1');
  player.pushLine('turn-1', sampleLine0);
  player.pushLine('turn-1', sampleLine1);

  render.calls.length = 0;
  events.length = 0;

  player.beginPlayerEdit();
  assert.equal(player.auto, false);
  assert.equal(player.skip, false);
  assert.equal(player.state, 'input');

  // sampleLine1 was queued and now logged via logOnly
  assert.deepEqual(render.calls.find(([name]) => name === 'logOnly'), ['logOnly', sampleLine1]);
  assert.ok(events.some(([ev, payload]) => ev === 'logged' && payload === sampleLine1));
  assert.ok(events.some(([ev, payload]) => ev === 'input' && payload.canSend === false));

  // Later line pushed for this turn
  const sampleLine2 = { index: 2, zh: '第三句', ja: '三つ目', emotion: 'cool', act: {} };
  player.pushLine('turn-1', sampleLine2);
  assert.deepEqual(render.calls.at(-1), ['logOnly', sampleLine2]);

  // Settle arrives
  player.settle('turn-1', { ok: true });
  assert.deepEqual(render.calls.at(-1), ['enterInput', { canSend: true }]);
  assert.ok(events.some(([ev, payload]) => ev === 'input' && payload.canSend === true));
});

// -------------------------------------------------------------
// 9. Error and cancellation
// -------------------------------------------------------------
test('error displays error and dismissError transitions to input', () => {
  const { player, render } = setup();
  player.startTurn('turn-1');
  player.pushLine('turn-1', sampleLine0);

  player.settle('turn-1', { ok: false, kind: 'error', error: 'Network fail' });
  assert.equal(player.state, 'error');
  assert.deepEqual(render.calls.at(-1), ['showError', 'Network fail']);

  player.dismissError();
  assert.equal(player.state, 'input');
});

test('cancellation transitions to input with canSend: true', () => {
  const { player } = setup();
  player.startTurn('turn-1');
  player.pushLine('turn-1', sampleLine0);

  player.settle('turn-1', { ok: false, kind: 'cancelled' });
  assert.equal(player.state, 'input');
  assert.equal(player.canSend, true);
});

// -------------------------------------------------------------
// 10. Stale turnId
// -------------------------------------------------------------
test('stale turnId calls are completely ignored', () => {
  const { player } = setup();
  player.startTurn('turn-1');
  player.startTurn('turn-2');

  // Calls with turn-1
  player.pushLine('turn-1', sampleLine0);
  assert.equal(player.currentIndex, null);

  player.setLineAudio('turn-1', 0, { status: 'ready', chunks: [] });
  player.settle('turn-1', { ok: true });
  assert.equal(player.state, 'waiting_line');
});

// -------------------------------------------------------------
// 11. reducedMotion
// -------------------------------------------------------------
test('reducedMotion makes reveals instant and passes reducedMotion to activate', () => {
  const { player, render } = setup({ settings: { reducedMotion: true } });
  player.startTurn('turn-1');
  render.calls.length = 0; // Clear startTurn calls

  player.setLineAudio('turn-1', 0, { status: 'ready', chunks: [] });
  player.pushLine('turn-1', sampleLine0);

  assert.equal(player.state, 'waiting_advance');
  assert.deepEqual(render.calls[1], ['activate', sampleLine0, { skipping: false, reducedMotion: true }]);
  assert.deepEqual(render.calls[3], ['reveal', sampleLine0.zh, { durationMs: 0, pageIndex: 0, pageCount: 1 }]);
});

// -------------------------------------------------------------
// 12. primary text selection (ja vs zh)
// -------------------------------------------------------------
test('settings.primary = ja reveals Japanese text', () => {
  const { player, render } = setup({ settings: { primary: 'ja', revealMsPerGrapheme: 0 } });
  player.startTurn('t');
  player.setLineAudio('t', 0, { status: 'skipped' });
  player.pushLine('t', sampleLine0);

  assert.equal(render.calls.find(([name]) => name === 'reveal')?.[1], sampleLine0.ja);
});

// -------------------------------------------------------------
// 13. Regression: Fix 1 - Stuck after resume()
// -------------------------------------------------------------
test('Regression Fix 1: resume from waiting_line activates line or enters settled input', () => {
  // Case A: line arrives while suspended in waiting_line
  const appA = setup();
  appA.player.startTurn('t');
  assert.equal(appA.player.state, 'waiting_line');

  appA.player.suspend();
  assert.equal(appA.player.state, 'suspended');

  // Line arrives while suspended
  appA.player.setLineAudio('t', 0, { status: 'skipped' });
  appA.player.pushLine('t', sampleLine0);

  // Resume should re-evaluate and activate line 0 immediately
  appA.player.resume();
  assert.equal(appA.player.currentIndex, 0);
  assert.equal(appA.player.state, 'revealing');

  // Case B: settle arrives while suspended in waiting_line
  const appB = setup();
  appB.player.startTurn('t');
  appB.player.suspend();
  appB.player.settle('t', { ok: true });

  appB.player.resume();
  assert.equal(appB.player.state, 'input');
  assert.equal(appB.player.canSend, true);
});

// -------------------------------------------------------------
// 14. Regression: Fix 2 - Partial failure must still play
// -------------------------------------------------------------
test('Regression Fix 2: failed status with non-empty chunks plays available chunks', () => {
  const partialChunks = [{ index: 0, line: 0, audio_url: '/partial.wav', sentence: 'こんにちは' }];

  // Case A: on activation
  const appA = setup();
  appA.player.startTurn('t');
  appA.player.setLineAudio('t', 0, { status: 'failed', chunks: partialChunks });
  appA.player.pushLine('t', sampleLine0);

  assert.equal(appA.player.currentIndex, 0);
  assert.equal(appA.voice.calls.length, 1);
  assert.equal(appA.voice.calls[0].type, 'play');
  assert.deepEqual(appA.voice.calls[0].chunks, partialChunks);

  // Case B: late audio arrival during waiting_audio
  const appB = setup();
  appB.player.startTurn('t');
  appB.player.pushLine('t', sampleLine0);
  assert.equal(appB.player.state, 'waiting_audio');

  appB.player.setLineAudio('t', 0, { status: 'failed', chunks: partialChunks });
  assert.equal(appB.voice.calls.length, 1);
  assert.equal(appB.voice.calls[0].type, 'play');

  // Case C: failed with empty chunks reveals without voice
  const appC = setup();
  appC.player.startTurn('t');
  appC.player.setLineAudio('t', 0, { status: 'failed', chunks: [] });
  appC.player.pushLine('t', sampleLine0);
  assert.equal(appC.voice.calls.length, 0);
});

// -------------------------------------------------------------
// 15. Regression: Fix 3 - Stale voice resolutions
// -------------------------------------------------------------
test('Regression Fix 3: voice resolution from previous turn does not end current turn voice', async () => {
  let resolveVoiceA;
  let resolveVoiceB;
  let playCount = 0;
  const customVoice = {
    play(lineIndex, chunks) {
      playCount++;
      if (playCount === 1) {
        return new Promise(r => { resolveVoiceA = r; });
      } else {
        return new Promise(r => { resolveVoiceB = r; });
      }
    },
    stop() {} // does not synchronously resolve promises
  };

  const { player, clock } = setup({
    voice: customVoice,
    settings: { autoMode: 'voice-priority', autoBaseMs: 2000, autoPerCharMs: 0, revealMsPerGrapheme: 0 }
  });
  player.setAuto(true);

  // Turn A starts and plays line 0
  player.startTurn('turn-A');
  player.setLineAudio('turn-A', 0, { status: 'ready', chunks: [{ audio_url: '/a.wav' }] });
  player.pushLine('turn-A', sampleLine0);
  assert.equal(playCount, 1);

  // Turn B starts before Turn A voice resolves
  player.startTurn('turn-B');
  player.setLineAudio('turn-B', 0, { status: 'ready', chunks: [{ audio_url: '/b.wav' }] });
  player.pushLine('turn-B', sampleLine0);
  assert.equal(playCount, 2);

  // Reveal is instant for Turn B, waiting_advance
  assert.equal(player.state, 'waiting_advance');
  assert.equal(clock.activeTimerCount, 0, 'Turn B voice is playing, AUTO timer not set');

  // Now Turn A's voice resolves late
  resolveVoiceA('ended');
  await flushMicrotasks();

  // Turn B's voice must STILL be considered playing!
  // It must NOT have ended B's voice or started B's AUTO timer!
  assert.equal(clock.activeTimerCount, 0, 'Turn A late resolution must not start Turn B AUTO timer');

  // Now Turn B's voice resolves
  resolveVoiceB('ended');
  await flushMicrotasks();

  // Now Turn B's AUTO timer starts!
  assert.equal(clock.activeTimerCount, 1, 'Turn B AUTO timer started after B voice resolved');
});

// -------------------------------------------------------------
// 16. Regression: Fix 4 - pendingAdvance and suspension
// -------------------------------------------------------------
test('Regression Fix 4: suspend explicitly clears pendingAdvance without skipping lines or getting stuck', async () => {
  const { player, clock, voice } = setup({ settings: { interruptVoice: false, revealMsPerGrapheme: 0 } });
  player.startTurn('t');
  player.setLineAudio('t', 0, { status: 'ready', chunks: [{ audio_url: '/a.wav' }] });
  player.pushLine('t', sampleLine0);
  player.pushLine('t', sampleLine1);

  // Line 0 is waiting_advance with voice playing
  assert.equal(player.state, 'waiting_advance');
  assert.equal(voice.pending.length, 1);
  const line0Voice = voice.pending[0];

  // User clicks advance -> pendingAdvance is recorded
  player.advance();
  assert.equal(player.currentIndex, 0);

  // Overlay opens -> suspend
  player.suspend();
  assert.equal(player.state, 'suspended');

  // The interrupted voice resolves while suspended
  line0Voice.resolve('interrupted');
  await flushMicrotasks();

  // Overlay closes -> resume
  player.resume();
  assert.equal(player.state, 'waiting_advance');
  assert.equal(player.currentIndex, 0, 'still on line 0, line 1 not skipped');

  // User clicks advance now -> advances cleanly to line 1
  player.advance();
  assert.equal(player.currentIndex, 1, 'advanced to line 1 successfully, not stuck');
});


test('late audio never plays while suspended, in player input, or in error', async () => {
  for (const enterQuietState of [
    (player) => player.suspend(),
    (player) => player.beginPlayerEdit(),
    (player) => player.settle('t1', { ok: false, kind: 'error', error: 'boom' }),
  ]) {
    const { clock, voice, player } = setup();
    player.startTurn('t1');
    player.pushLine('t1', sampleLine0);
    clock.tick(DEFAULT_SETTINGS.audioWaitMs); // reveal without audio
    enterQuietState(player);
    player.setLineAudio('t1', 0, { status: 'ready', chunks: [{ index: 0, line: 0, audio_url: '/a.wav', sentence: 'x' }] });
    await flushMicrotasks();
    assert.equal(voice.calls.filter((c) => c.type === 'play').length, 0, `no voice in ${player.state}`);
  }
});

test('repaginate recomputes pages for current line with new primary setting without replaying voice', async () => {
  const { clock, voice, render, player } = setup({
    paginate: (line, primaryText) => {
      if (primaryText === line.ja) return ['ja_p1', 'ja_p2'];
      return ['zh_p1'];
    },
  });
  player.startTurn('t1');
  player.pushLine('t1', { index: 0, zh: '你好', ja: 'こんにちは世界' });
  player.setLineAudio('t1', 0, { status: 'ready', chunks: [{ index: 0, line: 0, audio_url: '/a.wav', sentence: 'x' }] });
  await flushMicrotasks();
  assert.equal(voice.calls.length, 1);

  // Finish reveal of page 0
  clock.tick(1000);
  assert.equal(player.page, 0);

  // Switch primary to ja and repaginate
  player.setSettings({ primary: 'ja' });
  player.repaginate();

  // Voice was NOT replayed
  assert.equal(voice.calls.length, 1);

  // Advance now moves to ja_p2 (page 1) rather than skipping to next line
  player.advance();
  assert.equal(player.page, 1);
  const reveals = render.calls.filter((c) => c[0] === 'reveal');
  assert.equal(reveals[reveals.length - 1][1], 'ja_p2');
});

test('isPresenting is false unless the stage is showing or waiting for a line', () => {
  const { player } = setup({ settings: { voiceEnabled: false } });
  assert.equal(player.isPresenting(), false, 'idle');

  player.startTurn('t');
  assert.equal(player.state, 'waiting_line');
  assert.equal(player.isPresenting(), true, 'waiting_line');
  player.suspend('overlay');
  assert.equal(player.state, 'suspended');
  assert.equal(player.isPresenting(), true, 'suspended from waiting_line');
  player.resume('overlay');

  player.pushLine('t', sampleLine0);
  assert.equal(player.state, 'revealing');
  assert.equal(player.isPresenting(), true, 'revealing');
  player.suspend('overlay');
  assert.equal(player.isPresenting(), true, 'suspended from revealing');
  player.resume('overlay');

  assert.equal(player.state, 'waiting_advance');
  assert.equal(player.isPresenting(), true, 'waiting_advance');
  player.suspend('overlay');
  assert.equal(player.isPresenting(), true, 'suspended from waiting_advance');
  player.resume('overlay');

  player.beginPlayerEdit();
  assert.equal(player.state, 'input');
  assert.equal(player.isPresenting(), false, 'input');
  player.suspend('overlay');
  assert.equal(player.state, 'suspended');
  assert.equal(player.isPresenting(), false, 'suspended from input');
  player.resume('overlay');

  player.settle('t', { ok: false, kind: 'error', error: 'boom' });
  assert.equal(player.state, 'error');
  assert.equal(player.isPresenting(), false, 'error');
});

test('repaginate preserves the raw text offset when viewport page widths change', () => {
  let wide = false;
  const { player, render } = setup({ settings: { voiceEnabled: false, revealMsPerGrapheme: 0 },
    paginate: () => wide ? ['abcd', 'efgh', 'ijkl'] : ['ab', 'cd', 'ef', 'gh', 'ij', 'kl'] });
  player.startTurn('resize'); player.pushLine('resize', { index: 0, zh: 'abcdefghijkl', ja: '' });
  player.advance(); player.advance(); player.advance();
  assert.equal(player.page, 3); // Offset 6, at "gh".
  wide = true; player.repaginate(); assert.equal(player.page, 1, 'new page contains offset 6');
  player.advance();
  assert.equal(render.calls.filter(call => call[0] === 'reveal').at(-1)[1], 'ijkl');
  player.reset();
});

test('voice.play returning failed or throwing reveals line and lets voice-priority AUTO continue', async () => {
  // Case A: voice.play() resolves to 'failed'
  const { player, clock, voice } = setup({
    settings: { autoMode: 'voice-priority', autoBaseMs: 2000, autoPerCharMs: 0, revealMsPerGrapheme: 0 },
  });
  player.setAuto(true);
  player.startTurn('t-fail');
  player.setLineAudio('t-fail', 0, { status: 'ready', chunks: [{ audio_url: '/a.wav' }] });
  player.pushLine('t-fail', sampleLine0);
  player.pushLine('t-fail', sampleLine1);

  assert.equal(player.state, 'waiting_advance');
  assert.equal(clock.activeTimerCount, 0);

  voice.resolveLast('failed');
  await flushMicrotasks();

  assert.equal(clock.activeTimerCount, 1, 'AUTO timer starts after voice.play returns failed');
  clock.tick(2000);
  assert.equal(player.currentIndex, 1, 'AUTO advances to next line after failed voice');

  // Case B: voice.play() rejects asynchronously
  const rejectingVoice = {
    play() {
      return Promise.reject(new Error('playback failed'));
    },
    stop() {},
  };
  const { player: playerReject, clock: clockReject } = setup({
    voice: rejectingVoice,
    settings: { autoMode: 'voice-priority', autoBaseMs: 2000, autoPerCharMs: 0, revealMsPerGrapheme: 0 },
  });
  playerReject.setAuto(true);
  playerReject.startTurn('t-reject');
  playerReject.setLineAudio('t-reject', 0, { status: 'ready', chunks: [{ audio_url: '/a.wav' }] });
  playerReject.pushLine('t-reject', sampleLine0);
  playerReject.pushLine('t-reject', sampleLine1);

  assert.equal(playerReject.state, 'waiting_advance');
  await flushMicrotasks();

  assert.equal(clockReject.activeTimerCount, 1, 'AUTO timer starts after voice.play rejects');
  clockReject.tick(2000);
  assert.equal(playerReject.currentIndex, 1, 'AUTO advances to next line after rejected voice');

  // Case C: voice.play() throws synchronously
  const throwingVoice = {
    play() {
      throw new Error('sync playback error');
    },
    stop() {},
  };
  const { player: playerThrow, clock: clockThrow } = setup({
    voice: throwingVoice,
    settings: { autoMode: 'voice-priority', autoBaseMs: 2000, autoPerCharMs: 0, revealMsPerGrapheme: 0 },
  });
  playerThrow.setAuto(true);
  playerThrow.startTurn('t-throw');
  playerThrow.setLineAudio('t-throw', 0, { status: 'ready', chunks: [{ audio_url: '/a.wav' }] });
  playerThrow.pushLine('t-throw', sampleLine0);
  playerThrow.pushLine('t-throw', sampleLine1);

  assert.equal(playerThrow.state, 'waiting_advance');
  await flushMicrotasks();

  assert.equal(clockThrow.activeTimerCount, 1, 'AUTO timer starts after voice.play throws');
  clockThrow.tick(2000);
  assert.equal(playerThrow.currentIndex, 1, 'AUTO advances to next line after thrown voice');
});
