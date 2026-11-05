import assert from 'node:assert/strict';
import { test } from 'node:test';
import { openModal, closeModal, hasOpenModal, initSettingsUi } from '../frontend/src/settings_ui.js';

// Exercise focus ownership and keyboard behavior with a small DOM surface.
class Node {
  constructor(parent = null) {
    this.parent = parent; this.isConnected = true; this.disabled = false; this.tabIndex = 0;
    this.attrs = {}; this.classes = new Set(); this.handlers = {};
    this.classList = { add: n => this.classes.add(n), remove: n => this.classes.delete(n) };
  }
  focus() { document.activeElement = this; }
  closest(selector) {
    for (let node = this; node; node = node.parent) {
      if ((selector.includes('[inert]') && node.inert) ||
          (selector.includes('[hidden]') && node.hidden) ||
          (selector.includes('.hidden') && node.classes.has('hidden'))) return node;
    }
    return null;
  }
  contains(node) { for (; node; node = node.parent) if (node === this) return true; return false; }
  getClientRects() { return this.closest('[inert], [hidden], .hidden') ? [] : [{}]; }
  hasAttribute(name) { return name in this.attrs; }
  addEventListener(name, callback) { this.handlers[name] = callback; }
  removeEventListener(name) { delete this.handlers[name]; }
  click() { this.handlers.click?.(); }
  querySelector() { return null; }
  querySelectorAll() { return []; }
}
const app = new Node();
let handler;
globalThis.document = {
  activeElement: null,
  getElementById: id => id === 'advRoot' ? app : null,
  querySelectorAll: () => [],
  addEventListener: (_event, callback) => { handler = callback; },
};
initSettingsUi();
function dialog() {
  const overlay = new Node(), modal = new Node(overlay), first = new Node(modal), last = new Node(modal);
  overlay.querySelector = selector => selector === '.modal' ? modal : selector === '[data-close]' ? first : null;
  overlay.querySelectorAll = () => [modal, first, last];
  first.addEventListener('click', () => closeModal(overlay));
  return { overlay, modal, first, last };
}
const key = (key, shiftKey = false, extra = {}) => {
  const event = { key, shiftKey, prevented: false, stopped: false, ...extra,
    preventDefault() { this.prevented = true; }, stopImmediatePropagation() { this.stopped = true; } };
  handler(event); return event;
};

test('Escape key during IME composition (isComposing or keyCode 229) does not close the dialog', () => {
  const d = dialog(); openModal(d.overlay);
  assert.equal(hasOpenModal(), true);

  const eventComposing = key('Escape', false, { isComposing: true });
  assert.equal(eventComposing.prevented, false);
  assert.equal(eventComposing.stopped, false);
  assert.equal(hasOpenModal(), true, 'Modal should remain open when isComposing is true');

  const event229 = key('Escape', false, { keyCode: 229 });
  assert.equal(event229.prevented, false);
  assert.equal(event229.stopped, false);
  assert.equal(hasOpenModal(), true, 'Modal should remain open when keyCode is 229');

  const eventTabComposing = key('Tab', false, { isComposing: true });
  assert.equal(eventTabComposing.prevented, false, 'Tab should not prevent default when isComposing is true');

  const eventTab229 = key('Tab', false, { keyCode: 229 });
  assert.equal(eventTab229.prevented, false, 'Tab should not prevent default when keyCode is 229');

  const eventNormal = key('Escape');
  assert.equal(eventNormal.prevented, true);
  assert.equal(eventNormal.stopped, true);
  assert.equal(hasOpenModal(), false, 'Modal should close on normal Escape');
});

test('closing a dialog restores its trigger and releases the background', () => {
  const trigger = new Node(); trigger.focus();
  const d = dialog(); openModal(d.overlay);
  assert.equal(document.activeElement, d.modal); assert.equal(app.inert, true);
  closeModal(d.overlay);
  assert.equal(document.activeElement, trigger); assert.equal(app.inert, false); assert.equal(hasOpenModal(), false);
});
test('Tab and Shift+Tab remain in the visible dialog', () => {
  const d = dialog(); openModal(d.overlay);
  assert.equal(key('Tab').prevented, true); assert.equal(document.activeElement, d.first);
  assert.equal(key('Tab', true).prevented, true); assert.equal(document.activeElement, d.last);
  assert.equal(key('Tab').prevented, true); assert.equal(document.activeElement, d.first);
  closeModal(d.overlay);
});
test('a nested token dialog owns focus and Escape closes only that dialog', () => {
  const settings = dialog(), token = dialog();
  openModal(settings.overlay); settings.last.focus(); openModal(token.overlay, {focus:token.last});
  assert.equal(settings.overlay.inert, true); assert.equal(document.activeElement, token.last);
  const event = key('Escape');
  assert.equal(event.stopped, true); assert.equal(token.overlay.hidden, true);
  assert.equal(settings.overlay.inert, false); assert.equal(app.inert, true);
  assert.equal(document.activeElement, settings.last); assert.equal(hasOpenModal(), true);
  closeModal(settings.overlay);
});
test('reopening an already open dialog does not leave duplicate focus ownership', () => {
  const d = dialog(); openModal(d.overlay); openModal(d.overlay); closeModal(d.overlay);
  assert.equal(hasOpenModal(), false); assert.equal(app.inert, false);
});
test('hidden and disabled controls are excluded from the focus loop', () => {
  const d = dialog(); d.last.disabled = true; openModal(d.overlay); d.first.focus();
  assert.equal(key('Tab').prevented, true); assert.equal(document.activeElement, d.first);
  d.first.hidden = true;
  assert.equal(key('Tab').prevented, true);
  closeModal(d.overlay);
});

test('auth token input ignores Enter and Escape during IME composition and preserves normal submit and cancel', async () => {
  const modal = new Node(), input = new Node(modal), saveBtn = new Node(modal);
  modal.id = 'tokenModal';
  input.id = 'consoleTokenInput';
  saveBtn.id = 'tokenModalSave';
  input.value = 'test-token';

  const oldGetElementById = document.getElementById;
  const hadWindow = 'window' in globalThis;
  const oldWindow = globalThis.window;
  document.getElementById = id => {
    if (id === 'tokenModal') return modal;
    if (id === 'consoleTokenInput') return input;
    if (id === 'tokenModalSave') return saveBtn;
    return oldGetElementById(id);
  };
  globalThis.window = {
    sessionStorage: {
      _store: {},
      getItem(k) { return this._store[k] || ''; },
      setItem(k, v) { this._store[k] = String(v); },
      removeItem(k) { delete this._store[k]; },
    },
  };

  const { promptConsoleToken } = await import('../frontend/src/auth.js');

  try {
    let resolved = false;
    let p = promptConsoleToken().then(val => { resolved = true; return val; });

    // 1. Escape with isComposing
    const imeEscape1 = { key: 'Escape', isComposing: true, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; } };
    input.handlers.keydown(imeEscape1);
    assert.equal(imeEscape1.defaultPrevented, false);
    assert.equal(resolved, false);

    // 2. Escape with keyCode 229
    const imeEscape2 = { key: 'Escape', keyCode: 229, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; } };
    input.handlers.keydown(imeEscape2);
    assert.equal(imeEscape2.defaultPrevented, false);
    assert.equal(resolved, false);

    // 3. Enter with isComposing
    const imeEnter1 = { key: 'Enter', isComposing: true, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; } };
    input.handlers.keydown(imeEnter1);
    assert.equal(imeEnter1.defaultPrevented, false);
    assert.equal(resolved, false);

    // 4. Enter with keyCode 229
    const imeEnter2 = { key: 'Enter', keyCode: 229, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; } };
    input.handlers.keydown(imeEnter2);
    assert.equal(imeEnter2.defaultPrevented, false);
    assert.equal(resolved, false);

    // 5. Normal Escape cancels
    const normalEscape = { key: 'Escape', defaultPrevented: false, preventDefault() { this.defaultPrevented = true; } };
    input.handlers.keydown(normalEscape);
    assert.equal(normalEscape.defaultPrevented, true);
    const resultCancel = await p;
    assert.equal(resultCancel, null);
    assert.equal(resolved, true);

    // 6. Next prompt: normal Enter submits token
    resolved = false;
    p = promptConsoleToken().then(val => { resolved = true; return val; });
    input.value = 'saved-token-123';
    const normalEnter = { key: 'Enter', defaultPrevented: false, preventDefault() { this.defaultPrevented = true; } };
    input.handlers.keydown(normalEnter);
    assert.equal(normalEnter.defaultPrevented, true);
    const resultSave = await p;
    assert.equal(resultSave, 'saved-token-123');
    assert.equal(resolved, true);
  } finally {
    document.getElementById = oldGetElementById;
    if (hadWindow) globalThis.window = oldWindow;
    else delete globalThis.window;
  }
});
