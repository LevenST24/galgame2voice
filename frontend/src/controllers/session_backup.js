import { requestJson } from '../api.js';
import { showToast } from '../ui.js';
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

export function initSessionBackup({ onImport, isBusy = () => false } = {}) {
  const $ = id => document.getElementById(id);
  const status = $('gChatStorageStatus');
  const original = $('gExportDamagedChats');
  const date = () => new Date().toISOString().replace(/[:.]/g, '-');
  const updateStatus = value => {
    if (status) status.textContent = value.ok ? '聊天记录已保存在当前浏览器。建议在更新或换浏览器前导出备份。' : value.message;
    if (original) original.hidden = !value.damaged;
    if (!value.ok) showToast(value.message, 'error');
  };
  onStorageStatus(updateStatus);
  updateStatus(getStorageStatus());
  $('gExportChats')?.addEventListener('click', () => {
    try {
      download(exportSessionData(), `galgame2voice-chat-${date()}.json`);
      showToast(isBusy() ? '已导出聊天备份。请在当前回复结束后再导出一次，以包含完整回复。' : '已导出聊天备份', 'success');
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
        settings: session.settings, messages: session.messages.map(message => ({ role: message.role,
          content: message.content, japanese: message.japanese || '' })) })) };
      await requestJson('/api/chat/import', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload) }, { timeoutMs: 60000 });
      const result = applySessionImport(incoming);
      onImport?.();
      showToast(`已导入 ${result.count} 个会话，原有会话保留。请在会话设置中重新选择音色。`, 'success');
      if (!result.saved) showToast('记录已存入本机数据库；浏览器保存失败，请保留备份并检查存储权限。', 'info');
    } catch (error) { showToast(error.message, 'error'); }
    finally { button.disabled = false; }
  });
}
