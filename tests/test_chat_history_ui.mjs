import assert from 'node:assert/strict';
import { initApp } from '../frontend/src/main.js';
import { fakeChatStream, tick } from './helpers/chat_stream.mjs';

class Element {
  constructor(tag = 'div') {
    this.tag = tag; this.children = []; this.parentNode = null; this.textContent = '';
    this.value = ''; this.disabled = false; this.className = ''; this.dataset = {};
    this.style = { setProperty() {} }; this.scrollHeight = 0; this.scrollTop = 0;
    this.classList = {
      add: (...names) => { this.className = [...new Set([...this.className.split(' '), ...names])].filter(Boolean).join(' '); },
      remove: (...names) => { this.className = this.className.split(' ').filter(name => !names.includes(name)).join(' '); },
      contains: name => this.className.split(' ').includes(name),
      toggle: (name, enabled) => {
        const on = enabled ?? !this.classList.contains(name);
        this.classList[on ? 'add' : 'remove'](name);
        return on;
      },
    };
  }
  append(...nodes) { nodes.forEach(node => this.appendChild(node)); }
  prepend(el) { el.remove(); el.parentNode = this; this.children.unshift(el); }
  setAttribute() {}
  appendChild(el) { el.remove(); el.parentNode = this; this.children.push(el); return el; }
  remove() {
    if (this.parentNode) {
      this.parentNode.children = this.parentNode.children.filter(el => el !== this);
      this.parentNode = null;
    }
  }
  replaceWith(el) {
    if (!this.parentNode) return;
    const parent = this.parentNode; const index = parent.children.indexOf(this);
    el.remove(); parent.children[index] = el; el.parentNode = parent; this.parentNode = null;
  }
  set innerHTML(value) { this.children.forEach(el => { el.parentNode = null; }); this.children = []; this.textContent = value.replace(/<[^>]*>/g, ''); }
  get isConnected() { return this.tag === 'root' || Boolean(this.parentNode?.isConnected); }
  querySelector(selector) {
    for (const child of this.children) {
      if (child.className.split(' ').includes(selector.slice(1))) return child;
      const found = child.querySelector(selector); if (found) return found;
    }
    return null;
  }
  addEventListener() {}
  scrollTo({ top }) { this.scrollTop = top; }
}
const olderPage = () => ({ messages: [{ id: 10, role: 'user', content_chinese: '更早消息' }], has_more: false, oldest_id: 10 });
function setup(fetchPage = async () => olderPage()) {
  const session = { id: 's', title: 'Session', settings: {}, messages: [{ id: 'm_20', role: 'user', content: '已有消息' }], oldestMessageId: 20, hasMoreHistory: true };
  const dom = { messages: new Element('root'), input: new Element('input'), sendBtn: new Element('button'), stopBtn: new Element('button') };
  const noop = () => {};
  let requests = 0, sequence = 0;
  const toasts = [];
  const stream = fakeChatStream();
  globalThis.document = { createElement: tag => new Element(tag), createDocumentFragment: () => new Element('fragment') };
  const app = initApp({
    initialize: false, dom,
    state: { activeId: 's', global: { voiceMode: false } },
    getActive: () => session, getSession: () => session, isSessionDeleting: () => false,
    getCurrentActiveProvider: () => null, getVoiceProfiles: () => [], haveProvidersLoaded: () => true,
    saveState: noop, autoTitle: noop,
    persistSession: async () => {}, clearMicTimers: noop, getCurrentVoice: () => null,
    stopCurrentVoice: noop, setCurrentVoice: noop, uid: () => `local_${++sequence}`,
    showToast: text => toasts.push(text), fetch: stream.fetch,
    streamAudioController: { currentMsgId: null, isPlaying: false },
    portraitStage: { updateDialogue: noop, setEmotion: noop, handleMessageEmotion: noop, refreshAffection: noop },
    addMessage: (role, content) => { const msg = { id: `local_${++sequence}`, role, content }; session.messages.push(msg); return msg; },
    fetchSessionHistoryPage: async (...args) => { requests++; return fetchPage(...args); },
    earliestServerMessageId: () => 20,
  });
  app.fullRender();
  return { session, dom, toasts, streams: stream.requests, conversation: app.conversation,
    get stream() { return stream.requests.at(-1); }, get requests() { return requests; },
    send: text => app.sendMessage(text), load: () => app.loadOlderSessionHistory(session),
    retry: msg => app.retryFailedMessage(msg) };
}
const visibleText = el => [el.textContent, ...el.children.flatMap(child => visibleText(child))].filter(Boolean);
let passed = 0;
{
  const app = setup();
  app.send('用户提问');
  const typing = app.dom.messages.querySelector('.messages-inner').children.at(-1);
  assert.equal(app.dom.stopBtn.classList.contains('hidden'), false);
  await app.stream.onChunk('正在回复');
  assert.equal(typing.isConnected, false, 'first text removes the typing indicator');
  assert.equal(app.dom.messages.querySelector('.history-more-btn').disabled, true);
  await app.load();
  assert.equal(app.requests, 0); assert.ok(visibleText(app.dom.messages).includes('正在回复'));
  await app.stream.onChunk('完整回复'); await app.stream.onEnd('完整回复');
  assert.ok(visibleText(app.dom.messages).includes('完整回复'));
  assert.equal(app.dom.stopBtn.classList.contains('hidden'), true);
  assert.equal(app.dom.messages.querySelector('.history-more-btn').disabled, false);
  await app.load();
  assert.ok(visibleText(app.dom.messages).includes('完整回复'));
  assert.ok(visibleText(app.dom.messages).includes('更早消息')); passed++;
}
{
  let complete; const app = setup(() => new Promise(resolve => { complete = resolve; }));
  app.dom.input.value = '待发送';
  const pending = app.load(); await tick();
  assert.equal(app.dom.sendBtn.disabled, true);
  app.send('待发送'); await app.load();
  assert.equal(app.stream, undefined); assert.equal(app.session.messages.length, 1);
  assert.equal(app.dom.input.value, '待发送'); assert.equal(app.requests, 1);
  complete(olderPage()); await pending;
  assert.equal(app.dom.sendBtn.disabled, false);
  app.send('待发送'); await app.stream.onChunk('回复'); await app.stream.onEnd('回复');
  assert.ok(visibleText(app.dom.messages).includes('回复')); passed++;
}
{
  let fail = true;
  const app = setup(async () => { if (fail) throw new TypeError('offline'); return olderPage(); });
  app.dom.input.value = '可以重试';
  await app.load();
  assert.equal(app.session.loadingOlder, false); assert.equal(app.session.messages.length, 1);
  assert.equal(app.dom.sendBtn.disabled, false);
  assert.equal(app.dom.messages.querySelector('.history-more-btn').disabled, false);
  assert.match(app.toasts[0], /加载更早消息失败/);
  fail = false; await app.load();
  assert.equal(app.session.messages.length, 2); passed++;
}
{
  // A reply that never started keeps its reason under the message instead of only a toast.
  const app = setup();
  app.send('没有密钥时提问');
  const failed = app.session.messages.at(-1);
  await app.stream.onEnd('', { error: '尚未填写「Google Gemini」的 API Key：请打开「全局设置 → 对话模型」填写密钥后重新发送。', errorCode: 'API_KEY_MISSING' });
  assert.deepEqual(failed.failed, {
    error: '尚未填写「Google Gemini」的 API Key：请打开「全局设置 → 对话模型」填写密钥后重新发送。', code: 'API_KEY_MISSING',
  });
  assert.equal(app.toasts.length, 0);
  assert.equal(app.dom.sendBtn.disabled, true, 'empty input still disables send');
  // Resending reuses the same message: no duplicate bubble, same prompt, note cleared.
  const count = app.session.messages.length;
  app.retry(failed);
  assert.equal(app.streams.length, 2);
  assert.equal(app.stream.prompt, '没有密钥时提问');
  assert.equal(app.session.messages.length, count);
  assert.equal(failed.failed, undefined);
  await app.stream.onChunk('终于回复了'); await app.stream.onEnd('终于回复了');
  assert.equal(app.session.messages.at(-1).content, '终于回复了');
  assert.equal(app.session.messages.at(-2), failed); passed++;
}
{
  // Partial replies are kept as text; the error stays a toast and nothing is marked failed.
  const app = setup();
  app.send('说到一半断开');
  const sent = app.session.messages.at(-1);
  await app.stream.onChunk('半句');
  await app.stream.onEnd('半句', { error: '回复连接提前结束，已保留收到的内容。' });
  assert.equal(sent.failed, undefined);
  assert.equal(app.session.messages.at(-1).content, '半句');
  assert.match(app.toasts[0], /提前结束/); passed++;
}
{
  // Only the latest failed message can be resent, and never while another reply streams.
  const app = setup();
  app.send('第一次');
  const first = app.session.messages.at(-1);
  await app.stream.onEnd('', { error: '请求过于频繁，请稍候再试。', errorCode: 'RATE_LIMIT' });
  app.send('第二次');
  app.retry(first);
  assert.equal(app.streams.length, 2, 'an older failure is not resent mid-conversation');
  await app.stream.onEnd('', { error: '请求过于频繁，请稍候再试。', errorCode: 'RATE_LIMIT' });
  const second = app.session.messages.at(-1);
  // A failure note may still exist while its turn is streaming; retry must keep it.
  app.retry(second);
  second.failed = { error: '请求过于频繁', code: 'RATE_LIMIT' };
  app.retry(second);
  assert.equal(app.streams.length, 3);
  assert.ok(second.failed, 'the note stays until a resend actually starts');
  assert.match(app.toasts.at(-1), /回复完成后即可重新发送/);
  app.conversation.stop(); passed++;
}
console.log(`Chat history and streaming UI: ${passed} scenarios passed`);
