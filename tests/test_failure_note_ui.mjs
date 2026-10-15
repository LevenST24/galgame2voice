import assert from 'node:assert/strict';
import { test } from 'node:test';

// Minimal DOM for the real ui.js renderers: enough structure to find text and click buttons.
class Element {
  constructor(tag) {
    this.tag = tag; this.children = []; this.attributes = {}; this.listeners = {};
    this.textContent = ''; this.title = ''; this.type = ''; this._html = '';
    const classes = new Set();
    this.classList = {
      add: (...names) => names.forEach(n => classes.add(n)), remove: (...names) => names.forEach(n => classes.delete(n)),
      toggle: (n, on) => ((on ?? !classes.has(n)) ? classes.add(n) : classes.delete(n)), contains: n => classes.has(n),
    };
    Object.defineProperty(this, 'className', { get: () => [...classes].join(' '), set: v => { classes.clear(); String(v).split(/\s+/).filter(Boolean).forEach(n => classes.add(n)); } });
    this.style = { setProperty() {} };
  }
  set innerHTML(value) { this._html = value; this.children = []; }
  get innerHTML() { return this._html; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  appendChild(child) { this.children.push(child); return child; }
  append(...nodes) { nodes.forEach(n => this.appendChild(n)); }
  prepend(node) { this.children.unshift(node); }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  click() { (this.listeners.click || []).forEach(fn => fn({ preventDefault() {}, stopPropagation() {} })); }
  querySelector(selector) { return this.findAll(selector)[0] || null; }
  findAll(selector) {
    const name = selector.slice(1);
    return this.children.flatMap(c => [...(c.classList.contains(name) ? [c] : []), ...c.findAll(selector)]);
  }
}
globalThis.document = { createElement: tag => new Element(tag), documentElement: { style: { values: {}, setProperty(k, v) { this.values[k] = v; } } } };

const { createMessageEl, needsModelSettings, keepToastsAboveComposer } = await import('../frontend/src/ui.js');
const buttons = el => el.findAll('.msg-failure-btn');

test('a failed user message shows the reason with resend and model-settings actions', () => {
  const calls = [];
  const msg = { id: 'm1', role: 'user', content: '你好', ts: 0, failed: { error: '尚未填写 API Key：请打开「全局设置 → 对话模型」', code: 'API_KEY_MISSING' } };
  const { el } = createMessageEl(msg, { onRetry: m => calls.push(['retry', m]), onOpenModels: () => calls.push(['models']) });
  assert.ok(el.classList.contains('has-failure'));
  const note = el.querySelector('.msg-failure');
  assert.equal(note.attributes.role, 'status');
  assert.equal(note.querySelector('.msg-failure-text').textContent, `未收到回复：${msg.failed.error}`);
  assert.deepEqual(buttons(el).map(b => b.textContent), ['重新发送', '配置对话模型']);
  buttons(el).forEach(b => b.click());
  assert.deepEqual(calls, [['retry', msg], ['models']]);
});

test('rate limits and older failures offer only what helps', () => {
  const limited = { id: 'm2', role: 'user', content: 'a', ts: 0, failed: { error: '请求过于频繁', code: 'RATE_LIMIT' } };
  assert.deepEqual(buttons(createMessageEl(limited, { onRetry() {}, onOpenModels() {} }).el).map(b => b.textContent), ['重新发送']);
  const older = { ...limited, id: 'm3', failed: { error: '模型地址无法连接', code: null } };
  assert.deepEqual(buttons(createMessageEl(older, { onOpenModels() {} }).el).map(b => b.textContent), ['配置对话模型']);
  const offline = { ...limited, id: 'm4', failed: { error: '无法连接本机服务。请保留启动窗口' } };
  assert.equal(buttons(createMessageEl(offline, { onOpenModels() {} }).el).length, 0);
});

test('only user messages with a recorded reason get a note', () => {
  for (const msg of [
    { id: 'a', role: 'assistant', content: '回复', ts: 0, failed: { error: 'x' } },
    { id: 'u', role: 'user', content: '正常', ts: 0 },
    { id: 'bad', role: 'user', content: '损坏', ts: 0, failed: true },
  ]) {
    const { el } = createMessageEl(msg, {});
    assert.equal(el.querySelector('.msg-failure'), null, msg.id);
    assert.equal(el.classList.contains('has-failure'), false, msg.id);
  }
});

test('error codes decide the settings shortcut; text is the fallback', () => {
  assert.equal(needsModelSettings('anything', 'PRIVATE_ENDPOINT_BLOCKED'), true);
  assert.equal(needsModelSettings('请求过于频繁', 'OPENAI_RATE_LIMIT'), false);
  assert.equal(needsModelSettings('API Key 无效', null), true);
  assert.equal(needsModelSettings('连接中断，请检查服务与网络后再试。', null), false);
});

test('toasts track the composer height so they never cover the input', () => {
  let observed; let callback;
  globalThis.ResizeObserver = class { constructor(cb) { callback = cb; } observe(target) { observed = target; } };
  let height = 96.2;
  const composer = { getBoundingClientRect: () => ({ height }) };
  keepToastsAboveComposer(composer);
  assert.equal(observed, composer);
  assert.equal(document.documentElement.style.values['--composer-clearance'], '97px');
  height = 180; callback();
  assert.equal(document.documentElement.style.values['--composer-clearance'], '180px');
  delete globalThis.ResizeObserver;
  keepToastsAboveComposer(composer); // older browsers keep the CSS fallback
});
