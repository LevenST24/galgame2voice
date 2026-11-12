import assert from 'node:assert/strict';
import fs from 'node:fs';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

import { fakeDocument } from './helpers/adv_dom.mjs';
import * as apiModule from '../frontend/src/api.js';
import * as settingsModule from '../frontend/src/settings.js';
import * as updateManagerModule from '../frontend/src/controllers/update_manager.js';

const INDEX_HTML_PATH = fileURLToPath(new URL('../frontend/index.html', import.meta.url));
const RELEASES_URL = 'https://github.com/LevenST24/galgame2voice/releases';

function extractGpUpdateSection(html) {
  const start = html.indexOf('<div class="gtab-panel hidden" id="gp-update">');
  const end = html.indexOf('<footer class="modal-foot">', start);
  assert.ok(start >= 0 && end > start, 'gp-update section must exist in frontend/index.html');
  return html.slice(start, end);
}

function buildUpdatePanelDomFromHtml(doc, html) {
  const gpUpdateHtml = extractGpUpdateSection(html);

  const toastRoot = doc.createElement('div');
  toastRoot.id = 'toastRoot';
  doc.body.appendChild(toastRoot);

  const gTabs = doc.createElement('div');
  gTabs.id = 'gTabs';
  const updateTabBtn = doc.createElement('button');
  updateTabBtn.setAttribute('data-gtab', 'update');
  updateTabBtn.dataset.gtab = 'update';
  gTabs.appendChild(updateTabBtn);
  doc.body.appendChild(gTabs);

  const panel = doc.createElement('div');
  panel.id = 'gp-update';
  doc.body.appendChild(panel);

  const elementsById = {};
  const openTagWithIdRe = /<([a-zA-Z0-9]+)\b([^>]*\bid="([^"]+)"[^>]*)\/?>/g;
  for (const match of gpUpdateHtml.matchAll(openTagWithIdRe)) {
    const [, tag, attrs, id] = match;
    if (id === 'gp-update') continue;
    const el = doc.createElement(tag);
    el.id = id;
    const cls = attrs.match(/\bclass="([^"]*)"/);
    if (cls) el.className = cls[1];
    const href = attrs.match(/\bhref="([^"]*)"/);
    if (href) {
      el.href = href[1];
      el.setAttribute('href', href[1]);
    }
    const target = attrs.match(/\btarget="([^"]*)"/);
    if (target) el.setAttribute('target', target[1]);
    const rel = attrs.match(/\brel="([^"]*)"/);
    if (rel) el.setAttribute('rel', rel[1]);
    panel.appendChild(el);
    elementsById[id] = el;
  }

  return {
    toastRoot,
    updateTabBtn,
    dom: {
      gTabs,
      gDashVersionBadge: doc.createElement('span'),
      gDashVersionCommit: doc.createElement('div'),
      gDashVersionBranch: doc.createElement('div'),
      gBtnGoUpdateTab: doc.createElement('button'),
      ...elementsById,
    },
  };
}

function jsonResponse(payload, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => payload,
  };
}

test('index.html replaces in-app update button and terminal with external GitHub Releases link', () => {
  const html = fs.readFileSync(INDEX_HTML_PATH, 'utf-8');
  assert.equal(html.includes('id="gBtnApplyUpdate"'), false, 'gBtnApplyUpdate must be removed');
  assert.equal(html.includes('id="gUpdateProgressBox"'), false, 'gUpdateProgressBox must be removed');
  assert.equal(html.includes('id="gCommitsLogContainer"'), false, 'gCommitsLogContainer must be removed');
  assert.equal(html.includes('id="gBtnReloadPage"'), false, 'gBtnReloadPage must be removed');

  assert.match(
    html,
    /<a\s+[^>]*id="gReleaseDownloadLink"[^>]*href="https:\/\/github\.com\/LevenST24\/galgame2voice\/releases"[^>]*target="_blank"[^>]*rel="noopener noreferrer"[^>]*>前往下载页<\/a>/,
  );

  assert.equal('checkSystemUpdate' in apiModule, false, 'checkSystemUpdate must be removed from api.js');
  assert.equal('applySystemUpdate' in apiModule, false, 'applySystemUpdate must be removed from api.js');
  assert.equal('renderCommitsLog' in settingsModule, false, 'renderCommitsLog must be removed from settings.js');
});

test('formatUpdateStatus covers all 5 notice states in Chinese', () => {
  const { formatUpdateStatus } = settingsModule;

  const empty = formatUpdateStatus(null);
  assert.equal(empty.tagText, '未检查');
  assert.equal(empty.title, '尚未检查新版本');
  assert.equal(empty.desc, '点击「检查新版本」查询 GitHub Releases 最新正式版。');

  const blocked = formatUpdateStatus({
    current_version: '3.1.2',
    current_branch: 'source',
    has_update: false,
    error: '无法连接 GitHub Releases，请检查网络或直接前往下载页查看。',
  });
  assert.equal(blocked.tagText, '检查受阻');
  assert.equal(blocked.title, '暂时无法获取最新版本信息');
  assert.equal(
    blocked.desc,
    '无法连接 GitHub Releases，请检查网络或直接前往下载页查看。 可直接点击「前往下载页」查看发布包。',
  );

  const sourceUpdate = formatUpdateStatus({
    current_version: '3.1.2',
    current_branch: 'source',
    has_update: true,
    latest_version: '9.9.0',
  });
  assert.equal(sourceUpdate.tagText, '发现新版本 v9.9.0');
  assert.equal(sourceUpdate.title, '发现新版本 v9.9.0');
  assert.equal(
    sourceUpdate.desc,
    '当前版本为 v3.1.2。请点击「前往下载页」查看更新说明并下载新版发布包，或在终端使用 git pull 手动更新源码。',
  );

  const portableUpdate = formatUpdateStatus({
    current_version: '3.1.2',
    current_branch: 'portable',
    has_update: true,
    latest_version: '9.9.0',
  });
  assert.equal(portableUpdate.tagText, '发现新版本 v9.9.0');
  assert.equal(
    portableUpdate.desc,
    '当前版本为 v3.1.2。请点击「前往下载页」下载新版便携包；解压到新文件夹后，将旧目录中的 data、audio、characters、backgrounds（及个人 .env）复制过去即可保留全部数据。',
  );

  const latest = formatUpdateStatus({
    current_version: '3.1.2',
    current_branch: 'source',
    has_update: false,
    latest_version: '3.1.2',
  });
  assert.equal(latest.tagText, '已是最新');
  assert.equal(latest.title, '当前已是最新版本');
  assert.equal(latest.desc, '当前版本 v3.1.2 已是最新正式版（远端最新 v3.1.2）。');

  const localOnly = formatUpdateStatus({
    current_version: '3.1.2',
    current_branch: 'portable',
    has_update: false,
    latest_version: null,
  });
  assert.equal(localOnly.tagText, '便携版');
  assert.equal(localOnly.title, '当前版本 v3.1.2');
  assert.equal(localOnly.desc, '点击「检查新版本」查询是否有新发布的版本，或点击「前往下载页」浏览历史发布包。');
});

async function untilIdle(button) {
  for (let i = 0; i < 50 && button.disabled; i += 1) await new Promise(resolve => setTimeout(resolve, 0));
  assert.equal(button.disabled, false, 'the update check never finished');
}

test('update panel shows the local version, then the GitHub Releases result with a download link', async () => {
  const html = fs.readFileSync(INDEX_HTML_PATH, 'utf-8');
  const prevDoc = globalThis.document;
  const prevFetch = globalThis.fetch;
  const origError = console.error;
  const doc = fakeDocument();
  globalThis.document = doc;
  console.error = () => {};

  const { toastRoot, updateTabBtn, dom } = buildUpdatePanelDomFromHtml(doc, html);
  let tabClicks = 0;
  updateTabBtn.addEventListener('click', () => { tabClicks += 1; });
  const requestedUrls = [];
  let remotePayload = null;
  const lastToast = () => toastRoot.children.at(-1).dataset.msg;

  globalThis.fetch = async (url) => {
    const target = String(url);
    requestedUrls.push(target);
    if (target.endsWith('/api/system/version?check_remote=false')) {
      return jsonResponse({
        current_version: '3.1.2',
        current_branch: 'source',
        remote_url: RELEASES_URL,
        release_url: RELEASES_URL,
        latest_version: null,
        has_update: false,
        error: null,
      });
    }
    if (target.endsWith('/api/system/version?check_remote=true')) return jsonResponse(remotePayload);
    throw new Error(`Unexpected fetch URL: ${target}`);
  };

  const controller = updateManagerModule.createUpdateManagerController(dom);
  try {
    // 1. Opening settings reads the local version only; the download link points at the Releases page.
    await controller.loadSystemVersionInfo(false);
    assert.deepEqual(requestedUrls, ['/api/system/version?check_remote=false']);
    assert.equal(dom.gCurrentCommit.textContent, 'v3.1.2');
    assert.equal(dom.gCurrentBranch.textContent, '源码运行');
    assert.equal(dom.gRemoteStatusTag.textContent, '本地版本');
    assert.equal(dom.gUpdateNoticeTitle.textContent, '当前版本 v3.1.2');
    assert.equal(dom.gReleaseDownloadLink.href, RELEASES_URL);

    // 2. "检查新版本" asks GitHub and points the link at the new release.
    remotePayload = {
      current_version: '3.1.2',
      current_branch: 'source',
      remote_url: RELEASES_URL,
      release_url: `${RELEASES_URL}/tag/v9.9.0`,
      latest_version: '9.9.0',
      has_update: true,
      error: null,
    };
    dom.gBtnCheckUpdate.click();
    await untilIdle(dom.gBtnCheckUpdate);
    assert.equal(requestedUrls.at(-1), '/api/system/version?check_remote=true');
    assert.equal(dom.gRemoteStatusTag.textContent, '发现新版本 v9.9.0');
    assert.equal(dom.gDashVersionBadge.textContent, '发现新版本 v9.9.0');
    assert.equal(dom.gUpdateNoticeTitle.textContent, '发现新版本 v9.9.0');
    assert.equal(
      dom.gUpdateNoticeDesc.textContent,
      '当前版本为 v3.1.2。请点击「前往下载页」查看更新说明并下载新版发布包，或在终端使用 git pull 手动更新源码。',
    );
    assert.equal(dom.gReleaseDownloadLink.href, `${RELEASES_URL}/tag/v9.9.0`);
    assert.equal(dom.gBtnCheckUpdateText.textContent, '检查新版本');
    assert.equal(lastToast(), '发现新版本 v9.9.0');

    // 3. Reopening settings re-reads only the local version and must not forget that result.
    await controller.loadSystemVersionInfo(false);
    assert.equal(requestedUrls.at(-1), '/api/system/version?check_remote=false');
    assert.equal(dom.gRemoteStatusTag.textContent, '发现新版本 v9.9.0');
    assert.equal(dom.gReleaseDownloadLink.href, `${RELEASES_URL}/tag/v9.9.0`);

    // 4. The dashboard's "查看更新" opens the update tab and checks right away.
    remotePayload = { ...remotePayload, latest_version: '3.1.2', release_url: RELEASES_URL, has_update: false };
    dom.gBtnGoUpdateTab.click();
    await untilIdle(dom.gBtnCheckUpdate);
    assert.equal(tabClicks, 1);
    assert.equal(requestedUrls.at(-1), '/api/system/version?check_remote=true');
    assert.equal(dom.gRemoteStatusTag.textContent, '已是最新');
    assert.equal(dom.gUpdateNoticeTitle.textContent, '当前已是最新版本');
    assert.equal(lastToast(), '当前已是最新版本');

    // 5. The portable build is told how to move its data to the new package.
    remotePayload = {
      ...remotePayload,
      current_branch: 'portable',
      latest_version: '9.9.0',
      release_url: `${RELEASES_URL}/tag/v9.9.0`,
      has_update: true,
    };
    dom.gBtnCheckUpdate.click();
    await untilIdle(dom.gBtnCheckUpdate);
    assert.equal(dom.gCurrentBranch.textContent, '便携版');
    assert.match(dom.gUpdateNoticeDesc.textContent, /data、audio、characters、backgrounds/);

    // 6. A GitHub error keeps the download link usable, and a link outside this repository is ignored.
    remotePayload = {
      current_version: '3.1.2',
      current_branch: 'source',
      remote_url: RELEASES_URL,
      release_url: 'https://untrusted.example/phish',
      latest_version: null,
      has_update: false,
      error: 'GitHub 接口请求受限，请稍后再试或直接前往下载页查看。',
    };
    dom.gBtnCheckUpdate.click();
    await untilIdle(dom.gBtnCheckUpdate);
    assert.equal(dom.gRemoteStatusTag.textContent, '检查受阻');
    assert.equal(dom.gUpdateNoticeTitle.textContent, '暂时无法获取最新版本信息');
    assert.equal(
      dom.gUpdateNoticeDesc.textContent,
      'GitHub 接口请求受限，请稍后再试或直接前往下载页查看。 可直接点击「前往下载页」查看发布包。',
    );
    assert.equal(dom.gReleaseDownloadLink.href, RELEASES_URL);
    assert.equal(dom.gReleaseDownloadLink.hidden, false);
    assert.equal(dom.gReleaseDownloadLink.classList.contains('hidden'), false);
    assert.equal(lastToast(), '检查更新失败：暂时无法获取最新版本信息，请检查网络连接后重试');
  } finally {
    for (const toast of [...toastRoot.children]) clearTimeout(toast._timer);
    console.error = origError;
    globalThis.fetch = prevFetch;
    globalThis.document = prevDoc;
  }
});
