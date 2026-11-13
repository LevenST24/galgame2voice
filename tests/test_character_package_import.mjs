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
    set innerHTML(h) { this.children = h.includes('跟随后端当前音色') ? [{ value: '', textContent: '跟随后端当前音色' }] : []; },
    addEventListener(n, fn) { (ls[n] ||= []).push(fn); },
    removeEventListener(n, fn) { ls[n] = (ls[n] || []).filter((h) => h !== fn); },
    async fire(n, ev = {}) { for (const fn of [...(ls[n] || [])]) await fn(ev); },
    appendChild(c) { this.children.push(c); },
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
  initVoiceSettings(dom);
  return { dom, session, toastRoot, cleanup: () => toastRoot.children.forEach((c) => clearTimeout(c._timer)) };
}

// 2. First-use hint toggle + 409 confirm -> overwrite=1 & select new char; cancel -> no retry + failure toast
const xhrs = [];
const prevXHR = globalThis.XMLHttpRequest, prevWin = globalThis.window;
globalThis.XMLHttpRequest = class {
  constructor() { this.upload = {}; xhrs.push(this); }
  open(m, u) { this.method = m; this.url = u; } setRequestHeader() {} send() {}
};

const { dom, session, toastRoot, cleanup } = setupEnv();
let profiles = [], confirmResult = false;
globalThis.window = { confirm: () => confirmResult };
globalThis.fetch = async (url) => ({
  ok: true, status: 200,
  json: async () => (String(url).endsWith('/profiles') ? { profiles, active_profile_id: profiles[0]?.id ?? null } : {}),
});

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

  // Failure response -> error toast
  dom.sVoiceImportZipInput.files = [z1];
  const pFail = dom.sVoiceImportZipInput.fire('change');
  await tick();
  xhrs[3].status = 422; xhrs[3].responseText = JSON.stringify({ detail: '缺少参考音频' });
  xhrs[3].onload();
  await pFail;
  assert.equal(toastRoot.children.at(-1)?.dataset.msg, '缺少参考音频');
  assert.match(toastRoot.children.at(-1)?.className || '', /toast-error/);
} finally {
  globalThis.XMLHttpRequest = prevXHR; globalThis.window = prevWin;
  cleanup();
}
console.log('character_package_import simplified frontend tests: passed');
