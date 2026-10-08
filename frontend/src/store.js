// 会话状态管理 + localStorage 持久化（含会话级设置与全局设置）
import { normalizeSnapshot, portableSnapshot } from './session_data.js';
const STORAGE_KEY = 'gal2voice.chat.v1';
const BACKUP_KEY = `${STORAGE_KEY}.backup`;
const LEGACY_KEYS = ['inkwell.chat.v2', 'inkwell.chat.v1'];
const listeners = new Set();
let damagedRaw = null;
let lastStoredRaw = null;
let skipBackupOnce = false;
let storageStatus = { ok: true, message: '' };

export function getStorageStatus() { return { ...storageStatus, damaged: damagedRaw !== null }; }
export function onStorageStatus(listener) { listeners.add(listener); return () => listeners.delete(listener); }
function reportStorage(ok, message = '') {
  if (storageStatus.ok === ok && storageStatus.message === message) return;
  storageStatus = { ok, message };
  for (const listener of listeners) {
    try { listener(getStorageStatus()); } catch (error) { console.warn('更新保存状态提示失败', error); }
  }
}

export function getDamagedSessionData() { return damagedRaw; }
const snapshot = () => ({ sessions: state.sessions, activeId: state.activeId, global: state.global });

export const DEFAULT_SESSION_SETTINGS = {
  systemPrompt: '',
  voiceProfileId: null,
  temperature: 0.7,
  topP: 1.0,
  maxTokens: 1024,
  freqPenalty: 0,
  presPenalty: 0,
  maxContext: 20,
  aiAdaptiveVoice: true,
  ttsSpeed: 1.0,
  ttsTopK: 15,
  ttsTopP: 1.0,
  ttsTemperature: 1.0,
};

export const DEFAULT_GLOBAL = {
  voiceMode: false,
  ttsPreset: '',
  autoTranslate: false,
};

export const state = {
  sessions: [],
  activeId: null,
  global: { ...DEFAULT_GLOBAL },
};

export const GREETING =
  '你好，我是 **Gal2Voice**。\n\n支持语音输入与朗读回复：点输入框旁的麦克风说话会自动转成文字，点顶栏喇叭可以朗读我的回复。右上角`会话设置`可绑定角色音色并配置人设与采样参数，侧边栏底部齿轮可切换对话模型。\n\n每个会话的上下文互相独立，切换回来时记录原样保留。';

export function uid(prefix = 'id') {
  return `${prefix}_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`;
}

const now = () => Date.now();

export function saveState() {
  if (damagedRaw !== null) {
    reportStorage(false, '原会话记录无法读取，已保留原内容。请在「全局设置 → 系统与更新」导出原始记录，再恢复本地保存。');
    return false;
  }
  try {
    const previous = localStorage.getItem(STORAGE_KEY);
    if (previous !== lastStoredRaw) {
      reportStorage(false, '另一网页已修改聊天记录。为避免覆盖，当前网页暂停保存；请先导出当前聊天备份，再刷新后导入需要保留的记录。');
      return false;
    }
    const next = JSON.stringify(snapshot());
    try { localStorage.setItem(STORAGE_KEY, next); }
    catch (error) {
      if (error.name !== 'QuotaExceededError') throw error;
      // Evict only our optional fallback snapshot, never the primary record.
      localStorage.removeItem(BACKUP_KEY);
      localStorage.setItem(STORAGE_KEY, next);
    }
    lastStoredRaw = next;
    // A fallback snapshot is best effort; lack of quota must not fail the main
    // write or remove any existing user records.
    if (previous && previous !== next && !skipBackupOnce) {
      try { localStorage.setItem(BACKUP_KEY, previous); } catch { /* keep the previous backup */ }
    }
    skipBackupOnce = false;
    reportStorage(true);
    return true;
  } catch (e) {
    console.warn('保存会话失败', e);
    reportStorage(false, '浏览器未能保存聊天记录。请先在「全局设置 → 系统与更新」导出聊天备份，再检查浏览器存储权限或清理不需要的会话；关闭网页前请完成备份。');
    return false;
  }
}

function ensureSettings(session) {
  session.settings = { ...DEFAULT_SESSION_SETTINGS, ...(session.settings || {}) };
  return session;
}

export function makeStarterSession() {
  const s = ensureSettings({
    id: uid('s'),
    title: '新对话',
    createdAt: now(),
    updatedAt: now(),
    messages: [{ id: uid('m'), role: 'assistant', content: GREETING, noVoice: true, ts: now() }],
  });
  state.sessions.unshift(s);
  state.activeId = s.id;
  saveState();
  return s;
}

export function loadState() {
  state.sessions = [];
  state.activeId = null;
  state.global = { ...DEFAULT_GLOBAL };
  damagedRaw = null;
  lastStoredRaw = null;
  skipBackupOnce = false;
  try {
    lastStoredRaw = localStorage.getItem(STORAGE_KEY);
    for (const key of [STORAGE_KEY, BACKUP_KEY, ...LEGACY_KEYS]) {
      const raw = localStorage.getItem(key);
      if (!raw) continue;
      try {
        const data = normalizeSnapshot(JSON.parse(raw), DEFAULT_SESSION_SETTINGS, DEFAULT_GLOBAL);
        Object.assign(state, data);
        break;
      } catch {
        if (key === STORAGE_KEY) damagedRaw = raw;
      }
    }
  } catch (e) {
    console.warn('读取本地会话失败', e);
  }
  if (!state.sessions.length) makeStarterSession();
  if (!state.sessions.some((s) => s.id === state.activeId)) {
    state.activeId = state.sessions[0]?.id ?? null;
  }
  saveState();
}

export function exportSessionData() {
  return JSON.stringify({ format: 'galgame2voice.chat', version: 1, exportedAt: new Date().toISOString(),
    ...portableSnapshot(normalizeSnapshot(snapshot(), DEFAULT_SESSION_SETTINGS, DEFAULT_GLOBAL)) }, null, 2);
}

export function prepareSessionImport(raw) {
  let parsed;
  try { parsed = JSON.parse(raw); } catch { throw new Error('无法读取 JSON，请选择之前导出的聊天备份文件。'); }
  if (parsed?.format !== 'galgame2voice.chat' || parsed.version !== 1) {
    throw new Error('备份格式或版本不受支持，请选择 Galgame2Voice 导出的聊天备份。');
  }
  const incoming = portableSnapshot(normalizeSnapshot(parsed, DEFAULT_SESSION_SETTINGS, DEFAULT_GLOBAL));
  if (incoming.sessions.length === 0) throw new Error('备份中没有聊天会话，当前记录未修改。');
  if (incoming.sessions.length > 10000) throw new Error('单份备份超过 10000 个会话，请先导出较小的备份再导入。');
  // New IDs protect both browser and server records from replacement.
  for (const session of incoming.sessions) {
    session.id = uid('s_import');
    session.settings.voiceProfileId = null;
  }
  return incoming;
}

export function applySessionImport(incoming) {
  state.sessions.unshift(...incoming.sessions);
  state.activeId = incoming.sessions[0].id;
  const saved = saveState();
  return { count: incoming.sessions.length, saved };
}

export function resumeLocalSaving() {
  if (damagedRaw !== null) {
    try {
      localStorage.setItem(`${STORAGE_KEY}.damaged.${Date.now()}`, damagedRaw);
    } catch {
      throw new Error('无法另存原始记录。请先导出原始记录，并检查浏览器存储权限和可用空间。');
    }
    skipBackupOnce = true;
    damagedRaw = null;
  }
  return saveState();
}

export function createSession(title = '新对话') {
  const s = ensureSettings({
    id: uid('s'),
    title,
    createdAt: now(),
    updatedAt: now(),
    messages: [],
  });
  state.sessions.unshift(s);
  state.activeId = s.id;
  saveState();
  return s;
}

export function deleteSession(id) {
  state.sessions = state.sessions.filter((s) => s.id !== id);
  if (state.activeId === id) state.activeId = state.sessions[0]?.id ?? null;
  if (!state.sessions.length) makeStarterSession();
  saveState();
}

export function getActive() {
  if (!state.sessions.length) makeStarterSession();
  let s = state.sessions.find((sess) => sess.id === state.activeId);
  if (!s) {
    state.activeId = state.sessions[0]?.id ?? null;
    s = state.sessions[0] ?? null;
  }
  return s;
}

export function getSession(id) {
  return state.sessions.find((s) => s.id === id) ?? null;
}

export function saveGlobal(patch) {
  state.global = { ...state.global, ...patch };
  saveState();
}

export function autoTitle(session) {
  if (!session || (session.title && session.title !== '新对话' && session.title !== '开始 · Gal2Voice 使用指南')) return false;
  const firstUser = session.messages.find((m) => m.role === 'user');
  if (!firstUser) return false;
  const t = firstUser.content.replace(/[\r\n\t]+/g, ' ').trim();
  if (!t) return false;
  session.title = t.length > 20 ? `${t.slice(0, 20)}…` : t;
  return true;
}

export function addMessage(role, content, sessionId = state.activeId) {
  const s = getSession(sessionId);
  if (!s) return null;
  const msg = { id: uid('m'), role, content, ts: now() };
  s.messages.push(msg);
  s.updatedAt = now();
  autoTitle(s);
  saveState();
  return msg;
}
