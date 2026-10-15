import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

// Exercise the actual entry-point handlers without booting auth, microphones,
// or the server. The DOM and network below are isolated test dependencies.
const main = readFileSync(new URL('../frontend/src/main.js', import.meta.url), 'utf8');
const ui = readFileSync(new URL('../frontend/src/ui.js', import.meta.url), 'utf8');
function functionSource(source, name) {
  const match = source.match(new RegExp(`(?:export )?(?:async )?function ${name}\\(`));
  assert.ok(match, `missing function ${name}`);
  const end = source.indexOf('\n}', match.index);
  return source.slice(match.index, end + 2).replace(/^export /, '');
}

class Element {
  constructor(tag = 'div') {
    this.tag = tag; this.children = []; this.parentNode = null; this.textContent = '';
    this.value = ''; this.disabled = false; this.className = '';
    this.style = { setProperty() {} }; this.scrollHeight = 0; this.scrollTop = 0;
    this.classList = { add() {}, remove() {}, toggle() {} };
  }
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
  set innerHTML(value) { this.children.forEach(el => { el.parentNode = null; }); this.children = []; }
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
const tick = () => new Promise(resolve => setTimeout(resolve, 0));
const olderPage = () => ({ messages: [{ id: 10, role: 'user', content_chinese: '更早消息' }], has_more: false, oldest_id: 10 });
function setup(fetchPage = async () => olderPage()) {
  const session = { id: 's', title: 'Session', settings: {}, messages: [{ id: 'm_20', role: 'user', content: '已有消息' }], oldestMessageId: 20, hasMoreHistory: true };
  const dom = { messages: new Element('root'), input: new Element('input'), sendBtn: new Element('button'), stopBtn: new Element('button') };
  const noop = () => {};
  let stream, requests = 0, sequence = 0;
  const toasts = [];
  const context = vm.createContext({
    document: { createElement: tag => new Element(tag), createDocumentFragment: () => new Element('fragment') },
    dom, state: { activeId: 's', global: { voiceMode: false } },
    getActive: () => session, getSession: () => session, isSessionDeleting: () => false,
    renderSidebar: noop, renderHeader: noop, playVoice: noop, resolveJapanese: noop,
    openGlobalSettings: noop, openSessionSettings: noop, saveState: noop, autoTitle: noop,
    persistSession: async () => {}, clearMicTimers: noop, getCurrentVoice: () => null,
    stopCurrentVoice: noop, setCurrentVoice: noop, uid: () => `local_${++sequence}`,
    showToast: text => toasts.push(text),
    streamChat: options => { stream = options; return noop; },
    streamAudioController: { currentMsgId: null, isPlaying: false },
    portraitStage: { updateDialogue: noop, setEmotion: noop, handleMessageEmotion: noop, refreshAffection: noop },
    addMessage: (role, content) => { const msg = { id: `local_${++sequence}`, role, content }; session.messages.push(msg); return msg; },
    createMessageEl: msg => { const el = new Element(); el.className = 'msg'; const contentEl = new Element(); contentEl.textContent = msg.content; el.appendChild(contentEl); return { el, contentEl }; },
    createTypingEl: () => new Element(),
    fetchSessionHistoryPage: async (...args) => { requests++; return fetchPage(...args); },
    earliestServerMessageId: () => 20,
    mapBackendMessage: msg => ({ ...msg, id: `m_${msg.id}`, content: msg.content_chinese }),
  });
  vm.runInContext('let listInner, nearBottom = true, busy = false, cancelStream = null, _syncSessionsGen = 0;', context);
  for (const name of ['fullRender', 'updateComposer', 'autoGrow', 'scrollBottom', 'maybeScroll', 'sendMessage', 'loadOlderSessionHistory', 'retryFailedMessage']) {
    vm.runInContext(functionSource(main, name), context);
  }
  vm.runInContext(functionSource(ui, 'renderMessages'), context);
  vm.runInContext(functionSource(ui, 'needsModelSettings'), context);
  vm.runInContext('fullRender();', context);
  const streams = [];
  context.streamChat = options => { stream = options; streams.push(options); return noop; };
  return { session, dom, context, toasts, streams, get stream() { return stream; }, get requests() { return requests; },
    send: text => context.sendMessage(text), load: () => context.loadOlderSessionHistory(session),
    retry: msg => context.retryFailedMessage(msg) };
}
const visibleText = el => [el.textContent, ...el.children.flatMap(child => visibleText(child))].filter(Boolean);
let passed = 0;
{
  const app = setup();
  app.send('用户提问'); app.stream.onChunk('正在回复');
  assert.equal(app.dom.messages.querySelector('.history-more-btn').disabled, true);
  await app.load();
  assert.equal(app.requests, 0); assert.ok(visibleText(app.dom.messages).includes('正在回复'));
  app.stream.onChunk('完整回复'); app.stream.onEnd('完整回复');
  assert.ok(visibleText(app.dom.messages).includes('完整回复'));
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
  app.send('待发送'); app.stream.onChunk('回复'); app.stream.onEnd('回复');
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
  app.stream.onEnd('', { error: '尚未填写「Google Gemini」的 API Key：请打开「全局设置 → 对话模型」填写密钥后重新发送。', errorCode: 'API_KEY_MISSING' });
  assert.deepEqual(JSON.parse(JSON.stringify(failed.failed)), {
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
  app.stream.onChunk('终于回复了'); app.stream.onEnd('终于回复了');
  assert.equal(app.session.messages.at(-1).content, '终于回复了');
  assert.equal(app.session.messages.at(-2), failed); passed++;
}
{
  // Partial replies are kept as text; the error stays a toast and nothing is marked failed.
  const app = setup();
  app.send('说到一半断开');
  const sent = app.session.messages.at(-1);
  app.stream.onChunk('半句');
  app.stream.onEnd('半句', { error: '回复连接提前结束，已保留收到的内容。' });
  assert.equal(sent.failed, undefined);
  assert.equal(app.session.messages.at(-1).content, '半句');
  assert.match(app.toasts[0], /提前结束/); passed++;
}
{
  // Only the latest failed message can be resent, and never while another reply streams.
  const app = setup();
  app.send('第一次');
  const first = app.session.messages.at(-1);
  app.stream.onEnd('', { error: '请求过于频繁，请稍候再试。', errorCode: 'RATE_LIMIT' });
  app.send('第二次');
  app.retry(first);
  assert.equal(app.streams.length, 2, 'an older failure is not resent mid-conversation');
  app.stream.onEnd('', { error: '请求过于频繁，请稍候再试。', errorCode: 'RATE_LIMIT' });
  const second = app.session.messages.at(-1);
  vm.runInContext('busy = true', app.context);
  app.retry(second);
  assert.equal(app.streams.length, 2);
  assert.ok(second.failed, 'the note stays until a resend actually starts');
  assert.match(app.toasts.at(-1), /回复完成后即可重新发送/); passed++;
}
console.log(`Chat history and streaming UI: ${passed} scenarios passed`);
