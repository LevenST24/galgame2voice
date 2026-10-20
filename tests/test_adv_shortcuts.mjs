import assert from 'node:assert/strict';
import { test } from 'node:test';
import { decide, createShortcuts } from '../frontend/src/adv/shortcuts.js';
import { fakeDocument } from './helpers/adv_dom.mjs';

test('hidden restore precedes player input exclusion for stage click and Enter', () => {
  for (const event of [{ type: 'click', button: 0 }, { type: 'keydown', key: 'Enter' }]) {
    assert.equal(decide(event, { hidden: true, playerInput: true }), 'unhide');
  }
});

test('Enter and Space leave all interactive controls and their descendants native', () => {
  const doc = fakeDocument();
  for (const [tag, attr] of [['button'], ['a', ['href', '/settings']], ['input'], ['textarea'], ['select'],
    ['div', ['role', 'button']], ['div', ['contenteditable', '']], ['div', ['contenteditable', 'plaintext-only']]]) {
    const control = doc.createElement(tag); if (attr) control.setAttribute(...attr);
    const child = doc.createElement('span'); control.appendChild(child);
    for (const target of [control, child]) for (const key of ['Enter', ' ']) {
      assert.equal(decide({ type: 'keydown', key, target }, {}), null, `${tag} ${key}`);
    }
  }
});


test('shortcuts decide: §3.4 table standard keys outside input', () => {
  const ctx = { inInput: false, composing: false, overlayOpen: false, stageFocused: true, hidden: false };

  // AUTO: A / F6
  assert.equal(decide({ type: 'keydown', key: 'a' }, ctx), 'toggle_auto');
  assert.equal(decide({ type: 'keydown', key: 'A' }, ctx), 'toggle_auto');
  assert.equal(decide({ type: 'keydown', key: 'F6' }, ctx), 'toggle_auto');

  // SKIP toggle: F / F7
  assert.equal(decide({ type: 'keydown', key: 'f' }, ctx), 'toggle_skip');
  assert.equal(decide({ type: 'keydown', key: 'F' }, ctx), 'toggle_skip');
  assert.equal(decide({ type: 'keydown', key: 'F7' }, ctx), 'toggle_skip');

  // LOG: R / F5 (with stage focus)
  assert.equal(decide({ type: 'keydown', key: 'r' }, ctx), 'log');
  assert.equal(decide({ type: 'keydown', key: 'R' }, ctx), 'log');
  assert.equal(decide({ type: 'keydown', key: 'F5' }, ctx), 'log');

  // Replay voice: F9
  assert.equal(decide({ type: 'keydown', key: 'F9' }, ctx), 'replay_voice');

  // Hide window: H
  assert.equal(decide({ type: 'keydown', key: 'h' }, ctx), 'toggle_hide');
  assert.equal(decide({ type: 'keydown', key: 'H' }, ctx), 'toggle_hide');

  // Settings: F8
  assert.equal(decide({ type: 'keydown', key: 'F8' }, ctx), 'settings');

  // Swap languages: X
  assert.equal(decide({ type: 'keydown', key: 'x' }, ctx), 'swap_languages');
  assert.equal(decide({ type: 'keydown', key: 'X' }, ctx), 'swap_languages');
});

test('shortcuts decide: suppression inside input/editor', () => {
  const inInputCtx = { inInput: true, composing: false, overlayOpen: false, stageFocused: true, hidden: false };

  // Letters suppressed in input
  assert.equal(decide({ type: 'keydown', key: 'a' }, inInputCtx), null);
  assert.equal(decide({ type: 'keydown', key: 'f' }, inInputCtx), null);
  assert.equal(decide({ type: 'keydown', key: 'r' }, inInputCtx), null);
  assert.equal(decide({ type: 'keydown', key: 'h' }, inInputCtx), null);
  assert.equal(decide({ type: 'keydown', key: 'x' }, inInputCtx), null);

  // Function keys that are disabled while typing
  assert.equal(decide({ type: 'keydown', key: 'F6' }, inInputCtx), null);
  assert.equal(decide({ type: 'keydown', key: 'F7' }, inInputCtx), null);
  assert.equal(decide({ type: 'keydown', key: 'F9' }, inInputCtx), null);

  // Settings F8 remains available outside IME composition to inspect settings
  assert.equal(decide({ type: 'keydown', key: 'F8' }, inInputCtx), 'settings');

  // Enter & Space inside input are for editing (not advancing)
  assert.equal(decide({ type: 'keydown', key: 'Enter' }, inInputCtx), null);
  assert.equal(decide({ type: 'keydown', key: ' ' }, inInputCtx), null);

  // Right click inside input keeps editor context menu
  assert.equal(decide({ type: 'contextmenu' }, inInputCtx), null);

  // Wheel up over input does not open LOG
  assert.equal(decide({ type: 'wheel', deltaY: -50 }, inInputCtx), null);

  // Left click inside input does not advance
  assert.equal(decide({ type: 'click', button: 0 }, inInputCtx), null);

  // Hold Ctrl does not trigger skip inside input (allows native editing shortcuts like Ctrl+A, Ctrl+C)
  assert.equal(decide({ type: 'keydown', key: 'Control' }, inInputCtx), null);
  assert.equal(decide({ type: 'keyup', key: 'Control' }, inInputCtx), null);
});

test('shortcuts decide: suppression during IME composition', () => {
  const composingCtx = { inInput: false, composing: true, overlayOpen: false, stageFocused: true, hidden: false };

  assert.equal(decide({ type: 'keydown', key: 'Enter', isComposing: true }, composingCtx), null);
  assert.equal(decide({ type: 'keydown', key: 'a', isComposing: true }, composingCtx), null);
  assert.equal(decide({ type: 'keydown', key: 'F8', keyCode: 229 }, composingCtx), null);
  assert.equal(decide({ type: 'keydown', key: 'k', ctrlKey: true, isComposing: true }, composingCtx), null);
});

test('shortcuts decide: F5 only triggers LOG when stageFocused', () => {
  const focusedCtx = { stageFocused: true };
  const unfocusedCtx = { stageFocused: false };

  assert.equal(decide({ type: 'keydown', key: 'F5' }, focusedCtx), 'log');
  assert.equal(decide({ type: 'keydown', key: 'F5' }, unfocusedCtx), null);
});

test('shortcuts decide: Ctrl hold for temporary skip', () => {
  const ctx = { inInput: false };

  assert.equal(decide({ type: 'keydown', key: 'Control' }, ctx), 'skip-hold-start');
  assert.equal(decide({ type: 'keyup', key: 'Control' }, ctx), 'skip-hold-end');
});

test('shortcuts decide: right click toggles hide outside input', () => {
  assert.equal(decide({ type: 'contextmenu' }, { inInput: false }), 'toggle_hide');
  assert.equal(decide({ type: 'contextmenu' }, { inInput: true }), null);
});

test('shortcuts decide: wheel up opens LOG outside input and overlay', () => {
  assert.equal(decide({ type: 'wheel', deltaY: -100 }, { inInput: false, overlayOpen: false }), 'log');
  assert.equal(decide({ type: 'wheel', deltaY: -100 }, { inInput: true, overlayOpen: false }), null);
  assert.equal(decide({ type: 'wheel', deltaY: -100 }, { inInput: false, overlayOpen: true }), null);
  // Wheel down returns null
  assert.equal(decide({ type: 'wheel', deltaY: 100 }, { inInput: false, overlayOpen: false }), null);
});

test('shortcuts decide: click returns null when overlayOpen', () => {
  assert.equal(decide({ type: 'click', button: 0 }, { inInput: false, hidden: false, overlayOpen: true }), null);
});

test('shortcuts decide: advance via Enter, Space, and left click', () => {
  const ctx = { inInput: false, hidden: false };

  assert.equal(decide({ type: 'keydown', key: 'Enter' }, ctx), 'advance');
  assert.equal(decide({ type: 'keydown', key: ' ' }, ctx), 'advance');
  assert.equal(decide({ type: 'click', button: 0 }, ctx), 'advance');
});

test('shortcuts decide: hidden window unhide behavior', () => {
  const hiddenCtx = { inInput: false, hidden: true };

  assert.equal(decide({ type: 'keydown', key: 'Escape' }, hiddenCtx), 'unhide');
  assert.equal(decide({ type: 'keydown', key: 'Enter' }, hiddenCtx), 'unhide');
  assert.equal(decide({ type: 'keydown', key: ' ' }, hiddenCtx), 'unhide');
  assert.equal(decide({ type: 'click', button: 0 }, hiddenCtx), 'unhide');
});

test('shortcuts decide: Esc closes overlay or stops generation', () => {
  assert.equal(decide({ type: 'keydown', key: 'Escape' }, { overlayOpen: true }), 'close_overlay');
  assert.equal(decide({ type: 'keydown', key: 'Escape' }, { overlayOpen: false, hidden: false }), 'stop');
});

test('shortcuts decide: Ctrl+K starts new session outside composition', () => {
  assert.equal(decide({ type: 'keydown', key: 'k', ctrlKey: true }, { composing: false }), 'new_session');
  assert.equal(decide({ type: 'keydown', key: 'k', ctrlKey: true }, { composing: true }), null);
});

test('shortcuts decide: swap language X disabled when subtitle is none', () => {
  assert.equal(decide({ type: 'keydown', key: 'x' }, { subtitleNone: true }), null);
  assert.equal(decide({ type: 'keydown', key: 'x' }, { subtitleNone: false }), 'swap_languages');
});

test('shortcuts decide: unrelated keys return null', () => {
  const ctx = { inInput: false };
  assert.equal(decide({ type: 'keydown', key: 'b' }, ctx), null);
  assert.equal(decide({ type: 'keydown', key: 'ArrowRight' }, ctx), null);
  assert.equal(decide({ type: 'keydown', key: 'Tab' }, ctx), null);
});

test('shortcuts decide: Control key repeat returns null', () => {
  assert.equal(decide({ type: 'keydown', key: 'Control', repeat: true }, { inInput: false }), null);
  assert.equal(decide({ type: 'keydown', key: 'Control', repeat: false }, { inInput: false }), 'skip-hold-start');
});

test('shortcuts decide: blur clears temporary skip', () => {
  assert.equal(decide({ type: 'blur' }, { inInput: false }), 'skip-hold-end');
});

class FakeDOMElement {
  constructor(tag = 'div', className = '') {
    this.tagName = tag.toUpperCase();
    this.parentNode = null;
    this.children = [];
    this.listeners = {};
    const classes = new Set(className.split(/\s+/).filter(Boolean));
    this.classList = {
      add: (...n) => n.forEach(c => classes.add(c)),
      remove: (...n) => n.forEach(c => classes.delete(c)),
      contains: c => classes.has(c),
    };
    Object.defineProperty(this, 'className', {
      get: () => [...classes].join(' '),
      set: v => {
        classes.clear();
        String(v).split(/\s+/).filter(Boolean).forEach(c => classes.add(c));
      },
    });
  }

  appendChild(child) {
    child.parentNode = this;
    this.children.push(child);
    return child;
  }

  contains(el) {
    if (el === this) return true;
    for (const c of this.children) {
      if (c === el || (c.contains && c.contains(el))) return true;
    }
    return false;
  }

  closest(selector) {
    const selClasses = selector.split(',').map(s => s.trim().replace(/^\./, ''));
    let cur = this;
    while (cur) {
      for (const sc of selClasses) {
        if (cur.classList.contains(sc) || cur.tagName.toLowerCase() === sc) {
          return cur;
        }
      }
      cur = cur.parentNode;
    }
    return null;
  }

  addEventListener(type, fn) {
    (this.listeners[type] ||= []).push(fn);
  }

  removeEventListener(type, fn) {
    if (!this.listeners[type]) return;
    this.listeners[type] = this.listeners[type].filter(f => f !== fn);
  }

  dispatchEvent(event) {
    if (!event.target) event.target = this;
    let stopped = false;
    event.stopPropagation = () => { stopped = true; };
    const list = this.listeners[event.type] || [];
    for (const fn of list) {
      fn(event);
      if (stopped) break;
    }
    if (!stopped && this.parentNode) {
      this.parentNode.dispatchEvent(event);
    }
  }
}

test('shortcuts DOM: createShortcuts DOM attachment and preventDefault execution', () => {
  const target = new FakeDOMElement('div', 'test-root');
  const handled = [];
  const shortcuts = createShortcuts(target, {
    getContext: () => ({ inInput: false, stageFocused: true }),
    onAction: (act) => handled.push(act),
  });

  // Handled event: Enter -> advance, preventDefault should be called
  let pdCalled = false;
  target.dispatchEvent({
    type: 'keydown',
    key: 'Enter',
    preventDefault: () => { pdCalled = true; },
  });
  assert.equal(pdCalled, true, 'preventDefault called for handled shortcut');
  assert.deepEqual(handled, ['advance']);

  // Unhandled event: key 'b' -> preventDefault should NOT be called
  let pdUnhandled = false;
  target.dispatchEvent({
    type: 'keydown',
    key: 'b',
    preventDefault: () => { pdUnhandled = true; },
  });
  assert.equal(pdUnhandled, false, 'preventDefault should not be called for unhandled key');

  // Blur event triggers skip-hold-end
  target.dispatchEvent({
    type: 'blur',
    preventDefault: () => {},
  });
  assert.equal(handled[handled.length - 1], 'skip-hold-end');

  // Destroy cleans up
  shortcuts.destroy();
  target.dispatchEvent({
    type: 'keydown',
    key: 'Enter',
    preventDefault: () => {},
  });
  assert.equal(handled.length, 2, 'No more actions after destroy');
});

test('shortcuts DOM: click returns null and never advances when overlayOpen is true', () => {
  const shell = new FakeDOMElement('div', 'adv-shell');
  const stage = new FakeDOMElement('div', 'adv-stage');
  const stageBg = new FakeDOMElement('div', 'adv-layer-bg');
  shell.appendChild(stage);
  stage.appendChild(stageBg);

  const actions = [];
  const shortcuts = createShortcuts(shell, {
    getContext: () => ({ inInput: false, stageFocused: true, overlayOpen: true }),
    onAction: (act) => actions.push(act),
  });

  stageBg.dispatchEvent({
    type: 'click',
    button: 0,
    target: stageBg,
    preventDefault: () => {},
  });
  assert.equal(actions.length, 0, 'Click on stage when overlayOpen must not advance');
  shortcuts.destroy();
});

test('shortcuts DOM: clicks on letterbox, quick menu, and editor never advance; exactly one advance per stage click', () => {
  // Build hierarchy: shell -> stage -> [textbox -> textarea, quickmenu]
  const shell = new FakeDOMElement('div', 'adv-shell');
  const stage = new FakeDOMElement('div', 'adv-stage');
  const textbox = new FakeDOMElement('div', 'adv-layer-textbox');
  const editor = new FakeDOMElement('textarea', 'adv-player-textarea');
  const quickmenu = new FakeDOMElement('div', 'adv-quick-menu');
  const stageBg = new FakeDOMElement('div', 'adv-layer-bg');

  shell.appendChild(stage);
  stage.appendChild(stageBg);
  stage.appendChild(textbox);
  textbox.appendChild(editor);
  stage.appendChild(quickmenu);

  const actions = [];
  const shortcuts = createShortcuts(shell, {
    getContext: () => ({ inInput: false, stageFocused: true }),
    onAction: (act) => actions.push(act),
  });

  // 1. Click on letterbox (shell)
  shell.dispatchEvent({
    type: 'click',
    button: 0,
    target: shell,
    preventDefault: () => {},
  });
  assert.equal(actions.length, 0, 'Click on letterbox must not advance');

  // 2. Click on quick menu
  quickmenu.dispatchEvent({
    type: 'click',
    button: 0,
    target: quickmenu,
    preventDefault: () => {},
  });
  assert.equal(actions.length, 0, 'Click on quick menu must not advance');

  // 3. Click inside editor
  editor.dispatchEvent({
    type: 'click',
    button: 0,
    target: editor,
    preventDefault: () => {},
  });
  assert.equal(actions.length, 0, 'Click inside editor must not advance');

  // 4. Click on stage background (should advance exactly once)
  stageBg.dispatchEvent({
    type: 'click',
    button: 0,
    target: stageBg,
    preventDefault: () => {},
  });
  assert.deepEqual(actions, ['advance'], 'One stage click equals exactly one advance');

  shortcuts.destroy();
});

