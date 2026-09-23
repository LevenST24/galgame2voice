// 会话状态管理 + localStorage 持久化（含会话级设置与全局设置）
const STORAGE_KEY = 'gal2voice.chat.v1';
const LEGACY_KEYS = ['inkwell.chat.v2', 'inkwell.chat.v1'];

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
  '你好，我是 **Gal2Voice**。\n\n支持语音输入与朗读回复：点输入框旁的麦克风说话会自动转成文字，点顶栏喇叭可以朗读我的回复。右上角`会话设置`可配置专属人设与采样参数，侧边栏底部齿轮可切换音色与对话模型。\n\n每个会话的上下文互相独立，切换回来时记录原样保留。';

export function uid(prefix = 'id') {
  return `${prefix}_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`;
}

const now = () => Date.now();

export function saveState() {
  try {
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({ sessions: state.sessions, activeId: state.activeId, global: state.global })
    );
  } catch (e) {
    console.warn('保存会话失败', e);
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
  try {
    const raw = localStorage.getItem(STORAGE_KEY) || LEGACY_KEYS.map((k) => localStorage.getItem(k)).find(Boolean);
    if (raw) {
      const data = JSON.parse(raw);
      if (Array.isArray(data.sessions)) state.sessions = data.sessions.map(ensureSettings);
      if (data.activeId) state.activeId = data.activeId;
      if (data.global) state.global = { ...DEFAULT_GLOBAL, ...data.global };
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
