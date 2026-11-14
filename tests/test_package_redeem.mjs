import assert from 'node:assert/strict';
import { state, loadState, DEFAULT_SESSION_SETTINGS } from '../frontend/src/store.js';
import { initVoiceSettings, formatRedeemProgress } from '../frontend/src/controllers/voice_settings.js';

const done = Math.round(1.2 * 1024 ** 3);
const total = Math.round(3.4 * 1024 ** 3);
assert.equal(
  formatRedeemProgress({ phase: 'download', done, total, name: '爱丽丝' }),
  '正在下载「爱丽丝」… 35%（1.2 / 3.4 GB）',
);
assert.equal(formatRedeemProgress({ phase: 'import', name: '爱丽丝' }), '正在导入…');

const store = new Map();
globalThis.localStorage = {
  getItem: (k) => store.get(k) ?? null,
  setItem: (k, v) => store.set(k, v),
  removeItem: (k) => store.delete(k),
};
const mkEl = (val = '') => {
  const ls = {};
  const set = new Set();
  return {
    value: val, files: [], disabled: false, textContent: '', children: [], dataset: {}, style: { setProperty() {} },
    classList: {
      contains: (n) => set.has(n),
      add: (...a) => a.forEach((n) => set.add(n)),
      remove: (...a) => a.forEach((n) => set.delete(n)),
      toggle: (n, f) => ((f === undefined ? !set.has(n) : f) ? set.add(n) : set.delete(n), set.has(n)),
    },
    set innerHTML(h) { this.children = h.includes('跟随后端当前音色') ? [{ value: '', textContent: '跟随后端当前音色' }] : []; },
    addEventListener(n, fn) { (ls[n] ||= []).push(fn); },
    async fire(n) { for (const fn of [...(ls[n] || [])]) await fn(); },
    appendChild(c) { this.children.push(c); },
    querySelectorAll: () => [],
    remove() {},
  };
};
const toastRoot = mkEl();
globalThis.document = {
  getElementById: (id) => (id === 'toastRoot' ? toastRoot : null),
  createElement: () => mkEl(),
};
loadState();
const session = { id: 's1', title: '测试', settings: { ...DEFAULT_SESSION_SETTINGS }, messages: [] };
state.sessions = [session];
state.activeId = session.id;
const ids = [
  'sessionModal', 'sSystem', 'sVoice', 'sVoiceActive', 'sVoiceImportZipInput', 'sVoiceImportDirInput',
  'sVoiceRescan', 'sVoiceDelete', 'sCvName', 'sCvGpt', 'sCvSovits', 'sCvRef', 'sCvPromptText', 'sCvCreate',
  'sVoiceRedeem', 'sVoiceRedeemPanel', 'sVoiceRedeemCode', 'sVoiceRedeemQuery', 'sVoiceRedeemList',
  'sVoiceRedeemStatus',
];
const dom = Object.assign(Object.fromEntries(ids.map((id) => [id, mkEl()])), {
  sVoiceImport: mkEl(), sVoiceImportFolder: mkEl(), sVoiceDropOverlay: mkEl(),
  sCustomVoiceBox: mkEl(), sCvLang: mkEl('ja'),
});
initVoiceSettings(dom);
const code = 'G2V1.sample-code-value';
let downloads = 0;
let firstStatus = '';
let sawImport = '';
let confirmed = '';
const bodies = [];
globalThis.window = {
  confirm: (msg) => {
    confirmed = msg;
    return true;
  },
};
const ok = (data, status = 200) => ({ ok: status >= 200 && status < 300, status, json: async () => data });
globalThis.fetch = async (url, opts = {}) => {
  const u = String(url);
  if (u.includes('/redeem/list')) return ok({ packages: [{ id: 'alice', name: '爱丽丝', size: 2048, remaining: 2 }] });
  if (u.includes('/redeem/progress')) return ok({ phase: 'download', done, total, name: '爱丽丝' });
  if (u.includes('/redeem/download')) {
    bodies.push(JSON.parse(opts.body));
    downloads += 1;
    if (downloads === 1) {
      firstStatus = dom.sVoiceRedeemStatus.textContent;
      return ok({ code: 'package_exists', names: ['爱丽丝'], detail: '已有同名角色包：爱丽丝' }, 409);
    }
    sawImport = dom.sVoiceRedeemStatus.textContent;
    return ok({ status: 'ok', imported: [{ name: '爱丽丝', profile_id: 8 }] });
  }
  if (u.includes('/profiles')) {
    return ok({ profiles: [{ id: 8, name: '爱丽丝', system_prompt: '你是爱丽丝' }], active_profile_id: 8 });
  }
  return ok({});
};
dom.sVoiceRedeemCode.value = code;
await dom.sVoiceRedeemQuery.fire('click');
assert.equal(dom.sVoiceRedeemCode.value, '');
assert.equal([...store.values()].some((v) => String(v).includes(code)), false);
assert.equal(dom.sVoiceRedeemList.children[0].children[0].textContent, '爱丽丝 · 2.0 KB · 还可下载 2 次');
assert.equal(dom.sVoiceRedeemList.children[0].children[1].textContent, '下载并导入');
await dom.sVoiceRedeemList.children[0].children[1].fire('click');
assert.equal(firstStatus, '正在下载「爱丽丝」… 0%（0.0 / 2.0 KB）');
assert.equal(sawImport, '正在导入…');
assert.equal(dom.sVoiceRedeemStatus.textContent, '');
assert.equal(confirmed, '已有同名角色包「爱丽丝」，覆盖它吗？旧的会先备份。');
assert.equal(bodies[0].overwrite, false);
assert.equal(bodies[1].overwrite, true);
assert.equal(bodies[0].code, code);
assert.equal(dom.sVoice.value, '8');
assert.equal(dom.sSystem.value, '你是爱丽丝');

globalThis.fetch = async (url) => (String(url).includes('/redeem/list') ? ok({ packages: [] }) : ok({}));
dom.sVoiceRedeemCode.value = 'G2V1.empty-list-code';
await dom.sVoiceRedeemQuery.fire('click');
assert.equal(dom.sVoiceRedeemCode.value, '');
assert.equal(dom.sVoiceRedeemStatus.textContent, '这个兑换码下没有可下载的角色包。');

const bad = 'G2V1.missing-tail';
globalThis.fetch = async (url) => (
  String(url).includes('/redeem/list') ? ok({ detail: '兑换码格式无效，应为 G2V1.<地址>.<密钥>' }, 400) : ok({})
);
dom.sVoiceRedeemCode.value = bad;
await dom.sVoiceRedeemQuery.fire('click');
assert.equal(dom.sVoiceRedeemCode.value, bad);
assert.equal([...store.values()].some((v) => String(v).includes(bad)), false);
toastRoot.children.forEach((c) => clearTimeout(c._timer));
console.log('package redeem frontend tests: passed');
