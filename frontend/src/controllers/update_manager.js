// Update Manager Controller: GitHub online updates, commits diff, and progress tracking
import { fetchSystemVersion, applySystemUpdate } from '../api.js';
import { formatUpdateStatus, renderCommitsLog } from '../settings.js';
import { showToast } from '../ui.js';

let _dom = {};
let currentVersionData = null;

export function initUpdateManager(dom) {
  _dom = dom;

  if (_dom.gBtnCheckUpdate) {
    _dom.gBtnCheckUpdate.addEventListener('click', handleCheckUpdate);
  }
  if (_dom.gBtnApplyUpdate) {
    _dom.gBtnApplyUpdate.addEventListener('click', handleApplyUpdate);
  }
  if (_dom.gBtnReloadPage) {
    _dom.gBtnReloadPage.addEventListener('click', () => {
      window.location.reload();
    });
  }
  if (_dom.gBtnGoUpdateTab) {
    _dom.gBtnGoUpdateTab.addEventListener('click', () => {
      const updateTabBtn = _dom.gTabs ? _dom.gTabs.querySelector('[data-gtab="update"]') : null;
      if (updateTabBtn) {
        updateTabBtn.click();
        handleCheckUpdate();
      }
    });
  }
}

export async function loadSystemVersionInfo(checkRemote = false) {
  if (_dom.gDashVersionCommit && !_dom.gDashVersionCommit.textContent.startsWith('Commit: ')) {
    _dom.gDashVersionCommit.textContent = 'Commit: 读取中...';
  }
  if (_dom.gCurrentCommit && _dom.gCurrentCommit.textContent === '-') {
    _dom.gCurrentCommit.textContent = '读取中...';
  }

  try {
    const data = await fetchSystemVersion(checkRemote);
    currentVersionData = data;
    renderSystemVersionUI(data);
    return data;
  } catch (err) {
    console.warn('Failed to load system version:', err);
    if (_dom.gRemoteStatusTag) {
      _dom.gRemoteStatusTag.className = 'badge-status-pill badge-pill-yellow';
      _dom.gRemoteStatusTag.textContent = '读取受阻';
    }
    return null;
  }
}

export function renderSystemVersionUI(data) {
  if (!data) return;

  // 1. Dashboard 概览卡片更新
  if (_dom.gDashVersionCommit) {
    _dom.gDashVersionCommit.textContent = `Commit: ${data.current_version || '-'}`;
  }
  if (_dom.gDashVersionBranch) {
    _dom.gDashVersionBranch.textContent = `分支: ${data.current_branch || 'main'}`;
  }
  if (_dom.gDashVersionBadge) {
    if (data.has_update) {
      _dom.gDashVersionBadge.className = 'badge-status-pill badge-pill-indigo';
      _dom.gDashVersionBadge.textContent = `有更新 (${data.behind_count})`;
    } else {
      _dom.gDashVersionBadge.className = 'badge-status-pill badge-pill-green';
      _dom.gDashVersionBadge.textContent = '最新版';
    }
  }

  // 2. 独立更新面板字段更新
  if (_dom.gCurrentCommit) _dom.gCurrentCommit.textContent = data.current_version || '-';
  if (_dom.gCurrentBranch) _dom.gCurrentBranch.textContent = data.current_branch || 'main';
  if (_dom.gCurrentDate) _dom.gCurrentDate.textContent = data.commit_date || '-';
  if (_dom.gRemoteUrl) {
    _dom.gRemoteUrl.textContent = data.remote_url || '-';
    _dom.gRemoteUrl.title = data.remote_url || '';
  }
  if (_dom.gCurrentMsgText) {
    _dom.gCurrentMsgText.textContent = data.commit_message || '无提交信息';
  }

  const statusInfo = formatUpdateStatus(data);
  if (_dom.gRemoteStatusTag) {
    _dom.gRemoteStatusTag.className = `badge-status-pill ${statusInfo.badgeClass}`;
    _dom.gRemoteStatusTag.textContent = statusInfo.badgeText;
  }

  // 更新提示框
  if (_dom.gUpdateNoticeBox) {
    if (data.has_update || data.error || (data.commits_log && data.commits_log.length > 0)) {
      _dom.gUpdateNoticeBox.classList.remove('hidden');
    }
    if (_dom.gUpdateNoticeTitle) _dom.gUpdateNoticeTitle.textContent = statusInfo.title;
    if (_dom.gUpdateNoticeDesc) _dom.gUpdateNoticeDesc.textContent = statusInfo.desc;
    if (_dom.gUpdateNoticeIcon) {
      _dom.gUpdateNoticeIcon.textContent = data.has_update ? '🚀' : (data.error ? '⚠️' : '✅');
    }
  }

  // 提交日志列表
  if (_dom.gCommitsLogContainer && _dom.gCommitsLogList) {
    if (data.has_update && data.commits_log && data.commits_log.length > 0) {
      _dom.gCommitsLogContainer.classList.remove('hidden');
      renderCommitsLog(_dom.gCommitsLogList, data.commits_log);
    } else {
      _dom.gCommitsLogContainer.classList.add('hidden');
      _dom.gCommitsLogList.innerHTML = '';
    }
  }

  // 一键更新按钮显示逻辑
  if (_dom.gBtnApplyUpdate) {
    _dom.gBtnApplyUpdate.classList.remove('hidden');
    if (statusInfo.hasUpdate) {
      _dom.gBtnApplyUpdate.className = 'btn primary';
      if (_dom.gBtnApplyUpdateText) {
        _dom.gBtnApplyUpdateText.textContent = `一键拉取并更新 (${data.behind_count} 个新提交)`;
      }
    } else {
      _dom.gBtnApplyUpdate.className = 'btn secondary';
      if (_dom.gBtnApplyUpdateText) {
        _dom.gBtnApplyUpdateText.textContent = '一键拉取并更新';
      }
    }
  }
}

export async function handleCheckUpdate() {
  if (!_dom.gBtnCheckUpdate) return;
  _dom.gBtnCheckUpdate.disabled = true;
  const originalText = _dom.gBtnCheckUpdateText ? _dom.gBtnCheckUpdateText.textContent : '检查更新';
  if (_dom.gBtnCheckUpdateText) _dom.gBtnCheckUpdateText.textContent = '正在检查远程...';
  if (_dom.gRemoteStatusTag) {
    _dom.gRemoteStatusTag.className = 'badge-status-pill badge-pill-yellow';
    _dom.gRemoteStatusTag.textContent = '检查中...';
  }

  try {
    const data = await loadSystemVersionInfo(true);
    if (data && data.has_update) {
      showToast(`检测到新版本可用！落后 ${data.behind_count} 个提交`, 'info');
    } else if (data && !data.error) {
      showToast('当前已是最新版本', 'success');
      if (_dom.gUpdateNoticeBox) _dom.gUpdateNoticeBox.classList.remove('hidden');
    } else if (data && data.error) {
      showToast(`检查更新失败: ${data.error}`, 'error');
    }
  } catch (err) {
    showToast(`检查更新发生异常: ${err.message}`, 'error');
  } finally {
    _dom.gBtnCheckUpdate.disabled = false;
    if (_dom.gBtnCheckUpdateText) _dom.gBtnCheckUpdateText.textContent = originalText;
  }
}

export async function handleApplyUpdate() {
  const isLatest = currentVersionData && !currentVersionData.has_update;
  const promptText = isLatest
    ? '当前本地版本已是最新。确定要从 GitHub 重新拉取吗？（前端文件无变更时将跳过静态产物构建）'
    : '确定要从 GitHub 拉取最新版本吗？\n拉取后系统会在检测到前端文件变更时自动构建静态资源，并同步角色包。';

  if (!confirm(promptText)) {
    return;
  }

  if (_dom.gBtnApplyUpdate) _dom.gBtnApplyUpdate.disabled = true;
  if (_dom.gBtnCheckUpdate) _dom.gBtnCheckUpdate.disabled = true;
  if (_dom.gBtnApplyUpdateText) _dom.gBtnApplyUpdateText.textContent = '正在拉取并更新...';

  if (_dom.gUpdateProgressBox) _dom.gUpdateProgressBox.classList.remove('hidden');
  if (_dom.gUpdateSpinner) _dom.gUpdateSpinner.classList.remove('hidden');
  if (_dom.gUpdateProgressTitle) _dom.gUpdateProgressTitle.textContent = '正在执行 git pull 与资源构建，请稍候...';
  if (_dom.gBtnReloadPage) _dom.gBtnReloadPage.classList.add('hidden');
  if (_dom.gUpdateLogOutput) _dom.gUpdateLogOutput.textContent = '>>> 开始拉取 GitHub 最新版本...\n';

  try {
    const result = await applySystemUpdate();
    if (_dom.gUpdateLogOutput) {
      _dom.gUpdateLogOutput.textContent = result.output || (result.success ? '更新成功' : '更新未完成');
    }

    if (result.success) {
      if (_dom.gUpdateProgressTitle) {
        _dom.gUpdateProgressTitle.textContent = result.restart_required
          ? '更新完成！检测到后端变动，建议重启后台服务并刷新页面。'
          : '更新完成！静态产物已就绪，点击右侧刷新页面即可生效。';
      }
      if (_dom.gUpdateSpinner) _dom.gUpdateSpinner.classList.add('hidden');
      if (_dom.gBtnReloadPage) _dom.gBtnReloadPage.classList.remove('hidden');
      showToast('版本更新成功！请按下方提示刷新页面或重启服务', 'success');

      await loadSystemVersionInfo(false);
    } else {
      if (_dom.gUpdateProgressTitle) {
        _dom.gUpdateProgressTitle.textContent = '更新中断，请根据下方诊断信息处理:';
      }
      if (_dom.gUpdateSpinner) _dom.gUpdateSpinner.classList.add('hidden');
      showToast(`更新失败: ${result.error || '详见下方输出'}`, 'error');
    }
  } catch (err) {
    if (_dom.gUpdateProgressTitle) {
      _dom.gUpdateProgressTitle.textContent = '更新请求失败:';
    }
    if (_dom.gUpdateSpinner) _dom.gUpdateSpinner.classList.add('hidden');
    if (_dom.gUpdateLogOutput) {
      _dom.gUpdateLogOutput.textContent += `\n[错误] ${err.message}`;
    }
    showToast(`更新请求异常: ${err.message}`, 'error');
  } finally {
    if (_dom.gBtnApplyUpdate) _dom.gBtnApplyUpdate.disabled = false;
    if (_dom.gBtnCheckUpdate) _dom.gBtnCheckUpdate.disabled = false;
    if (_dom.gBtnApplyUpdateText) _dom.gBtnApplyUpdateText.textContent = '一键拉取并更新';
  }
}
