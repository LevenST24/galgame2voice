import assert from 'node:assert/strict';
import { test } from 'node:test';

// Hand-rolled DOM fake
class FakeElement {
  constructor(tag = 'div') {
    this.tag = tag;
    this.children = [];
    this.attributes = {};
    this.listeners = {};
    this.textContent = '';
    this.value = '';
    this._html = '';
    this.disabled = false;
    this.dataset = {};
    this.parentNode = null;

    const classes = new Set();
    this.classList = {
      add: (...names) => names.forEach(n => classes.add(n)),
      remove: (...names) => names.forEach(n => classes.delete(n)),
      toggle: (n, on) => ((on ?? !classes.has(n)) ? classes.add(n) : classes.delete(n)),
      contains: n => classes.has(n),
    };
    Object.defineProperty(this, 'className', {
      get: () => [...classes].join(' '),
      set: v => {
        classes.clear();
        String(v).split(/\s+/).filter(Boolean).forEach(n => classes.add(n));
      },
    });
    this.style = {
      values: {},
      setProperty: (k, v) => { this.style.values[k] = v; },
      getPropertyValue: (k) => this.style.values[k] || '',
    };
  }

  set innerHTML(value) {
    this._html = value;
    this.children = [];
  }
  get innerHTML() {
    return this._html;
  }

  setAttribute(name, value) {
    this.attributes[name] = String(value);
  }
  getAttribute(name) {
    return this.attributes[name] ?? null;
  }

  appendChild(child) {
    child.parentNode = this;
    this.children.push(child);
    return child;
  }
  removeChild(child) {
    const idx = this.children.indexOf(child);
    if (idx !== -1) {
      this.children.splice(idx, 1);
      child.parentNode = null;
    }
    return child;
  }

  contains(child) {
    if (child === this) return true;
    for (const c of this.children) {
      if (c === child || (c.contains && c.contains(child))) return true;
    }
    return false;
  }

  addEventListener(type, fn) {
    (this.listeners[type] ||= []).push(fn);
  }
  removeEventListener(type, fn) {
    if (!this.listeners[type]) return;
    this.listeners[type] = this.listeners[type].filter(f => f !== fn);
  }

  dispatchEvent(event) {
    const list = this.listeners[event.type] || [];
    for (const fn of list) {
      fn(event);
    }
  }

  focus() {}

  querySelector(selector) {
    return this.findAll(selector)[0] || null;
  }

  findAll(selector) {
    const className = selector.startsWith('.') ? selector.slice(1) : null;
    const tag = !selector.startsWith('.') ? selector.toLowerCase() : null;
    const res = [];
    for (const child of this.children) {
      let match = false;
      if (className && child.classList.contains(className)) match = true;
      if (tag && child.tag === tag) match = true;
      if (match) res.push(child);
      res.push(...child.findAll(selector));
    }
    return res;
  }
}

globalThis.document = {
  createElement: (tag) => new FakeElement(tag),
};

function createFakeClock() {
  let currentTime = 0;
  let nextTimerId = 1;
  const timers = new Map();

  return {
    now: () => currentTime,
    setTimeout: (fn, ms) => {
      const id = nextTimerId++;
      timers.set(id, { fn, dueTime: currentTime + Math.max(0, ms) });
      return id;
    },
    clearTimeout: (id) => {
      timers.delete(id);
    },
    tick: (ms) => {
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
    },
  };
}

const { createLinePlayer } = await import('../frontend/src/adv/line_player.js');
const { createTextbox } = await import('../frontend/src/adv/textbox.js');

test('integration: line player driven by textbox adapter (two lines + player input)', async () => {
  const clock = createFakeClock();
  const voice = createFakeVoice();
  const container = new FakeElement();
  const sentMessages = [];

  let player;

  const textbox = createTextbox(container, {
    clock,
    getSettings: () => ({ primary: 'zh', subtitle: 'ja', revealMsPerGrapheme: 10, playerName: '旅行者' }),
    onAdvance: () => player?.advance(),
    onSend: (text) => { sentMessages.push(text); return true; },
  });

  player = createLinePlayer({
    clock,
    voice,
    render: textbox,
    paginate: (line, text) => textbox.paginate(line, text),
    settings: {
      revealMsPerGrapheme: 10,
      primary: 'zh',
      audioWaitMs: 1500,
      autoBaseMs: 1500,
    },
  });

  const primaryEl = container.querySelector('.adv-primary-text');
  const subEl = container.querySelector('.adv-subtitle-text');
  const nameEl = container.querySelector('.adv-name-text');
  const glyphEl = container.querySelector('.adv-click-glyph');
  const liveEl = container.querySelector('.adv-live-region');
  const inputWrapEl = container.querySelector('.adv-player-input-wrap');
  const textareaEl = container.querySelector('.adv-player-textarea');

  // 1. Turn starts: waiting_line with thinking dots
  player.startTurn('turn-1');
  assert.equal(player.state, 'waiting_line');
  const dotsEl = container.querySelector('.adv-thinking-dots');
  assert.equal(dotsEl.classList.contains('is-hidden'), false);

  // 2. Line 0 arrives: speaker "菲莉"
  const line0 = { index: 0, zh: '你好！', ja: 'こんにちは！', speaker: '菲莉' };
  player.pushLine('turn-1', line0);
  // Audio is not yet ready, so line_player is waiting_audio
  assert.equal(player.state, 'waiting_audio');

  // Audio arrives for line 0
  player.setLineAudio('turn-1', 0, {
    status: 'ready',
    chunks: [{ index: 0, line: 0, audio_url: '/audio0.wav', sentence: 'こんにちは！' }],
  });
  assert.equal(player.state, 'revealing');
  assert.equal(voice.calls.length, 1);
  assert.equal(voice.calls[0].lineIndex, 0);

  // Step clock through typewriter: '「你好！」' (5 graphemes at 10ms = 50ms)
  for (let i = 0; i < 5; i++) {
    clock.tick(10);
  }
  // Reveal completes, state becomes waiting_advance
  assert.equal(player.state, 'waiting_advance');
  assert.equal(primaryEl.textContent, '「你好！」');
  assert.equal(subEl.textContent, '「こんにちは！」');
  assert.equal(nameEl.textContent, '菲莉');
  assert.equal(glyphEl.classList.contains('is-hidden'), false, 'Glyph should be visible on waiting_advance');
  assert.equal(liveEl.textContent, '菲莉：你好！', 'Aria live announced line 0');

  // 3. User advances to next line
  // Line 1 arrives: audio is skipped
  const line1 = { index: 1, zh: '今天是个好日子。', ja: '今日は良い日ですね。', speaker: '菲莉' };
  player.pushLine('turn-1', line1);
  player.setLineAudio('turn-1', 1, { status: 'skipped', chunks: [] });

  player.advance();
  assert.equal(player.state, 'revealing');
  assert.equal(player.currentIndex, 1);
  assert.equal(glyphEl.classList.contains('is-hidden'), true, 'Glyph hidden while revealing line 1');

  // Complete reveal of line 1 immediately
  player.advance(); // Advance during revealing completes page immediately
  assert.equal(player.state, 'waiting_advance');
  assert.equal(primaryEl.textContent, '「今天是个好日子。」');
  assert.equal(subEl.textContent, '「今日は良い日ですね。」');

  // 4. Turn settles
  player.settle('turn-1', { ok: true });
  // Still at waiting_advance for line 1 because user has not advanced past it yet
  assert.equal(player.state, 'waiting_advance');

  // User advances past line 1 -> now turn enters input mode!
  player.advance();
  assert.equal(player.state, 'input');
  assert.equal(inputWrapEl.classList.contains('is-hidden'), false, 'Player input view is visible');
  assert.equal(nameEl.textContent, '旅行者', 'Name plate shows player name');

  // 5. Player types answer and hits Enter
  textareaEl.value = '很高兴认识你！';
  textareaEl.dispatchEvent({ type: 'keydown', key: 'Enter', shiftKey: false, preventDefault() {} });

  assert.deepEqual(sentMessages, ['很高兴认识你！']);
  assert.equal(textareaEl.value, '', 'Textarea cleared after sending');
});

test('integration: audio timeout fallback (1500ms), AUTO progression, SKIP speed, and language swap', async () => {
  const clock = createFakeClock();
  const voice = createFakeVoice();
  const container = new FakeElement();
  let currentSettings = { primary: 'zh', subtitle: 'ja', revealMsPerGrapheme: 10, audioWaitMs: 1500, autoBaseMs: 1000, autoPerCharMs: 20 };

  let player;
  const textbox = createTextbox(container, {
    clock,
    getSettings: () => currentSettings,
    onAdvance: () => player?.advance(),
    onAction: (act, data) => {
      if (act === 'swap_languages') {
        currentSettings = { ...currentSettings, ...data };
      }
    },
    fits: (text) => text.length <= 15,
    fitsSubtitle: (text) => text.length <= 15,
  });

  player = createLinePlayer({
    clock,
    voice,
    render: textbox,
    paginate: (line, text) => textbox.paginate(line, text),
    settings: currentSettings,
  });
  textbox.setPlayer(player);

  const primaryEl = container.querySelector('.adv-primary-text');
  const subEl = container.querySelector('.adv-subtitle-text');

  // --- 1. Audio timeout fallback (1500ms) ---
  player.setSettings({ autoMode: 'normal' });
  player.startTurn('t-advanced');
  const line0 = { index: 0, zh: '第一句等待语音超时', ja: '音声タイムアウト', speaker: '艾尔' };
  player.pushLine('t-advanced', line0);
  assert.equal(player.state, 'waiting_audio', 'Should wait for audio when status is unknown');

  // Tick clock 1500ms (audioWaitMs)
  clock.tick(1500);
  // After 1500ms timeout, state should transition to revealing
  assert.equal(player.state, 'revealing', 'Should start revealing after audio wait timeout');
  // Finish typewriter reveal
  clock.tick(500);
  assert.equal(player.state, 'waiting_advance');
  assert.equal(primaryEl.textContent, '「第一句等待语音超时」');

  // --- 2. AUTO progression ---
  player.setAuto(true);
  const line1 = { index: 1, zh: '自动播放台词', ja: '自動送り', speaker: '艾尔' };
  player.pushLine('t-advanced', line1);
  player.setLineAudio('t-advanced', 1, { status: 'skipped', chunks: [] });

  // Advance from line 0 to line 1
  player.advance();
  assert.equal(player.currentIndex, 1);
  assert.equal(player.state, 'revealing');

  // Finish reveal of line 1
  clock.tick(200);
  assert.equal(player.state, 'waiting_advance');

  // AUTO timer is now running; tick the required duration
  // Duration is autoBaseMs (1000) + autoPerCharMs * count
  clock.tick(2000);
  // Should have automatically advanced!
  assert.equal(player.state, 'waiting_line', 'AUTO should automatically advance past line 1');
  player.setAuto(false);

  // --- 3. SKIP mode ---
  player.setSkip(true);
  const line2 = { index: 2, zh: '快进模式测试台词', ja: 'スキップテスト', speaker: '艾尔' };
  const line3 = { index: 3, zh: '第二句快进台词', ja: 'スキップ２', speaker: '艾尔' };
  player.pushLine('t-advanced', line2);
  player.setLineAudio('t-advanced', 2, { status: 'skipped', chunks: [] });
  player.pushLine('t-advanced', line3);
  player.setLineAudio('t-advanced', 3, { status: 'skipped', chunks: [] });

  // In skip mode, line 2 reveals instantly and schedules skip advance at 80ms
  assert.equal(player.currentIndex, 2);
  assert.equal(player.state, 'waiting_advance');
  clock.tick(80);
  // Advances to line 3 at 80ms interval
  assert.equal(player.currentIndex, 3);
  player.setSkip(false);

  // --- 4. Language swap integration ---
  const bilingualLine = {
    index: 4,
    zh: '短中文第一句。第二句。',
    ja: 'とても長い日本語の文章です。分割が必要です。',
    speaker: '艾尔',
  };
  player.pushLine('t-advanced', bilingualLine);
  player.setLineAudio('t-advanced', 4, { status: 'ready', chunks: [{ index: 0, line: 4, audio_url: '/v4.wav' }] });
  player.advance();
  assert.equal(player.currentIndex, 4);

  // Complete initial reveal
  clock.tick(500);
  assert.equal(player.state, 'waiting_advance');
  assert.equal(voice.calls.length, 1);

  // Trigger language swap
  textbox.swapLanguages();
  assert.equal(player.settings.primary, 'ja', 'Player primary setting updated to ja');
  assert.equal(primaryEl.textContent, '「とても長い日本語の文章です。」');
  assert.equal(subEl.textContent, '「短中文第一句。」');
  // Voice was NOT replayed
  assert.equal(voice.calls.length, 1);

  // Next advance moves to page 1 of Japanese
  player.advance();
  assert.equal(player.page, 1, 'Advance moved to next page of newly swapped language');
  clock.tick(500);
  assert.equal(primaryEl.textContent, '「分割が必要です。」');
  assert.equal(subEl.textContent, '「第二句。」');
});

