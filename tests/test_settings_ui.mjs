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
  click() { this.handlers.click?.(); }
  querySelector() { return null; }
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
const key = (key, shiftKey = false) => {
  const event = { key, shiftKey, prevented: false, stopped: false,
    preventDefault() { this.prevented = true; }, stopImmediatePropagation() { this.stopped = true; } };
  handler(event); return event;
};

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
