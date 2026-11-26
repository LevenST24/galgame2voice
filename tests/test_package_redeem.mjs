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
    set innerHTML(h) { this.children = h.includes('不指定（沿用语音服务当前的声音）') ? [{ value: '' }] : []; },
    addEventListener(n, fn) { (ls[n] ||= []).push(fn); },
    async fire(n, ev) {
      for (const fn of [...(ls[n] || [])]) {
        await fn(ev);
      }
    },
    appendChild(c) { this.children.push(c); },
    focused: false,
    focus() { this.focused = true; },
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
  if (u.includes('/redeem/list')) {
    return ok({ packages: [{ id: 'alice', name: '爱丽丝', size: 2048, remaining: downloads ? 1 : 2 }] });
  }
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
assert.equal(dom.sVoiceRedeemList.children[0].children[0].textContent, '爱丽丝 · 2.0 KB · 还可下载 1 次');

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
assert.equal(dom.sVoiceRedeemStatus.textContent, '兑换码格式无效，应为 G2V1.<地址>.<密钥>');
assert.equal([...store.values()].some((v) => String(v).includes(bad)), false);

// Empty input shows inline guidance and focuses the input box
dom.sVoiceRedeemCode.value = '   ';
dom.sVoiceRedeemCode.focused = false;
await dom.sVoiceRedeemQuery.fire('click');
assert.equal(dom.sVoiceRedeemCode.focused, true);
assert.equal(dom.sVoiceRedeemStatus.textContent, '请先粘贴兑换码（以 G2V1. 开头）。');

// While a query is in flight, the inline status shows progress until its answer arrives
let releaseQuery;
globalThis.fetch = async (url) => {
  if (String(url).includes('/redeem/list')) {
    await new Promise((resolve) => { releaseQuery = resolve; });
    return ok({ packages: [{ id: 'alice', name: '爱丽丝', size: 2048, remaining: 2 }] });
  }
  return ok({});
};
dom.sVoiceRedeemCode.value = 'G2V1.slow-query-code';
const pending = dom.sVoiceRedeemQuery.fire('click');
assert.equal(dom.sVoiceRedeemStatus.textContent, '正在查询兑换码…');
releaseQuery();
await pending;
assert.equal(dom.sVoiceRedeemStatus.textContent, '');

let listCalls = 0;
let prevented = false;
globalThis.fetch = async (url) => {
  if (String(url).includes('/redeem/list')) {
    listCalls += 1;
    return ok({ packages: [] });
  }
  return ok({});
};
const preventDefault = () => {
  prevented = true;
};
const keyEvent = (extra) => ({ key: 'Enter', isComposing: false, preventDefault, ...extra });
dom.sVoiceRedeemCode.value = 'G2V1.enter-query-code';
await dom.sVoiceRedeemCode.fire('keydown', keyEvent({ isComposing: true }));
await dom.sVoiceRedeemCode.fire('keydown', keyEvent({ keyCode: 229 }));
assert.equal(listCalls, 0);
assert.equal(prevented, false);
await dom.sVoiceRedeemCode.fire('keydown', keyEvent());
assert.equal(listCalls, 1);
assert.equal(prevented, true);
assert.equal(dom.sVoiceRedeemCode.value, '');
// Race 1: a slow earlier redeem query must not overwrite or clear a newer query's list.
let resolveSlowCode;
let rejectSlowCode;
let slowMode = 'resolve';
globalThis.fetch = async (url, opts = {}) => {
  const u = String(url);
  if (u.includes('/redeem/list')) {
    const reqCode = JSON.parse(opts.body).code;
    if (reqCode === 'G2V1.slow-old') {
      return new Promise((resolve, reject) => {
        resolveSlowCode = resolve;
        rejectSlowCode = reject;
      });
    }
    return ok({ packages: [{ id: 'bob', name: '鲍勃', size: 1024, remaining: 3 }] });
  }
  if (u.includes('/redeem/download')) {
    bodies.push(JSON.parse(opts.body));
    return ok({ status: 'ok', imported: [{ name: '鲍勃', profile_id: 12 }] });
  }
  if (u.includes('/profiles')) {
    return ok({ profiles: [{ id: 12, name: '鲍勃', system_prompt: '你是鲍勃' }], active_profile_id: 12 });
  }
  return ok({});
};
dom.sVoiceRedeemCode.value = 'G2V1.slow-old';
const slowQuery = dom.sVoiceRedeemQuery.fire('click');
dom.sVoiceRedeemCode.value = 'G2V1.fast-new';
await dom.sVoiceRedeemQuery.fire('click');
assert.equal(dom.sVoiceRedeemList.children[0].children[0].textContent, '鲍勃 · 1.0 KB · 还可下载 3 次');
resolveSlowCode(ok({ packages: [{ id: 'stale', name: '旧包', size: 512, remaining: 1 }] }));
await slowQuery;
assert.equal(
  dom.sVoiceRedeemList.children[0]?.children[0]?.textContent,
  '鲍勃 · 1.0 KB · 还可下载 3 次',
  '迟到的旧兑换码查询结果不应覆盖较新的角色包列表',
);

// Same race when the earlier query fails late: must not wipe the newer query's list.
dom.sVoiceRedeemCode.value = 'G2V1.slow-old';
const slowFailQuery = dom.sVoiceRedeemQuery.fire('click');
dom.sVoiceRedeemCode.value = 'G2V1.fast-new';
await dom.sVoiceRedeemQuery.fire('click');
rejectSlowCode(new Error('旧查询超时'));
await slowFailQuery;
assert.equal(
  dom.sVoiceRedeemList.children[0]?.children[0]?.textContent,
  '鲍勃 · 1.0 KB · 还可下载 3 次',
  '迟到的旧兑换码查询失败不应清空较新的角色包列表',
);

// Race 2: post-download list refresh from pkg1 must not wipe out pkg2's active download progress.
let resolvePostRefresh;
let resolvePkg2Download;
let listRefreshCount = 0;
globalThis.fetch = async (url, opts = {}) => {
  const u = String(url);
  if (u.includes('/redeem/list')) {
    listRefreshCount += 1;
    if (listRefreshCount === 1) {
      return ok({
        packages: [
          { id: 'pkg1', name: '角色一', size: 1024, remaining: 2 },
          { id: 'pkg2', name: '角色二', size: 2048, remaining: 2 },
        ],
      });
    }
    if (listRefreshCount === 2) {
      return new Promise(resolve => { resolvePostRefresh = resolve; });
    }
    return ok({
      packages: [
        { id: 'pkg1', name: '角色一', size: 1024, remaining: 1 },
        { id: 'pkg2', name: '角色二', size: 2048, remaining: 1 },
      ],
    });
  }
  if (u.includes('/redeem/download')) {
    const body = JSON.parse(opts.body);
    if (body.package_id === 'pkg1') {
      return ok({ status: 'ok', imported: [{ name: '角色一', profile_id: 21 }] });
    }
    return new Promise(resolve => { resolvePkg2Download = resolve; });
  }
  if (u.includes('/profiles')) {
    return ok({
      profiles: [
        { id: 21, name: '角色一', system_prompt: '一' },
        { id: 22, name: '角色二', system_prompt: '二' },
      ],
      active_profile_id: 21,
    });
  }
  return ok({});
};
dom.sVoiceRedeemCode.value = 'G2V1.two-packs';
await dom.sVoiceRedeemQuery.fire('click');
const pkg1Done = dom.sVoiceRedeemList.children[0].children[1].fire('click');
while (!resolvePostRefresh) await new Promise(r => setImmediate(r));
const pkg2Done = dom.sVoiceRedeemList.children[1].children[1].fire('click');
assert.match(dom.sVoiceRedeemStatus.textContent, /正在下载「角色二」/);
resolvePostRefresh(ok({
  packages: [
    { id: 'pkg1', name: '角色一', size: 1024, remaining: 1 },
    { id: 'pkg2', name: '角色二', size: 2048, remaining: 2 },
  ],
}));
await pkg1Done;
assert.match(
  dom.sVoiceRedeemStatus.textContent,
  /正在下载「角色二」/,
  '上一个包下载后的列表刷新不应抹掉下一个包正在显示的下载进度',
);
resolvePkg2Download(ok({ status: 'ok', imported: [{ name: '角色二', profile_id: 22 }] }));
await pkg2Done;
globalThis.fetch = async (u) => ok(String(u).includes('/redeem/list') ? { packages: [{ id: 'flaky', name: '断网', size: 2048, remaining: 0, resumable: true }] } : {});
dom.sVoiceRedeemCode.value = 'G2V1.resumable-code';
await dom.sVoiceRedeemQuery.fire('click');
assert.equal(dom.sVoiceRedeemList.children[0].children[0].textContent, '断网 · 2.0 KB · 可继续下载');
assert.equal(dom.sVoiceRedeemList.children[0].children[1].textContent, '继续下载');

// The server reports idle while it re-lists the packages before a download starts; the shown status stays.
{
  const realSetInterval = globalThis.setInterval;
  const realClearInterval = globalThis.clearInterval;
  let tick = null;
  globalThis.setInterval = (fn) => { tick = fn; return 1; };
  globalThis.clearInterval = () => { tick = null; };
  let finishDownload;
  globalThis.fetch = async (url) => {
    const u = String(url);
    if (u.includes('/redeem/list')) return ok({ packages: [{ id: 'carol', name: '卡罗尔', size: 4096, remaining: 2 }] });
    if (u.includes('/redeem/progress')) return ok({ phase: 'idle' });
    if (u.includes('/redeem/download')) return new Promise((resolve) => { finishDownload = resolve; });
    if (u.includes('/profiles')) return ok({ profiles: [{ id: 30, name: '卡罗尔', system_prompt: '' }], active_profile_id: 30 });
    return ok({});
  };
  dom.sVoiceRedeemCode.value = 'G2V1.idle-progress-code';
  await dom.sVoiceRedeemQuery.fire('click');
  const downloading = dom.sVoiceRedeemList.children[0].children[1].fire('click');
  const shown = dom.sVoiceRedeemStatus.textContent;
  assert.match(shown, /正在下载「卡罗尔」/);
  await tick();
  assert.equal(dom.sVoiceRedeemStatus.textContent, shown, 'an idle progress report must not blank the status while the download runs');
  finishDownload(ok({ status: 'ok', imported: [{ name: '卡罗尔', profile_id: 30 }] }));
  await downloading;
  globalThis.setInterval = realSetInterval;
  globalThis.clearInterval = realClearInterval;
}

toastRoot.children.forEach((c) => clearTimeout(c._timer));
console.log('package redeem frontend tests: passed');
