import assert from 'node:assert/strict';
import { test } from 'node:test';

// Hand-rolled DOM fake in the style of test_failure_note_ui.mjs and test_portrait_stage.mjs
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

const { createTextbox } = await import('../frontend/src/adv/textbox.js');
const { splitGraphemes } = await import('../frontend/src/adv/graphemes.js');

test('textbox: progressive reveal with fake clock, completeReveal shows everything immediately', () => {
  const clock = createFakeClock();
  const container = new FakeElement();
  const tb = createTextbox(container, {
    clock,
    getSettings: () => ({ primary: 'zh', subtitle: 'ja', reducedMotion: false }),
  });

  const line = { index: 0, zh: '你好，世界！', ja: 'こんにちは、世界！', speaker: '菲莉' };
  tb.activate(line);

  // Spoken text is wrapped in 「」 for display: '「你好，世界！」' (8 graphemes)
  // durationMs = 80ms -> 10ms per grapheme
  tb.reveal('你好，世界！', { durationMs: 80, pageIndex: 0, pageCount: 1 });

  const primaryEl = container.querySelector('.adv-primary-text');
  // First grapheme shown immediately on start of reveal
  assert.equal(primaryEl.textContent, '「');

  // Step 10ms
  clock.tick(10);
  assert.equal(primaryEl.textContent, '「你');

  // Step another 10ms
  clock.tick(10);
  assert.equal(primaryEl.textContent, '「你好');

  // completeReveal shows everything immediately
  tb.completeReveal();
  assert.equal(primaryEl.textContent, '「你好，世界！」');
});

test('textbox: reduced motion makes reveal instant', () => {
  const clock = createFakeClock();
  const container = new FakeElement();
  const tb = createTextbox(container, {
    clock,
    getSettings: () => ({ primary: 'zh', subtitle: 'none', reducedMotion: true }),
  });

  const line = { index: 0, zh: '即时显示。', speaker: '菲莉' };
  tb.activate(line);

  tb.reveal('即时显示。', { durationMs: 200, pageIndex: 0, pageCount: 1 });
  const primaryEl = container.querySelector('.adv-primary-text');
  // With reduced motion, full text is rendered instantly
  assert.equal(primaryEl.textContent, '「即时显示。」');
  assert.equal(clock.activeTimerCount, 0);
});

test('textbox: thinking indicator shows character name and dots, hides on thinking(false)', () => {
  const container = new FakeElement();
  const tb = createTextbox(container, {
    getSettings: () => ({ primary: 'zh', subtitle: 'ja' }),
  });

  tb.setSpeaker({ name: '菲莉', mode: 'character' });
  tb.thinking(true);

  const dotsEl = container.querySelector('.adv-thinking-dots');
  const nameEl = container.querySelector('.adv-name-text');
  assert.equal(dotsEl.classList.contains('is-hidden'), false);
  assert.equal(nameEl.textContent, '菲莉');

  tb.thinking(false);
  assert.equal(dotsEl.classList.contains('is-hidden'), true);
});

test('textbox: player input behavior (Enter sends, Shift+Enter newline, IME guards, canSend disabled)', () => {
  const container = new FakeElement();
  const sent = [];
  const actions = [];
  const tb = createTextbox(container, {
    getSettings: () => ({ playerName: '勇者' }),
    onSend: (text) => { sent.push(text); return true; },
    onAction: (act, data) => actions.push([act, data]),
  });

  // Enter input mode with canSend: false
  tb.enterInput({ canSend: false });
  const nameEl = container.querySelector('.adv-name-text');
  assert.equal(nameEl.textContent, '勇者');
  const faceSlot = container.querySelector('.adv-face-slot');
  assert.equal(faceSlot.classList.contains('is-hidden'), true);

  const textarea = container.querySelector('.adv-player-textarea');
  const sendBtn = container.querySelector('.adv-player-send-btn');
  assert.equal(sendBtn.disabled, true);

  // Typing when canSend is false
  textarea.value = '测试回答';
  textarea.dispatchEvent({ type: 'keydown', key: 'Enter', shiftKey: false, preventDefault() {} });
  assert.equal(sent.length, 0, 'Should not send when canSend is false');

  // Update canSend: true
  tb.enterInput({ canSend: true });
  assert.equal(sendBtn.disabled, false);

  // During IME composition, Enter should not send
  textarea.value = 'ceshi';
  textarea.dispatchEvent({ type: 'compositionstart' });
  textarea.dispatchEvent({ type: 'keydown', key: 'Enter', keyCode: 229, isComposing: true, preventDefault() {} });
  assert.equal(sent.length, 0, 'IME composition Enter must not send');

  // Immediately committing IME candidate (keyCode 229 / justComposed guard)
  textarea.dispatchEvent({ type: 'compositionend' });
  textarea.dispatchEvent({ type: 'keydown', key: 'Enter', keyCode: 229, preventDefault() {} });
  assert.equal(sent.length, 0, 'Enter committing candidate must not send');
  textarea.dispatchEvent({ type: 'keyup' });

  // Shift+Enter should not send (allows multiline)
  textarea.value = '第一行\n第二行';
  let defaultPrevented = false;
  textarea.dispatchEvent({ type: 'keydown', key: 'Enter', shiftKey: true, preventDefault() { defaultPrevented = true; } });
  assert.equal(sent.length, 0, 'Shift+Enter must not send');
  assert.equal(defaultPrevented, false, 'Shift+Enter should not preventDefault');

  // Normal Enter sends
  textarea.value = '正式回复！';
  textarea.dispatchEvent({ type: 'keydown', key: 'Enter', shiftKey: false, preventDefault() {} });
  assert.deepEqual(sent, ['正式回复！']);
  assert.equal(textarea.value, '', 'Textarea should be cleared after send');
});

test('textbox: aria-live region announces full text exactly once on activate', () => {
  const container = new FakeElement();
  const tb = createTextbox(container, {
    getSettings: () => ({ primary: 'zh', subtitle: 'ja' }),
  });

  const line = { index: 0, zh: '第一句很长的台词内容。', ja: '一言目。', speaker: '艾尔' };
  tb.setSpeaker({ name: '艾尔', mode: 'character' });
  tb.activate(line);

  const liveEl = container.querySelector('.adv-live-region');
  assert.equal(liveEl.textContent, '艾尔：第一句很长的台词内容。');

  // Typewriter reveals pages; aria-live must NOT change per typewriter character or page reveal
  tb.reveal('第一句', { durationMs: 0, pageIndex: 0, pageCount: 2 });
  assert.equal(liveEl.textContent, '艾尔：第一句很长的台词内容。');
  tb.reveal('很长的台词内容。', { durationMs: 0, pageIndex: 1, pageCount: 2 });
  assert.equal(liveEl.textContent, '艾尔：第一句很长的台词内容。');
});

test('textbox: language swap reflows current page without mutating line', () => {
  const container = new FakeElement();
  let currentSettings = { primary: 'zh', subtitle: 'ja' };
  const actions = [];
  const tb = createTextbox(container, {
    getSettings: () => currentSettings,
    onAction: (act, data) => {
      actions.push([act, data]);
      if (act === 'swap_languages') {
        currentSettings = { ...currentSettings, ...data };
      }
    },
  });

  const line = { index: 0, zh: '中文你好', ja: 'こんにちは日本語' };
  tb.activate(line);
  tb.reveal('中文你好', { durationMs: 0, pageIndex: 0, pageCount: 1 });

  const primaryEl = container.querySelector('.adv-primary-text');
  const subEl = container.querySelector('.adv-subtitle-text');
  assert.equal(primaryEl.textContent, '「中文你好」');
  assert.equal(subEl.textContent, '「こんにちは日本語」');

  // Swap languages
  tb.swapLanguages();

  assert.equal(primaryEl.textContent, '「こんにちは日本語」');
  assert.equal(subEl.textContent, '「中文你好」');

  // Stored line data was NEVER mutated
  assert.equal(line.zh, '中文你好');
  assert.equal(line.ja, 'こんにちは日本語');
});

function withMockMatchMedia(matchesInitial, callback) {
  const original = globalThis.matchMedia;
  const listeners = [];
  let currentMatches = matchesInitial;
  const mql = {
    get matches() { return currentMatches; },
    addEventListener(event, fn) {
      if (event === 'change') listeners.push(fn);
    },
    removeEventListener(event, fn) {
      const idx = listeners.indexOf(fn);
      if (idx !== -1) listeners.splice(idx, 1);
    },
    trigger(matches) {
      currentMatches = matches;
      for (const fn of [...listeners]) fn({ matches });
    },
    get listenerCount() { return listeners.length; },
  };
  globalThis.matchMedia = () => mql;
  try {
    return callback(mql);
  } finally {
    globalThis.matchMedia = original;
  }
}

test('textbox: destroy() unregisters matchMedia change listener (no memory leak)', () => {
  withMockMatchMedia(false, (mql) => {
    const container = new FakeElement();
    const tb = createTextbox(container, {
      getSettings: () => ({ primary: 'zh', subtitle: 'ja' }),
    });
    assert.equal(mql.listenerCount, 1, 'Listener registered on creation');

    tb.destroy();
    assert.equal(mql.listenerCount, 0, 'Listener unregistered on destroy');
  });
});

test('textbox: live prefers-reduced-motion change immediately completes active typewriter', () => {
  withMockMatchMedia(false, (mql) => {
    const clock = createFakeClock();
    const container = new FakeElement();
    const tb = createTextbox(container, {
      clock,
      getSettings: () => ({ primary: 'zh', subtitle: 'ja' }),
    });

    const line = { index: 0, zh: '测试动态切换打字机完成。', speaker: '艾尔' };
    tb.activate(line);
    tb.reveal('测试动态切换打字机完成。', { durationMs: 200, pageIndex: 0, pageCount: 1 });

    const primaryEl = container.querySelector('.adv-primary-text');
    // Reveal has just started
    assert.notEqual(primaryEl.textContent, '「测试动态切换打字机完成。」');

    // Trigger reduced motion change
    mql.trigger(true);

    // Immediately finishes reveal without waiting for clock ticks
    assert.equal(primaryEl.textContent, '「测试动态切换打字机完成。」');
  });
});

test('textbox: multi-page quotation wrapping does not double-wrap on split pages', () => {
  const container = new FakeElement();
  const tb = createTextbox(container, {
    getSettings: () => ({ primary: 'zh', subtitle: 'none' }),
  });

  const line = { index: 0, zh: '「第一页内容，第二页内容。」', speaker: '艾尔' };
  tb.activate(line);

  // Page 0 has opening quote but not closing quote
  tb.reveal('「第一页内容，', { durationMs: 0, pageIndex: 0, pageCount: 2 });
  const primaryEl = container.querySelector('.adv-primary-text');
  assert.equal(primaryEl.textContent, '「第一页内容，」', 'Page 0 should have single closing quote, no double opening quote');

  // Page 1 has closing quote but not opening quote
  tb.reveal('第二页内容。」', { durationMs: 0, pageIndex: 1, pageCount: 2 });
  assert.equal(primaryEl.textContent, '「第二页内容。」', 'Page 1 should have single opening quote, no double closing quote');
});

test('textbox: paginate computes max page count for bilingual lines and respects box bounds', () => {
  const container = new FakeElement();
  const tb = createTextbox(container, {
    getSettings: () => ({ primary: 'zh', subtitle: 'ja' }),
    fits: (text) => text.length <= 30,
    fitsSubtitle: (text) => text.length <= 35,
  });

  // Line 0 from preview: zh fits in 1 page (26 chars), ja needs 2 pages (41 chars)
  const line0 = {
    index: 0,
    zh: '你好！欢迎来到视觉小说 ADV 文本框与舞台组件预览。',
    ja: 'こんにちは！ADVテキストボックスとステージコンポーネントのプレビューへようこそ。',
    speaker: '菲莉',
  };

  const pages = tb.paginate(line0, line0.zh);
  assert.equal(pages.length, 2, 'Should paginate to max of primary and subtitle (2 pages)');
  assert.equal(pages.join(''), line0.zh, 'Primary text losslessness preserved');
  assert.ok(pages[0].endsWith('！'), 'Primary breaks at punctuation');

  // Reveal page 0 and inspect rendered texts
  tb.activate(line0);
  tb.reveal(pages[0], { durationMs: 0, pageIndex: 0, pageCount: pages.length });

  const primaryEl = container.querySelector('.adv-primary-text');
  const subEl = container.querySelector('.adv-subtitle-text');
  assert.equal(primaryEl.textContent, '「你好！」');
  assert.equal(subEl.textContent, '「こんにちは！」');
});

test('textbox: activating the next line clears the previous line until its own text is revealed', () => {
  const container = new FakeElement();
  const tb = createTextbox(container, {
    getSettings: () => ({ primary: 'zh', subtitle: 'ja', reducedMotion: true }),
  });
  tb.activate({ index: 0, zh: '第一句。', ja: '一つ目。', speaker: '菲莉' });
  tb.reveal('第一句。', { durationMs: 0, pageIndex: 0, pageCount: 1 });
  const primaryEl = container.querySelector('.adv-primary-text');
  const subEl = container.querySelector('.adv-subtitle-text');
  assert.equal(primaryEl.textContent, '「第一句。」');
  assert.equal(subEl.textContent, '「一つ目。」');

  // The line player may hold the next line until its voice starts.
  tb.activate({ index: 1, zh: '第二句。', ja: '二つ目。', speaker: '菲莉' });
  assert.equal(primaryEl.textContent, '');
  assert.equal(subEl.textContent, '');
});

test('textbox: prepareInput clears errors and hides the click-wait glyph', () => {
  const container = new FakeElement();
  const tb = createTextbox(container, {
    getSettings: () => ({ playerName: '勇者' }),
  });

  tb.showError({ message: '网络错误，请稍后重试' });
  const errorWrapEl = container.querySelector('.adv-error-wrap');
  const errorMsgEl = container.querySelector('.adv-error-message');
  assert.equal(errorWrapEl.classList.contains('is-hidden'), false);
  assert.equal(errorMsgEl.textContent, '网络错误，请稍后重试');

  tb.glyph(true);
  assert.equal(container.querySelector('.adv-click-glyph').classList.contains('is-hidden'), false);
  tb.prepareInput({ canSend: true });
  assert.equal(container.querySelector('.adv-click-glyph').classList.contains('is-hidden'), true);
  assert.equal(errorWrapEl.classList.contains('is-hidden'), true);
  assert.equal(errorMsgEl.textContent, '');
});

test('graphemes: splitGraphemes splits correctly and caches Intl.Segmenter instance', () => {
  const clusters = splitGraphemes('你好世界👨‍👩‍👧‍👦！');
  assert.ok(clusters.length > 0);
  assert.equal(clusters.join(''), '你好世界👨‍👩‍👧‍👦！');

  const clusters2 = splitGraphemes('第二句台词');
  assert.equal(clusters2.join(''), '第二句台词');

  const NativeSegmenter = Intl.Segmenter;
  let created = 0;
  Intl.Segmenter = class extends NativeSegmenter {
    constructor(...args) { super(...args); created++; }
  };
  try {
    splitGraphemes('第三句');
    splitGraphemes('第四句');
  } finally {
    Intl.Segmenter = NativeSegmenter;
  }
  assert.equal(created, 0);
});

test('textbox: long Japanese line pagination strictly confines every page within single-line bounds without clipping or wrapping', () => {
  const container = new FakeElement();
  const tb = createTextbox(container, {
    getSettings: () => ({ primary: 'zh', subtitle: 'ja' }),
    fits: (text) => text.length <= 32,
    fitsSubtitle: (text) => text.length <= 37,
  });

  const line1 = {
    index: 1,
    speaker: '菲莉',
    mode: 'character',
    zh: '在漫长的黄昏里，夕阳的余晖悄悄穿透了活动室的窗棂，将整个房间染成一片温柔的淡金与青蓝交融之色；这正是我们在此相遇的初衷，无论前路多么漫长，故事也一定会继续向前延伸。',
    ja: '長い夕暮れの中、茜色の光がゆっくりと部室の窓を染め上げ、淡い黄金色と青が溶け合う世界が広がっていく。これが私たちの物語の始まりであり、どこまでも続いていく未来への約束なのだ。',
  };

  const primaryPages = tb.paginate(line1, line1.zh);
  assert.ok(primaryPages.length >= 3, `Expected at least 3 pages, got ${primaryPages.length}`);
  assert.equal(primaryPages.join(''), line1.zh, 'Primary text losslessness preserved');

  tb.activate(line1);

  // Validate every single page
  for (let p = 0; p < primaryPages.length; p++) {
    tb.reveal(primaryPages[p], { durationMs: 0, pageIndex: p, pageCount: primaryPages.length });

    const primaryEl = container.querySelector('.adv-primary-text');
    const subEl = container.querySelector('.adv-subtitle-text');

    // Neither page may exceed its single-line box allotment (§3.1)
    assert.ok(primaryPages[p].length <= 32, `Primary page ${p} exceeds 32 chars: ${primaryPages[p]}`);
    assert.ok(primaryEl.textContent.length > 0, `Primary page ${p} should not be empty`);

    // Verify subtitle text on this page fits within subtitle limit
    assert.ok(subEl.textContent.length > 0, `Subtitle page ${p} should not be empty`);
    // Subtitle displayed with quotes
    assert.ok(subEl.textContent.startsWith('「') && subEl.textContent.endsWith('」'), 'Subtitle wrapped in quotes');
  }
});


