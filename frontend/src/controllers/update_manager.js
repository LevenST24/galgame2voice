// Update Manager Controller: shows the running version, checks GitHub Releases for a newer one,
// and links to the release download page. It never updates anything from inside the page.
import { fetchSystemVersion } from '../api.js';
import { formatUpdateStatus } from '../settings.js';
import { showToast } from '../ui.js';

const RELEASES_URL = 'https://github.com/LevenST24/galgame2voice/releases';
const RELEASES_URL_PREFIX = 'https://github.com/LevenST24/galgame2voice/';

const releaseHref = (url) => (typeof url === 'string' && url.startsWith(RELEASES_URL_PREFIX) ? url : RELEASES_URL);

function noticeIcon(data) {
  if (data.error) return '⚠️';
  if (data.has_update) return '🚀';
  return data.latest_version ? '✅' : 'ℹ️';
}

export function createUpdateManagerController(dom) {
  let currentVersionData = null;

  function renderUI(data) {
    currentVersionData = data;
    const status = formatUpdateStatus(data);
    const version = `v${data.current_version}`;
    const runMode = data.current_branch === 'portable' ? '便携版' : '源码运行';

    if (dom.gDashVersionCommit) dom.gDashVersionCommit.textContent = `版本: ${version}`;
    if (dom.gDashVersionBranch) dom.gDashVersionBranch.textContent = runMode;
    if (dom.gDashVersionBadge) {
      dom.gDashVersionBadge.className = `badge-status-pill ${status.tagClass}`;
      dom.gDashVersionBadge.textContent = status.tagText;
    }

    if (dom.gCurrentCommit) dom.gCurrentCommit.textContent = version;
    if (dom.gCurrentBranch) dom.gCurrentBranch.textContent = runMode;
    if (dom.gReleaseDownloadLink) dom.gReleaseDownloadLink.href = releaseHref(data.release_url);
    if (dom.gRemoteStatusTag) {
      dom.gRemoteStatusTag.className = `badge-status-pill ${status.tagClass}`;
      dom.gRemoteStatusTag.textContent = status.tagText;
    }
    if (dom.gUpdateNoticeBox) {
      dom.gUpdateNoticeBox.className = status.boxClass;
      if (dom.gUpdateNoticeTitle) dom.gUpdateNoticeTitle.textContent = status.title;
      if (dom.gUpdateNoticeDesc) dom.gUpdateNoticeDesc.textContent = status.desc;
      if (dom.gUpdateNoticeIcon) dom.gUpdateNoticeIcon.textContent = noticeIcon(data);
    }
  }

  async function loadVersionInfo(checkRemote = false) {
    if (dom.gDashVersionCommit && dom.gDashVersionCommit.textContent === '版本: -') {
      dom.gDashVersionCommit.textContent = '版本: 读取中...';
    }
    if (dom.gCurrentCommit && dom.gCurrentCommit.textContent === '-') {
      dom.gCurrentCommit.textContent = '读取中...';
    }

    try {
      let data = await fetchSystemVersion(checkRemote);
      // A local-only read (opening settings) must not forget what the last GitHub check found.
      if (
        !checkRemote &&
        currentVersionData?.latest_version &&
        !currentVersionData.error &&
        data.current_version === currentVersionData.current_version
      ) {
        data = {
          ...data,
          has_update: currentVersionData.has_update,
          latest_version: currentVersionData.latest_version,
          release_url: currentVersionData.release_url,
        };
      }
      renderUI(data);
      return data;
    } catch (err) {
      console.warn('Failed to load system version:', err);
      if (dom.gRemoteStatusTag) {
        dom.gRemoteStatusTag.className = 'badge-status-pill badge-pill-yellow';
        dom.gRemoteStatusTag.textContent = '读取受阻';
      }
      if (checkRemote) {
        if (dom.gUpdateNoticeBox) dom.gUpdateNoticeBox.classList.add('hidden');
        throw err;
      }
      return null;
    }
  }

  async function checkUpdate() {
    if (!dom.gBtnCheckUpdate || dom.gBtnCheckUpdate.disabled) return;
    dom.gBtnCheckUpdate.disabled = true;
    if (dom.gBtnCheckUpdateText) dom.gBtnCheckUpdateText.textContent = '正在检查…';
    if (dom.gRemoteStatusTag) {
      dom.gRemoteStatusTag.className = 'badge-status-pill badge-pill-yellow';
      dom.gRemoteStatusTag.textContent = '检查中...';
    }

    try {
      const data = await loadVersionInfo(true);
      if (data.error) {
        console.error('检查更新失败:', data.error);
        showToast('检查更新失败：暂时无法获取最新版本信息，请检查网络连接后重试', 'error');
      } else if (data.has_update) {
        showToast(`发现新版本 v${data.latest_version}`, 'info');
      } else {
        showToast('当前已是最新版本', 'success');
      }
    } catch (err) {
      console.error('检查更新发生异常:', err);
      showToast('检查更新失败：暂时无法连接服务，请检查网络连接后重试', 'error');
    } finally {
      dom.gBtnCheckUpdate.disabled = false;
      if (dom.gBtnCheckUpdateText) dom.gBtnCheckUpdateText.textContent = '检查新版本';
    }
  }

  if (dom.gBtnCheckUpdate) dom.gBtnCheckUpdate.addEventListener('click', checkUpdate);
  if (dom.gBtnGoUpdateTab) {
    dom.gBtnGoUpdateTab.addEventListener('click', () => {
      const updateTabBtn = dom.gTabs ? dom.gTabs.querySelector('[data-gtab="update"]') : null;
      if (updateTabBtn) {
        updateTabBtn.click();
        checkUpdate();
      }
    });
  }

  return { loadSystemVersionInfo: loadVersionInfo, handleCheckUpdate: checkUpdate };
}

let activeController = null;

export function initUpdateManager(dom) {
  activeController = createUpdateManagerController(dom);
}

export function loadSystemVersionInfo(checkRemote = false) {
  return activeController.loadSystemVersionInfo(checkRemote);
}

export function handleCheckUpdate() {
  return activeController.handleCheckUpdate();
}
