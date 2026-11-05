import test from 'node:test';
import assert from 'node:assert/strict';
import { formatUpdateStatus } from '../frontend/src/settings.js';
import {
  initUpdateManager,
  loadSystemVersionInfo,
  renderSystemVersionUI,
  handleApplyUpdate,
  handleCheckUpdate,
} from '../frontend/src/controllers/update_manager.js';

test('formatUpdateStatus handles build_pending correctly', () => {
  // 1. Pending build while Git is otherwise up to date
  const pendingData = {
    current_version: '2222222',
    latest_version: '2222222',
    current_branch: 'main',
    has_update: false,
    behind_count: 0,
    build_pending: true,
  };

  const status = formatUpdateStatus(pendingData);
  assert.equal(status.canUpdate, true, 'canUpdate must be true when build is pending');
  assert.equal(status.hasUpdate, false);
  assert.equal(status.badgeText, '待补部署');
  assert.match(status.title, /更新未完成/);
  assert.match(status.desc, /前端静态产物/);
  assert.doesNotMatch(status.title, /已是最新/);

  // 2. Pending build when newer remote commits also exist
  const pendingWithRemote = {
    current_version: '2222222',
    latest_version: '3333333',
    current_branch: 'main',
    has_update: true,
    behind_count: 1,
    build_pending: true,
  };

  const statusWithRemote = formatUpdateStatus(pendingWithRemote);
  assert.equal(statusWithRemote.canUpdate, true);
  assert.equal(statusWithRemote.hasUpdate, true);
  assert.match(statusWithRemote.badgeText, /待补构建/);
  assert.doesNotMatch(statusWithRemote.title, /已是最新/);
});

test('renderSystemVersionUI updates DOM correctly when build_pending is true', async () => {
  const noopBtn = () => ({
    disabled: false,
    className: '',
    textContent: '',
    addEventListener() {},
    classList: {
      classes: new Set(['hidden']),
      remove(cls) { this.classes.delete(cls); },
      add(cls) { this.classes.add(cls); },
      contains(cls) { return this.classes.has(cls); },
    },
  });

  const dom = {
    gDashVersionBadge: { className: '', textContent: '' },
    gCurrentCommit: { textContent: '' },
    gCurrentBranch: { textContent: '' },
    gCurrentDate: { textContent: '' },
    gRemoteUrl: { textContent: '', title: '' },
    gCurrentMsgText: { textContent: '' },
    gRemoteStatusTag: { className: '', textContent: '' },
    gUpdateNoticeBox: {
      classList: {
        classes: new Set(['hidden']),
        remove(cls) { this.classes.delete(cls); },
        add(cls) { this.classes.add(cls); },
        contains(cls) { return this.classes.has(cls); },
      },
    },
    gUpdateNoticeTitle: { textContent: '' },
    gUpdateNoticeDesc: { textContent: '' },
    gUpdateNoticeIcon: { textContent: '' },
    gBtnCheckUpdate: noopBtn(),
    gBtnApplyUpdate: noopBtn(),
    gBtnApplyUpdateText: { textContent: '' },
    gBtnCheckUpdateText: { textContent: '' },
    gBtnReloadPage: noopBtn(),
  };

  initUpdateManager(dom);

  const data = {
    current_version: '2222222',
    latest_version: '2222222',
    current_branch: 'main',
    has_update: false,
    behind_count: 0,
    build_pending: true,
  };

  renderSystemVersionUI(data);

  assert.equal(dom.gDashVersionBadge.textContent, '待补部署');
  assert.equal(dom.gDashVersionBadge.className, 'badge-status-pill badge-pill-yellow');
  assert.equal(dom.gBtnApplyUpdateText.textContent, '重试前端构建并更新');
  assert.equal(dom.gBtnApplyUpdate.disabled, false);
  assert.equal(dom.gUpdateNoticeBox.classList.contains('hidden'), false);
  assert.equal(dom.gUpdateNoticeIcon.textContent, '⚠️');
});

test('handleApplyUpdate and handleCheckUpdate lifecycle with failure -> pending recovery -> retry -> success', async () => {
  const originalFetch = globalThis.fetch;
  const originalConfirm = globalThis.confirm;
  const originalDocument = globalThis.document;

  const toastMessages = [];
  globalThis.confirm = () => true;
  globalThis.document = {
    getElementById(id) {
      return null;
    },
    createElement(tag) {
      return {
        className: '',
        dataset: {},
        classList: { add() {}, remove() {}, contains() { return false; } },
        append() {},
        appendChild() {},
        remove() {},
      };
    },
  };

  const noopBtn = () => ({
    disabled: false,
    className: '',
    textContent: '',
    addEventListener() {},
    classList: {
      classes: new Set(['hidden']),
      remove(cls) { this.classes.delete(cls); },
      add(cls) { this.classes.add(cls); },
      contains(cls) { return this.classes.has(cls); },
    },
  });

  const dom = {
    gDashVersionBadge: { className: '', textContent: '' },
    gCurrentCommit: { textContent: '' },
    gCurrentBranch: { textContent: '' },
    gCurrentDate: { textContent: '' },
    gRemoteUrl: { textContent: '', title: '' },
    gCurrentMsgText: { textContent: '' },
    gRemoteStatusTag: { className: '', textContent: '' },
    gUpdateNoticeBox: {
      classList: {
        classes: new Set(['hidden']),
        remove(cls) { this.classes.delete(cls); },
        add(cls) { this.classes.add(cls); },
        contains(cls) { return this.classes.has(cls); },
      },
    },
    gUpdateNoticeTitle: { textContent: '' },
    gUpdateNoticeDesc: { textContent: '' },
    gUpdateNoticeIcon: { textContent: '' },
    gBtnCheckUpdate: noopBtn(),
    gBtnApplyUpdate: noopBtn(),
    gBtnApplyUpdateText: { textContent: '' },
    gBtnCheckUpdateText: { textContent: '' },
    gBtnReloadPage: noopBtn(),
    gUpdateProgressBox: {
      classList: {
        classes: new Set(['hidden']),
        remove(cls) { this.classes.delete(cls); },
        add(cls) { this.classes.add(cls); },
        contains(cls) { return this.classes.has(cls); },
      },
    },
    gUpdateSpinner: {
      classList: {
        classes: new Set(['hidden']),
        remove(cls) { this.classes.delete(cls); },
        add(cls) { this.classes.add(cls); },
        contains(cls) { return this.classes.has(cls); },
      },
    },
    gUpdateProgressTitle: { textContent: '' },
    gUpdateLogOutput: { textContent: '' },
  };

  initUpdateManager(dom);

  let applyCallCount = 0;
  let isPending = false;

  globalThis.fetch = async (url, opts = {}) => {
    const urlStr = String(url);
    if (urlStr.includes('/api/system/update/apply')) {
      applyCallCount++;
      if (applyCallCount === 1) {
        isPending = true;
        return {
          ok: true,
          status: 200,
          json: async () => ({
            success: false,
            build_pending: true,
            rebuilt_frontend: false,
            restart_required: false,
            output: '前端静态产物构建失败 (建议手动排查):\nnpm ERR! Missing npm',
            current_version: '2222222',
            previous_version: '1111111',
            error: '前端静态产物构建失败 (npm run deploy 异常退出)。代码已拉取至 2222222，但静态资源未部署。',
          }),
        };
      }
      isPending = false;
      return {
        ok: true,
        status: 200,
        json: async () => ({
          success: true,
          build_pending: false,
          rebuilt_frontend: true,
          restart_required: true,
          output: '前端静态产物编译并部署成功。\nVite deployed 3 assets',
          current_version: '2222222',
          previous_version: '2222222',
          error: null,
        }),
      };
    }
    if (urlStr.includes('/api/system/version')) {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          current_version: '2222222',
          latest_version: '2222222',
          current_branch: 'main',
          has_update: false,
          behind_count: 0,
          build_pending: isPending,
          error: null,
          commits_log: [],
        }),
      };
    }
    return {
      ok: true,
      status: 200,
      json: async () => ({}),
    };
  };

  try {
    // 1. Initial attempt: build fails
    await handleApplyUpdate();

    // Reload button must NOT be shown on failure
    assert.equal(dom.gBtnReloadPage.classList.contains('hidden'), true);
    assert.match(dom.gUpdateProgressTitle.textContent, /更新未完成/);
    // Button text must show retry, not be overwritten to default
    assert.equal(dom.gBtnApplyUpdateText.textContent, '重试前端构建并更新');
    assert.equal(dom.gDashVersionBadge.textContent, '待补部署');

    // 2. handleCheckUpdate while build_pending is true and has_update is false
    // Should NOT falsely show "已是最新"
    await handleCheckUpdate();
    assert.equal(dom.gBtnApplyUpdateText.textContent, '重试前端构建并更新');
    assert.equal(dom.gDashVersionBadge.textContent, '待补部署');
    assert.match(dom.gUpdateNoticeTitle.textContent, /更新未完成/);

    // 3. Retry attempt: user fixed build environment, retry succeeds
    await handleApplyUpdate();

    assert.equal(applyCallCount, 2);
    // Reload button is now shown
    assert.equal(dom.gBtnReloadPage.classList.contains('hidden'), false);
    assert.match(dom.gUpdateProgressTitle.textContent, /更新完成/);
    assert.equal(dom.gBtnApplyUpdateText.textContent, '一键拉取并更新');
    assert.equal(dom.gDashVersionBadge.textContent, '最新版');

    // 4. Checking update after success: normal clean status
    await handleCheckUpdate();
    assert.equal(dom.gDashVersionBadge.textContent, '最新版');
    assert.equal(dom.gBtnApplyUpdateText.textContent, '一键拉取并更新');
  } finally {
    globalThis.fetch = originalFetch;
    globalThis.confirm = originalConfirm;
    globalThis.document = originalDocument;
  }
});

test('loadSystemVersionInfo(false) after remote update check or failed git pull preserves detected update badge and apply button', async () => {
  const originalFetch = globalThis.fetch;
  const originalConfirm = globalThis.confirm;
  const originalDocument = globalThis.document;

  const confirmPrompts = [];
  globalThis.confirm = (msg) => {
    confirmPrompts.push(msg);
    return true;
  };
  globalThis.document = {
    getElementById() {
      return null;
    },
    createElement() {
      return {
        className: '',
        dataset: {},
        textContent: '',
        classList: { add() {}, remove() {}, contains() { return false; } },
        append() {},
        appendChild() {},
        remove() {},
      };
    },
  };

  const makeBox = () => ({
    disabled: false,
    className: '',
    textContent: '',
    addEventListener() {},
    classList: {
      classes: new Set(['hidden']),
      remove(cls) { this.classes.delete(cls); },
      add(cls) { this.classes.add(cls); },
      contains(cls) { return this.classes.has(cls); },
    },
  });

  const dom = {
    gDashVersionBadge: { className: '', textContent: '' },
    gDashVersionCommit: { textContent: '' },
    gDashVersionBranch: { textContent: '' },
    gCurrentCommit: { textContent: '-' },
    gCurrentBranch: { textContent: '' },
    gCurrentDate: { textContent: '' },
    gRemoteUrl: { textContent: '', title: '' },
    gCurrentMsgText: { textContent: '' },
    gRemoteStatusTag: { className: '', textContent: '待检查' },
    gUpdateNoticeBox: makeBox(),
    gUpdateNoticeTitle: { textContent: '' },
    gUpdateNoticeDesc: { textContent: '' },
    gUpdateNoticeIcon: { textContent: '' },
    gCommitsLogContainer: makeBox(),
    gCommitsLogList: { innerHTML: '', appendChild() {} },
    gBtnCheckUpdate: makeBox(),
    gBtnCheckUpdateText: { textContent: '检查更新' },
    gBtnApplyUpdate: makeBox(),
    gBtnApplyUpdateText: { textContent: '一键拉取并更新' },
    gBtnReloadPage: makeBox(),
    gUpdateProgressBox: makeBox(),
    gUpdateSpinner: makeBox(),
    gUpdateProgressTitle: { textContent: '' },
    gUpdateLogOutput: { textContent: '' },
  };

  initUpdateManager(dom);

  globalThis.fetch = async (url) => {
    const u = String(url);
    if (u.includes('/api/system/version?check_remote=true')) {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          current_version: '1111111',
          latest_version: '3333333',
          current_branch: 'main',
          has_update: true,
          behind_count: 2,
          build_pending: false,
          error: null,
          commits_log: ['3333333 feat: new feature (2026-11-05)', '2222222 fix: bug (2026-11-04)'],
        }),
      };
    }
    if (u.includes('/api/system/version?check_remote=false')) {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          current_version: '1111111',
          latest_version: '1111111',
          current_branch: 'main',
          has_update: false,
          behind_count: 0,
          build_pending: false,
          error: null,
          commits_log: [],
        }),
      };
    }
    if (u.includes('/api/system/update/apply')) {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          success: false,
          build_pending: false,
          rebuilt_frontend: false,
          restart_required: false,
          output: 'git pull 失败: 无法连接 GitHub',
          current_version: '1111111',
          previous_version: '1111111',
          error: 'git pull 失败: 无法连接 GitHub',
        }),
      };
    }
    return { ok: true, status: 200, json: async () => ({}) };
  };

  try {
    await handleCheckUpdate();
    assert.equal(dom.gDashVersionBadge.textContent, '有更新 (2)');
    assert.equal(dom.gRemoteStatusTag.textContent, '落后 2 个提交');
    assert.equal(dom.gBtnApplyUpdate.className, 'btn primary');
    assert.equal(dom.gBtnApplyUpdateText.textContent, '一键拉取并更新 (2 个新提交)');
    assert.equal(dom.gCommitsLogContainer.classList.contains('hidden'), false);

    // Reopening Settings Center triggers loadSystemVersionInfo(false)
    await loadSystemVersionInfo(false);
    assert.equal(dom.gDashVersionBadge.textContent, '有更新 (2)');
    assert.equal(dom.gRemoteStatusTag.textContent, '落后 2 个提交');
    assert.equal(dom.gBtnApplyUpdate.className, 'btn primary');
    assert.equal(dom.gBtnApplyUpdateText.textContent, '一键拉取并更新 (2 个新提交)');
    assert.equal(dom.gCommitsLogContainer.classList.contains('hidden'), false);

    // Failed git pull triggers loadSystemVersionInfo(false)
    await handleApplyUpdate();
    assert.equal(confirmPrompts.length, 1);
    assert.doesNotMatch(confirmPrompts[0], /当前本地版本已是最新/);
    assert.equal(dom.gDashVersionBadge.textContent, '有更新 (2)');
    assert.equal(dom.gRemoteStatusTag.textContent, '落后 2 个提交');
    assert.equal(dom.gBtnApplyUpdate.className, 'btn primary');
    assert.equal(dom.gBtnApplyUpdateText.textContent, '一键拉取并更新 (2 个新提交)');
    assert.doesNotMatch(dom.gUpdateNoticeTitle.textContent, /当前已是最新版本/);
  } finally {
    globalThis.fetch = originalFetch;
    globalThis.confirm = originalConfirm;
    globalThis.document = originalDocument;
  }
});

test('handleCheckUpdate hides stale up-to-date notice box and shows error toast when request fails', async () => {
  const originalFetch = globalThis.fetch;
  const originalDocument = globalThis.document;
  const originalError = console.error;
  const originalWarn = console.warn;

  const toasts = [];
  const toastRoot = {
    querySelectorAll: () => [],
    appendChild(el) {
      toasts.push({ msg: el.dataset?.msg || el.textContent, className: el.className });
      queueMicrotask(() => clearTimeout(el._timer));
    },
  };
  globalThis.document = {
    getElementById: (id) => (id === 'toastRoot' ? toastRoot : null),
    createElement: () => ({
      className: '',
      dataset: {},
      textContent: '',
      classList: { add() {}, remove() {}, contains() { return false; } },
      append(...nodes) {
        this.textContent = nodes.map((n) => (typeof n === 'string' ? n : n.textContent || '')).join('');
      },
      appendChild() {},
      remove() {},
    }),
  };
  console.error = () => {};
  console.warn = () => {};

  const makeBox = () => ({
    disabled: false,
    className: '',
    textContent: '',
    addEventListener() {},
    classList: {
      classes: new Set(['hidden']),
      remove(cls) { this.classes.delete(cls); },
      add(cls) { this.classes.add(cls); },
      contains(cls) { return this.classes.has(cls); },
    },
  });

  const dom = {
    gDashVersionBadge: { className: '', textContent: '' },
    gCurrentCommit: { textContent: '-' },
    gCurrentBranch: { textContent: '' },
    gRemoteStatusTag: { className: '', textContent: '待检查' },
    gUpdateNoticeBox: makeBox(),
    gUpdateNoticeTitle: { textContent: '' },
    gUpdateNoticeDesc: { textContent: '' },
    gUpdateNoticeIcon: { textContent: '' },
    gBtnCheckUpdate: makeBox(),
    gBtnCheckUpdateText: { textContent: '检查更新' },
    gBtnApplyUpdate: makeBox(),
    gBtnApplyUpdateText: { textContent: '一键拉取并更新' },
  };

  initUpdateManager(dom);

  try {
    globalThis.fetch = async () => ({
      ok: true,
      status: 200,
      json: async () => ({
        current_version: '1111111',
        latest_version: '1111111',
        current_branch: 'main',
        has_update: false,
        behind_count: 0,
        build_pending: false,
        error: null,
        commits_log: [],
      }),
    });

    await handleCheckUpdate();
    assert.equal(dom.gUpdateNoticeBox.classList.contains('hidden'), false);
    assert.equal(dom.gUpdateNoticeTitle.textContent, '当前已是最新版本');
    toasts.length = 0;

    globalThis.fetch = async () => {
      throw new TypeError('Failed to fetch');
    };

    await handleCheckUpdate();
    assert.equal(dom.gRemoteStatusTag.textContent, '读取受阻');
    assert.equal(dom.gUpdateNoticeBox.classList.contains('hidden'), true);
    assert.equal(toasts.length, 1);
    assert.equal(toasts[0].msg, '检查更新失败：暂时无法连接服务，请检查网络连接后重试');
    assert.match(toasts[0].className, /toast-error/);
  } finally {
    globalThis.fetch = originalFetch;
    globalThis.document = originalDocument;
    console.error = originalError;
    console.warn = originalWarn;
  }
});
