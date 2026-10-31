import { requestJson } from '../api.js';
import { showToast } from '../ui.js';
import { exportCompleteChatBackup } from '../chat_backup.js';
import { exportSessionData, prepareSessionImport, applySessionImport, getStorageStatus,
  onStorageStatus, getDamagedSessionData, resumeLocalSaving } from '../store.js';

function download(content, name) {
  const url = URL.createObjectURL(new Blob([content], { type: 'application/json;charset=utf-8' }));
  const link = document.createElement('a');
  link.href = url; link.download = name;
  document.body.appendChild(link);
  link.click(); link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export function initSessionBackup({ onImport, onOpenBackupTab, isBusy = () => false } = {}) {
  const $ = id => document.getElementById(id);
  const status = $('gChatStorageStatus');
  const original = $('gExportDamagedChats');
  const date = () => new Date().toISOString().replace(/[:.]/g, '-');
  const updateStatus = value => {
    if (status) status.textContent = value.ok ? '聊天记录已保存在当前浏览器。建议在更新或换浏览器前导出备份。' : value.message;
    if (original) original.hidden = !value.damaged;
    if (!value.ok) showToast(value.message, 'error', onOpenBackupTab ? {
      actionLabel: '打开备份设置',
      onAction: () => onOpenBackupTab(),
    } : null);
  };
  onStorageStatus(updateStatus);
  updateStatus(getStorageStatus());
  const exportButton = $('gExportChats');
  exportButton?.addEventListener('click', async () => {
    if (exportButton.disabled) return;
    exportButton.disabled = true;
    exportButton.textContent = '正在读取完整记录…';
    try {
      const result = await exportCompleteChatBackup();
      download(result.content, `galgame2voice-chat-${date()}.json`);
      const note = result.browserCopies ? ' 尚未同步的文字另存为「浏览器副本」。' : '';
      showToast(`已导出 ${result.count} 个会话。${note}${isBusy() ? '请在当前回复结束后再导出一次，以包含完整回复。' : ''}`, 'success');
    } catch (error) {
      showToast(`完整备份未导出：${error.message} 可先用「仅导出浏览器记录」保留当前网页内容。`, 'error');
    } finally {
      exportButton.disabled = false;
      exportButton.textContent = '导出完整聊天备份';
    }
  });
  $('gExportLocalChats')?.addEventListener('click', () => {
    try {
      download(exportSessionData(), `galgame2voice-browser-only-${date()}.json`);
      showToast('已导出浏览器记录；数据库中尚未加载的历史未包含。服务恢复后请再导出完整备份。', 'info');
    } catch (error) { showToast(error.message, 'error'); }
  });
  original?.addEventListener('click', () => {
    const raw = getDamagedSessionData();
    if (raw !== null) download(raw, `galgame2voice-original-${date()}.json`);
  });
  $('gResumeChatSaving')?.addEventListener('click', () => {
    if (getStorageStatus().damaged && !confirm('请先导出原始记录。恢复保存会另存原内容，然后保存当前显示的会话，是否继续？')) return;
    try { if (resumeLocalSaving()) showToast('本地保存已恢复', 'success'); }
    catch (error) { showToast(error.message, 'error'); }
  });
  const input = $('gImportChatFile');
  const button = $('gImportChats');
  button?.addEventListener('click', () => {
    if (isBusy()) { showToast('请等待当前回复完成，或停止回复后再导入。', 'info'); return; }
    input?.click();
  });
  input?.addEventListener('change', async () => {
    const file = input.files?.[0];
    input.value = '';
    if (!file) return;
    button.disabled = true;
    try {
      if (file.size > 20 * 1024 * 1024) throw new Error('备份超过 20 MB，请拆分后再导入。');
      const incoming = prepareSessionImport(await file.text());
      if (isBusy()) throw new Error('当前回复正在生成，请停止或等待完成后再导入。');
      const payload = { sessions: incoming.sessions.map(session => ({ id: session.id, title: session.title,
        settings: session.settings, created_at: session.createdAt, updated_at: session.updatedAt,
        messages: session.messages.map(message => ({ role: message.role,
          content: message.content, japanese: message.japanese || '', ts: message.ts, lines: message.lines })) })) };
      await requestJson('/api/chat/import', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload) }, { timeoutMs: 60000 });
      const result = applySessionImport(incoming);
      onImport?.();
      showToast(`已导入 ${result.count} 个会话，原有会话保留。请在会话设置中重新选择音色。`, 'success');
      if (!result.saved) showToast('记录已存入本机数据库；浏览器保存失败，请保留备份并检查存储权限。', 'info', onOpenBackupTab ? {
        actionLabel: '打开备份设置',
        onAction: () => onOpenBackupTab(),
      } : null);
    } catch (error) {
      // A multi-session file is one transaction: any existing id rolls the whole import back.
      const message = error.code === 'REQUEST_TIMEOUT'
        ? '导入结果未确认，请刷新页面查看会话是否已经导入；再次导入同一个文件不会重复写入。'
        : error.status === 409
          ? '导入的会话已存在，整份备份这次都没有写入。请刷新页面查看。'
          : error.message;
      showToast(message, 'error');
    }
    finally { button.disabled = false; }
  });
}
