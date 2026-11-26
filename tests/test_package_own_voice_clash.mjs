// A character package named like a voice the user made: the import asks, and "keep both" is the default.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { state, loadState, DEFAULT_SESSION_SETTINGS } from '../frontend/src/store.js';
import { initVoiceSettings, askOwnVoiceSameName } from '../frontend/src/controllers/voice_settings.js';

const store = new Map();
globalThis.localStorage = { getItem: (k) => store.get(k) ?? null, setItem: (k, v) => store.set(k, v) };
const tick = () => new Promise((r) => setTimeout(r, 0));
const mkEl = (val = '', classes = []) => {
  const ls = {}, set = new Set(classes);
  return {
    value: val, files: [], disabled: false, hidden: false, textContent: '', children: [], dataset: {}, style: { setProperty() {} },
    classList: { contains: (n) => set.has(n), add: (...a) => a.forEach((n) => set.add(n)), remove: (...a) => a.forEach((n) => set.delete(n)), toggle: (n, f) => (f === undefined ? !set.has(n) : f) ? (set.add(n), true) : (set.delete(n), false) },
    get options() { return this.children; },
    set innerHTML(h) { this.children = []; },
    addEventListener(n, fn) { (ls[n] ||= []).push(fn); },
    removeEventListener(n, fn) { ls[n] = (ls[n] || []).filter((h) => h !== fn); },
    async fire(n, ev = {}) { for (const fn of [...(ls[n] || [])]) await fn(ev); },
    click() { for (const fn of [...(ls.click || [])]) fn({}); },
    focus() { globalThis.document.activeElement = this; },
    appendChild(c) { this.children.push(c); },
    append(...cs) { this.children.push(...cs); },
    querySelector: () => null, querySelectorAll: () => [], remove() {},
  };
};

function fakeModalDocument() {
  const toastRoot = mkEl();
  const modal = mkEl(), inner = mkEl(), closeX = mkEl(), cancel = mkEl();
  modal.querySelector = (sel) => (sel === '.modal' ? inner : null);
  modal.querySelectorAll = (sel) => (sel === '[data-close]' ? [closeX, cancel] : []);
  const els = {
    toastRoot, ownVoiceClashModal: modal, ownVoiceClashText: mkEl(),
    ownVoiceClashKeep: mkEl(), ownVoiceClashReplace: mkEl(),
  };
  globalThis.document = { activeElement: null, getElementById: (id) => els[id] || null, createElement: () => mkEl() };
  return { ...els, closeX, cancel };
}

test('the dialog offers keep both first and focuses it', async () => {
  const d = fakeModalDocument();
  const choice = askOwnVoiceSameName({ names: ['爱丽丝'], suffix: '（自建）' });
  assert.equal(d.ownVoiceClashModal.hidden, false);
  assert.equal(globalThis.document.activeElement, d.ownVoiceClashKeep);
  assert.match(d.ownVoiceClashText.textContent, /你自己建的音色「爱丽丝」和这个角色包同名/);
  assert.match(d.ownVoiceClashText.textContent, /改名为「爱丽丝（自建）」/);
  d.ownVoiceClashKeep.click();
  assert.equal(await choice, 'keep_both');
  assert.equal(d.ownVoiceClashModal.hidden, true);
});

test('the dialog returns replace, and closing it cancels the import', async () => {
  let d = fakeModalDocument();
  const replace = askOwnVoiceSameName({ names: ['爱丽丝'] });
  d.ownVoiceClashReplace.click();
  assert.equal(await replace, 'replace');
  d = fakeModalDocument();
  const closed = askOwnVoiceSameName({ names: ['爱丽丝'] });
  d.closeX.click();
  assert.equal(await closed, null);
});

test('import retries with the choice and says the own voice was renamed', async () => {
  const toastRoot = mkEl();
  globalThis.document = { getElementById: (id) => (id === 'toastRoot' ? toastRoot : null), createElement: () => mkEl() };
  let confirms = 0;
  globalThis.window = { confirm: () => { confirms += 1; return true; } };
  globalThis.fetch = async () => ({ ok: true, status: 200, json: async () => ({ profiles: [], active_profile_id: null }) });
  const xhrs = [];
  globalThis.XMLHttpRequest = class {
    constructor() { this.upload = {}; xhrs.push(this); }
    open(m, u) { this.method = m; this.url = u; } setRequestHeader() {} send() {}
  };
  loadState();
  const session = { id: 's1', title: '测试', settings: { ...DEFAULT_SESSION_SETTINGS }, messages: [] };
  state.sessions = [session]; state.activeId = session.id;
  const ids = ['sessionModal', 'sSystem', 'sVoice', 'sVoiceActive', 'sVoiceImportZipInput', 'sVoiceImportDirInput', 'sVoiceRescan', 'sVoiceDelete', 'sCvName', 'sCvGpt', 'sCvSovits', 'sCvRef', 'sCvPromptText', 'sCvCreate', 'sVoiceImport', 'sVoiceImportFolder'];
  const dom = Object.assign(Object.fromEntries(ids.map((id) => [id, mkEl()])), {
    sVoiceDropOverlay: mkEl('', ['hidden']), sCustomVoiceBox: mkEl('', ['hidden']), sCvLang: mkEl('ja'),
  });
  dom.sessionModal.hidden = true;
  initVoiceSettings(dom, {});
  const zip = new Blob(['PK\x03\x04']); zip.name = 'alice.zip';
  dom.sVoiceImportZipInput.files = [zip];
  const done = dom.sVoiceImportZipInput.fire('change');
  await tick();
  xhrs[0].status = 409;
  xhrs[0].responseText = JSON.stringify({ code: 'own_voice_same_name', names: ['爱丽丝'], suffix: '（自建）' });
  xhrs[0].onload();
  await tick();
  assert.equal(confirms, 1, 'the first question already gives keep both');
  assert.equal(xhrs.length, 2);
  assert.match(xhrs[1].url, /same_name=keep_both/);
  xhrs[1].status = 200;
  xhrs[1].responseText = JSON.stringify({
    status: 'ok', imported: [{ name: '爱丽丝', profile_id: 9 }],
    kept_own_voices: [{ name: '爱丽丝', renamed_to: '爱丽丝（自建）' }],
  });
  xhrs[1].onload();
  await done;
  assert.match(toastRoot.children.at(-1)?.dataset.msg || '', /你自己建的「爱丽丝」已改名为「爱丽丝（自建）」/);
  for (const c of toastRoot.children) clearTimeout(c._timer);
});

test('ownVoiceClashModal uses dark modal styles and a bordered replace button', () => {
  const rd = (p) => readFileSync(new URL(p, import.meta.url), 'utf8');
  assert.match(rd('../frontend/index.html'), /class="btn (?:ghost|secondary)"[^>]*id="ownVoiceClashReplace"/);
  assert.match(rd('../frontend/src/interface.css'), /#ownVoiceClashModal[^}]*\.modal\s*\{/);
  assert.match(rd('../frontend/src/interface.css'), /#ownVoiceClashModal[^}]*\.btn\.ghost/);
  assert.match(rd('../frontend/src/adv/app.css'), /#ownVoiceClashModal:not\(\[hidden\]\)/);
});
