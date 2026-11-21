import assert from 'node:assert/strict';
import { packTarBlob } from '../frontend/src/tar_pack.js';
import { state, loadState, DEFAULT_SESSION_SETTINGS } from '../frontend/src/store.js';
import { initVoiceSettings, loadSessionVoiceSelect, FIRST_USE_VOICE_HINT } from '../frontend/src/controllers/voice_settings.js';

function parseUstar(bytes) {
  assert.equal(bytes.length % 512, 0);
  assert.equal(bytes.subarray(bytes.length - 1024).every((b) => b === 0), true);
  const dec = new TextDecoder('utf-8');
  const str = (b, s, n) => { const sl = b.subarray(s, s + n), i = sl.indexOf(0); return dec.decode(i < 0 ? sl : sl.subarray(0, i)); };
  const oct = (b, s, n) => parseInt(str(b, s, n).trim() || '0', 8);
  const files = [];
  for (let off = 0; off < bytes.length;) {
    const blk = bytes.subarray(off, off + 512);
    if (blk.every((b) => b === 0)) { assert.equal(bytes.subarray(off).every((b) => b === 0), true); break; }
    assert.equal(dec.decode(blk.subarray(257, 263)), 'ustar\0');
    assert.equal(dec.decode(blk.subarray(263, 265)), '00');
    let sum = 0;
    for (let i = 0; i < 512; i++) sum += i >= 148 && i < 156 ? 0x20 : blk[i];
    assert.equal(oct(blk, 148, 8), sum);
    assert.equal(String.fromCharCode(blk[156]), '0');
    const size = oct(blk, 124, 12), prefix = str(blk, 345, 155), name = str(blk, 0, 100);
    const pad = Math.ceil(size / 512) * 512;
    assert.equal(bytes.subarray(off + 512 + size, off + 512 + pad).every((b) => b === 0), true);
    files.push({ path: prefix ? `${prefix}/${name}` : name, size, content: bytes.subarray(off + 512, off + 512 + size) });
    off += 512 + pad;
  }
  return files;
}

// 1. tar_pack header fields, checksum, 512 alignment, and prefix long paths
{
  const audio = new Uint8Array(513).map((_, i) => (i * 7 + 3) & 0xff);
  const longPath = `characters/${'nested_folder_'.repeat(6)}爱丽丝/portrait/gentle.png`;
  assert.ok(new TextEncoder().encode(longPath).length > 100);
  const blob = packTarBlob([
    { path: '爱丽丝/refs/gentle.wav', file: new Blob([audio]) },
    { path: longPath, file: new Blob([new Uint8Array([1, 2, 3, 4])]) },
  ]);
  const parsed = parseUstar(new Uint8Array(await blob.arrayBuffer()));
  assert.deepEqual(parsed.map((f) => [f.path, f.size]), [['爱丽丝/refs/gentle.wav', 513], [longPath, 4]]);
  assert.deepEqual(parsed[0].content, audio);
  assert.throws(() => packTarBlob([{ path: `${'あ'.repeat(40)}.wav`, file: new Blob(['x']) }]), /路径过长/);
}

// --- Minimal DOM & XHR harness ---
const store = new Map();
globalThis.localStorage = { getItem: (k) => store.get(k) ?? null, setItem: (k, v) => store.set(k, v) };
const mkEl = (val = '', classes = []) => {
  const ls = {}, set = new Set(classes);
  return {
    value: val, files: [], disabled: false, hidden: false, textContent: '', children: [], dataset: {}, style: { setProperty() {} },
    classList: { contains: (n) => set.has(n), add: (...a) => a.forEach((n) => set.add(n)), remove: (...a) => a.forEach((n) => set.delete(n)), toggle: (n, f) => (f === undefined ? !set.has(n) : f) ? (set.add(n), true) : (set.delete(n), false) },
    get options() { return this.children; },
    set innerHTML(h) { this.children = h.includes('不指定（沿用语音服务当前的声音）') ? [{ value: '' }] : []; },
    addEventListener(n, fn) { (ls[n] ||= []).push(fn); },
    removeEventListener(n, fn) { ls[n] = (ls[n] || []).filter((h) => h !== fn); },
    async fire(n, ev = {}) { for (const fn of [...(ls[n] || [])]) await fn(ev); },
    appendChild(c) { this.children.push(c); },
    append(...cs) { this.children.push(...cs); },
    querySelectorAll: () => [], remove() {},
  };
};
const tick = () => new Promise((r) => setTimeout(r, 0));

function setupEnv() {
  const toastRoot = mkEl();
  globalThis.document = { getElementById: (id) => (id === 'toastRoot' ? toastRoot : null), createElement: () => mkEl() };
  loadState();
  const session = { id: 's1', title: '测试', settings: { ...DEFAULT_SESSION_SETTINGS }, messages: [] };
  state.sessions = [session]; state.activeId = session.id;
  const ids = ['sessionModal', 'sSystem', 'sVoice', 'sVoiceActive', 'sVoiceImportZipInput', 'sVoiceImportDirInput', 'sVoiceRescan', 'sVoiceDelete', 'sCvName', 'sCvGpt', 'sCvSovits', 'sCvRef', 'sCvPromptText', 'sCvCreate'];
  const dom = Object.assign(Object.fromEntries(ids.map((id) => [id, mkEl()])), {
    sVoiceImport: mkEl('', ['btn', 'primary']), sVoiceImportFolder: mkEl('', ['btn', 'ghost']),
    sVoiceDropOverlay: mkEl('', ['hidden']), sCustomVoiceBox: mkEl('', ['hidden']), sCvLang: mkEl('ja'),
  });
  let openedSettings = 0, renderSidebarCalls = 0, renderHeaderCalls = 0;
  initVoiceSettings(dom, {
    onOpenSessionSettings: () => { openedSettings += 1; },
    renderSidebar: () => { renderSidebarCalls += 1; },
    renderHeader: () => { renderHeaderCalls += 1; },
  });
  return {
    dom, session, toastRoot,
    getOpenedSettings: () => openedSettings,
    getRenderCounts: () => ({ sidebar: renderSidebarCalls, header: renderHeaderCalls }),
    cleanup: () => toastRoot.children.forEach((c) => clearTimeout(c._timer)),
  };
}

// 2. First-use hint toggle + 409 confirm -> overwrite=1 & select new char; cancel -> no retry + failure toast
const xhrs = [];
const prevXHR = globalThis.XMLHttpRequest, prevWin = globalThis.window;
globalThis.XMLHttpRequest = class {
  constructor() { this.upload = {}; xhrs.push(this); }
  open(m, u) { this.method = m; this.url = u; } setRequestHeader() {} send() {}
};

const { dom, session, toastRoot, getOpenedSettings, getRenderCounts, cleanup } = setupEnv();
let profiles = [], confirmResult = false;
let confirmCalls = 0;
globalThis.window = { confirm: () => { confirmCalls += 1; return confirmResult; } };
const fetchCalls = [];
let switchShouldFail = false;
globalThis.fetch = async (url, init = {}) => {
  fetchCalls.push({ url: String(url), method: init.method || 'GET' });
  if (String(url).includes('/api/voice/switch') && switchShouldFail) {
    return { ok: false, status: 503, json: async () => ({ detail: 'engine not ready' }) };
  }
  return {
    ok: true, status: 200,
    json: async () => (String(url).endsWith('/profiles')
      ? { profiles, active_profile_id: profiles[0]?.id ?? null }
      : { profile: profiles[0]?.name || 'ok' }),
  };
};

try {
  // First-use hint when empty
  await loadSessionVoiceSelect(session);
  assert.equal(dom.sVoiceActive.textContent, FIRST_USE_VOICE_HINT);
  assert.equal(dom.sVoiceImport.classList.contains('primary'), true);

  // 409 cancelled -> no retry
  confirmResult = false;
  const z1 = new Blob(['PK\x03\x04']); z1.name = 'alice.zip';
  dom.sVoiceImportZipInput.files = [z1];
  const pCancel = dom.sVoiceImportZipInput.fire('change');
  await tick();
  xhrs[0].status = 409; xhrs[0].responseText = JSON.stringify({ code: 'package_exists', names: ['爱丽丝'] });
  xhrs[0].onload();
  await pCancel;
  assert.equal(xhrs.length, 1);

  // 409 confirmed -> retry with overwrite=1 -> select imported profile & switch hint off
  confirmResult = true;
  dom.sVoiceImportZipInput.files = [z1];
  const pConfirm = dom.sVoiceImportZipInput.fire('change');
  await tick();
  assert.equal(xhrs.length, 2);
  xhrs[1].status = 409; xhrs[1].responseText = JSON.stringify({ code: 'package_exists', names: ['爱丽丝'] });
  xhrs[1].onload();
  await tick();
  assert.equal(xhrs.length, 3);
  assert.match(xhrs[2].url, /overwrite=1/);
  xhrs[2].upload.onprogress({ lengthComputable: true, loaded: 50, total: 100 });
  assert.equal(dom.sVoiceImport.textContent, '正在导入…50%');
  profiles = [{ id: 8, name: '爱丽丝', system_prompt: '你是爱丽丝' }];
  xhrs[2].status = 200;
  xhrs[2].responseText = JSON.stringify({ status: 'ok', imported: [{ name: '爱丽丝', profile_id: 8 }] });
  xhrs[2].onload();
  await pConfirm;
  assert.equal(dom.sVoice.value, '8');
  assert.equal(dom.sSystem.value, '你是爱丽丝');
  assert.equal(dom.sVoiceImport.classList.contains('primary'), false);
  assert.equal(dom.sVoiceActive.textContent, '当前加载：爱丽丝');
  // Session had no character bound (voiceProfileId === null), so import must bind & persist immediately.
  await tick();
  await tick();
  assert.equal(session.settings.voiceProfileId, 8);
  assert.equal(toastRoot.children.at(-1)?.dataset.msg, '已导入并选用「爱丽丝」，现在可以直接开始聊天');
  assert.ok(
    !toastRoot.children.some((c) => String(c.dataset.msg || '').includes('已切换音色')),
    'auto-bind must switch voice silently without a duplicate 已切换音色 toast',
  );
  assert.ok(fetchCalls.some((c) => c.url.includes('/api/chat/sessions') && c.method === 'POST'), 'must persist session binding');
  assert.ok(fetchCalls.some((c) => c.url.includes('/api/voice/switch') && c.method === 'POST'), 'must trigger ensureSessionVoice');
  assert.ok(getRenderCounts().sidebar >= 1, 'auto-bind must call renderSidebar');
  assert.ok(getRenderCounts().header >= 1, 'auto-bind must call renderHeader');

  // Failure response -> error toast
  dom.sVoiceImportZipInput.files = [z1];
  const pFail = dom.sVoiceImportZipInput.fire('change');
  await tick();
  xhrs[3].status = 422; xhrs[3].responseText = JSON.stringify({ detail: '缺少参考音频' });
  xhrs[3].onload();
  await pFail;
  assert.equal(toastRoot.children.at(-1)?.dataset.msg, '缺少参考音频');
  assert.match(toastRoot.children.at(-1)?.className || '', /toast-error/);

  // Manifest names gpt.ckpt / sovits.pth, but the import response says neither file is usable.
  // The success toast must say the character still cannot speak. A package with weights keeps the old text.
  profiles = [{ id: 9, name: '缺权重小灯', system_prompt: '你是缺权重小灯。' }];
  dom.sVoiceImportZipInput.files = [z1];
  const pMissing = dom.sVoiceImportZipInput.fire('change');
  await tick();
  const missingXhr = xhrs.at(-1);
  missingXhr.status = 200;
  missingXhr.responseText = JSON.stringify({
    status: 'ok',
    imported: [{ name: '缺权重小灯', folder: 'walk_noweight', character_id: 'walk_noweight', profile_id: 9, missing_voice_weights: true }],
  });
  missingXhr.onload();
  await pMissing;
  assert.equal(dom.sVoice.value, '9');
  assert.equal(toastRoot.children.at(-1)?.dataset.msg, '已导入「缺权重小灯」，但还不能出声，需要补上模型权重');
  assert.match(toastRoot.children.at(-1)?.className || '', /toast-success/);

  profiles = [{ id: 10, name: '爱丽丝', system_prompt: '你是爱丽丝' }];
  dom.sVoiceImportZipInput.files = [z1];
  const pReady = dom.sVoiceImportZipInput.fire('change');
  await tick();
  const readyXhr = xhrs.at(-1);
  readyXhr.status = 200;
  readyXhr.responseText = JSON.stringify({
    status: 'ok',
    imported: [{ name: '爱丽丝', profile_id: 10, missing_voice_weights: false }],
  });
  readyXhr.onload();
  await pReady;
  assert.equal(session.settings.voiceProfileId, 8, 'already-bound session must not be overwritten on import');
  assert.equal(dom.sVoice.value, '10');
  assert.equal(toastRoot.children.at(-1)?.dataset.msg, '已导入「爱丽丝」，点「保存」即可使用');
  assert.match(toastRoot.children.at(-1)?.className || '', /toast-success/);

  profiles = [
    { id: 11, name: '爱丽丝', system_prompt: '你是爱丽丝' },
    { id: 12, name: '缺权重小灯', system_prompt: '你是缺权重小灯。' },
  ];
  dom.sVoiceImportZipInput.files = [z1];
  const pMixed = dom.sVoiceImportZipInput.fire('change');
  await tick();
  const mixedXhr = xhrs.at(-1);
  mixedXhr.status = 200;
  mixedXhr.responseText = JSON.stringify({
    status: 'ok',
    imported: [
      { name: '爱丽丝', profile_id: 11, missing_voice_weights: false },
      { name: '缺权重小灯', profile_id: 12, missing_voice_weights: true },
    ],
  });
  mixedXhr.onload();
  await pMixed;
  assert.equal(
    toastRoot.children.at(-1)?.dataset.msg,
    '已导入「爱丽丝」、「缺权重小灯」，其中「缺权重小灯」还不能出声，需要补上模型权重',
  );

  // Closing Settings Center mid-import must not mutate hidden form fields, prompt confirm(),
  // or tell the user to click a hidden「保存」button.
  dom.sVoice.value = '11';
  dom.sSystem.value = '用户手写的未保存人设';
  confirmCalls = 0;
  profiles = [
    { id: 11, name: '爱丽丝', system_prompt: '你是爱丽丝' },
    { id: 13, name: '闭窗导入角色', system_prompt: '你是闭窗导入角色' },
  ];
  dom.sVoiceImportZipInput.files = [z1];
  const pClosed = dom.sVoiceImportZipInput.fire('change');
  await tick();
  dom.sessionModal.hidden = true;
  dom.sessionModal.classList.add('hidden');
  const closedXhr = xhrs.at(-1);
  closedXhr.status = 200;
  closedXhr.responseText = JSON.stringify({
    status: 'ok',
    imported: [{ name: '闭窗导入角色', profile_id: 13, missing_voice_weights: false }],
  });
  closedXhr.onload();
  await pClosed;
  assert.equal(confirmCalls, 0);
  assert.equal(session.settings.voiceProfileId, 8);
  assert.equal(dom.sVoice.value, '11');
  assert.equal(dom.sSystem.value, '用户手写的未保存人设');
  const closedToast = toastRoot.children.at(-1);
  assert.equal(closedToast?.dataset.msg, '已导入「闭窗导入角色」，可在「角色与会话」中选用');
  assert.match(closedToast?.className || '', /toast-success/);
  const actionBtn = closedToast?.children.find((c) => c.className === 'toast-action-btn');
  assert.equal(actionBtn?.textContent, '去选用');
  await actionBtn.fire('click', { stopPropagation() {} });
  assert.equal(getOpenedSettings(), 1);

  // Reopen modal on an unbound session:
  // 1) Importing a package with missing_voice_weights must auto-bind & render stage,
  //    but must NOT call /api/voice/switch or overwrite the missing-weights toast.
  dom.sessionModal.hidden = false;
  dom.sessionModal.classList.remove('hidden');
  session.settings.voiceProfileId = null;
  profiles = [{ id: 19, name: '缺权重小灯', system_prompt: '你是缺权重小灯。' }];
  const switchBeforeMissing = fetchCalls.filter((c) => c.url.includes('/api/voice/switch')).length;
  const countsBeforeMissing = getRenderCounts();
  dom.sVoiceImportZipInput.files = [z1];
  const pMissingUnbound = dom.sVoiceImportZipInput.fire('change');
  await tick();
  const missingUnboundXhr = xhrs.at(-1);
  missingUnboundXhr.status = 200;
  missingUnboundXhr.responseText = JSON.stringify({
    status: 'ok',
    imported: [{ name: '缺权重小灯', profile_id: 19, missing_voice_weights: true }],
  });
  missingUnboundXhr.onload();
  await pMissingUnbound;
  await tick();
  await tick();
  assert.equal(session.settings.voiceProfileId, 19);
  assert.equal(fetchCalls.filter((c) => c.url.includes('/api/voice/switch')).length, switchBeforeMissing, 'missing_voice_weights must not call /api/voice/switch');
  assert.equal(getRenderCounts().sidebar, countsBeforeMissing.sidebar + 1, 'must call renderSidebar on missing-weights auto-bind');
  assert.equal(getRenderCounts().header, countsBeforeMissing.header + 1, 'must call renderHeader on missing-weights auto-bind');
  assert.equal(toastRoot.children.at(-1)?.dataset.msg, '已导入「缺权重小灯」，但还不能出声，需要补上模型权重');

  // 2) Importing into an unbound session when /api/voice/switch fails must still call renderSidebar & renderHeader.
  session.settings.voiceProfileId = null;
  switchShouldFail = true;
  profiles = [{ id: 20, name: '无引擎小春', system_prompt: '你是无引擎小春。' }];
  const countsBeforeFailSwitch = getRenderCounts();
  dom.sVoiceImportZipInput.files = [z1];
  const pFailSwitch = dom.sVoiceImportZipInput.fire('change');
  await tick();
  const failSwitchXhr = xhrs.at(-1);
  failSwitchXhr.status = 200;
  failSwitchXhr.responseText = JSON.stringify({
    status: 'ok',
    imported: [{ name: '无引擎小春', profile_id: 20, missing_voice_weights: false }],
  });
  failSwitchXhr.onload();
  await pFailSwitch;
  await tick();
  await tick();
  switchShouldFail = false;
  assert.equal(session.settings.voiceProfileId, 20);
  assert.equal(getRenderCounts().sidebar, countsBeforeFailSwitch.sidebar + 1, 'must call renderSidebar even when /api/voice/switch fails');
  assert.ok(getRenderCounts().header > countsBeforeFailSwitch.header, 'must call renderHeader even when /api/voice/switch fails');

  // 3) Importing multiple packages into an unbound session binds only the first and says so clearly.
  session.settings.voiceProfileId = null;
  profiles = [
    { id: 21, name: 'A', system_prompt: '你是A' },
    { id: 22, name: 'B', system_prompt: '你是B' },
  ];
  dom.sVoiceImportZipInput.files = [z1];
  const pMulti = dom.sVoiceImportZipInput.fire('change');
  await tick();
  const multiXhr = xhrs.at(-1);
  multiXhr.status = 200;
  multiXhr.responseText = JSON.stringify({
    status: 'ok',
    imported: [
      { name: 'A', profile_id: 21, missing_voice_weights: false },
      { name: 'B', profile_id: 22, missing_voice_weights: false },
    ],
  });
  multiXhr.onload();
  await pMulti;
  await tick();
  await tick();
  assert.equal(session.settings.voiceProfileId, 21);
  assert.equal(toastRoot.children.at(-1)?.dataset.msg, '已导入「A」、「B」，已选用「A」，现在可以直接开始聊天');
} finally {
  globalThis.XMLHttpRequest = prevXHR; globalThis.window = prevWin;
  cleanup();
}
console.log('character_package_import simplified frontend tests: passed');
console.log('missing voice weights import hint: 1 scenario passed');
