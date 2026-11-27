// @ts-check
import { requestJson } from './api.js';
import { splitGraphemes } from './adv/graphemes.js';
import { normalizeSnapshot, portableSnapshot } from './session_data.js';
import { buildLocalBackupSnapshot, DEFAULT_SESSION_SETTINGS, DEFAULT_GLOBAL, GREETING, LEGACY_GREETINGS, uid } from './store.js';

const normalize = data => portableSnapshot(normalizeSnapshot(data, DEFAULT_SESSION_SETTINGS, DEFAULT_GLOBAL));

// A question that failed or was stopped before any reply stays in the browser, also after the chat goes on,
// but the server drops that user row. Compare the rest, so the session is not copied whole.
function failedQuestionIds(session) {
  return new Set(session.messages.filter(message => message.role === 'user' && message.failed).map(message => message.id));
}
const messageKey = message => JSON.stringify([message.role, message.content]);

/** @param {string} title */
function trimCopyTitle(title, maxUnits = 180) {
  let result = '';
  for (const grapheme of splitGraphemes(title)) {
    if (result.length + grapheme.length > maxUnits) break;
    result += grapheme;
  }
  return result;
}

// Match occurrences in order, never a set: repeated identical lines are real messages.
function containsLocalMessages(server, local) {
  let position = 0;
  for (const message of local) {
    if (message.noVoice && message.role === 'assistant'
      && (message.content === GREETING || LEGACY_GREETINGS.includes(message.content))) continue;
    const key = messageKey(message);
    while (position < server.length && messageKey(server[position]) !== key) position++;
    if (position === server.length) return false;
    if (message.japanese && message.japanese !== server[position].japanese) return false;
    position++;
  }
  return true;
}

export function mergeChatBackup(serverData, localData) {
  if (serverData?.format !== 'galgame2voice.chat' || serverData.version !== 1) {
    throw new Error('本地服务返回的备份格式无法识别，请更新程序或先单独导出浏览器记录。');
  }
  const server = normalize(serverData);
  const local = normalize(localData);
  // portableSnapshot drops the failed flag, so read it from the browser data itself.
  const failed = new Map(normalizeSnapshot(localData, DEFAULT_SESSION_SETTINGS, DEFAULT_GLOBAL)
    .sessions.map(session => [session.id, failedQuestionIds(session)]));
  const byId = new Map(server.sessions.map(session => [session.id, session]));
  let browserCopies = 0;
  for (const session of local.sessions) {
    const stored = byId.get(session.id);
    if (!stored) {
      server.sessions.push(session);
      byId.set(session.id, session);
    } else if (containsLocalMessages(stored.messages,
      session.messages.filter(message => !failed.get(session.id)?.has(message.id)))) {
      // These lines are already in the database. Keep the settings saved later, including the voice binding.
      if (session.updatedAt > stored.updatedAt) {
        const sameVoice = session.settings.voiceProfileId === stored.settings.voiceProfileId;
        stored.settings = session.settings;
        if (session.characterName) {
          stored.characterName = session.characterName;
        } else if (!sameVoice) {
          delete stored.characterName;
        }
      }
      if (session.title && session.title !== '新对话') stored.title = session.title;
    } else {
      // A partial reply or an offline edit cannot safely replace database history.
      // Keep the complete local sequence separately so neither version is lost.
      let copyId;
      do { copyId = uid('s_browser_backup'); } while (byId.has(copyId));
      const copy = { ...session, id: copyId, title: `${trimCopyTitle(session.title)}（浏览器副本）` };
      server.sessions.push(copy);
      byId.set(copyId, copy);
      browserCopies++;
    }
  }
  const messages = server.sessions.flatMap(session => session.messages);
  const size = messages.reduce((sum, message) => sum + message.content.length + (message.japanese?.length || 0), 0);
  if (server.sessions.length > 10000 || messages.length > 50000 || size > 10_000_000 ||
      server.sessions.some(session => session.messages.length > 10000)) {
    throw new Error('记录超过单份聊天备份的导入上限，请关闭程序后备份整个数据目录（data 文件夹），并单独导出浏览器记录。');
  }
  const data = { format: 'galgame2voice.chat', version: 1, exportedAt: new Date().toISOString(),
    ...server, activeId: local.activeId || server.activeId, global: local.global };
  const content = JSON.stringify(data, null, 2);
  if (new Blob([content]).size > 20 * 1024 * 1024) {
    throw new Error('完整备份超过 20 MB 的导入上限，请关闭程序后备份整个数据目录（data 文件夹），并单独导出浏览器记录。');
  }
  return { content, count: server.sessions.length, browserCopies };
}

/**
 * @param {Array<{ id?: number | string | null, name?: string | null }>} [profiles]
 */
export async function exportCompleteChatBackup(profiles = []) {
  const server = await requestJson('/api/chat/export', {}, { timeoutMs: 60000 });
  return mergeChatBackup(server, buildLocalBackupSnapshot(profiles));
}
