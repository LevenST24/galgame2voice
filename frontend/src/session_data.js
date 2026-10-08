// Strictly validate portable chat data before changing the current sessions.
const record = (value) => value && typeof value === 'object' && !Array.isArray(value);
const validId = (value) => typeof value === 'string' && value.length > 0 && value.length <= 200;
const timestamp = (value) => Number.isFinite(value) && value >= 0 ? value : Date.now();

function pickSettings(value, defaults) {
  const result = { ...defaults };
  if (!record(value)) return result;
  for (const key of Object.keys(defaults)) {
    const candidate = value[key];
    if (key === 'voiceProfileId') {
      if (candidate === null || (Number.isInteger(candidate) && candidate > 0)) result[key] = candidate;
    } else if (typeof candidate === typeof defaults[key] &&
               (typeof candidate !== 'number' || Number.isFinite(candidate))) {
      result[key] = candidate;
    }
  }
  return result;
}

export function normalizeSnapshot(data, sessionDefaults, globalDefaults) {
  if (!record(data) || !Array.isArray(data.sessions)) throw new Error('文件中没有有效的聊天记录。');
  const ids = new Set();
  const sessions = data.sessions.map((session) => {
    if (!record(session) || !validId(session.id) || ids.has(session.id) ||
        typeof session.title !== 'string' || !Array.isArray(session.messages)) {
      throw new Error('会话格式不完整或编号重复，当前聊天记录未修改。');
    }
    ids.add(session.id);
    const messageIds = new Set();
    const messages = session.messages.map((message) => {
      if (!record(message) || !validId(message.id) || messageIds.has(message.id) ||
          !['assistant', 'user', 'system'].includes(message.role) || typeof message.content !== 'string') {
        throw new Error('消息格式不完整或编号重复，当前聊天记录未修改。');
      }
      messageIds.add(message.id);
      // JSON is inert data. Preserve voice metadata for existing local playback.
      return { ...message, ts: timestamp(message.ts) };
    });
    return {
      id: session.id, title: session.title, messages,
      createdAt: timestamp(session.createdAt), updatedAt: timestamp(session.updatedAt),
      settings: pickSettings(session.settings, sessionDefaults),
    };
  });
  return {
    sessions,
    activeId: ids.has(data.activeId) ? data.activeId : sessions[0]?.id ?? null,
    global: pickSettings(data.global, globalDefaults),
  };
}

export function portableSnapshot(snapshot) {
  return {
    ...snapshot,
    // Generated audio is a separate cache. Browser recording blobs cannot be
    // restored from JSON, and URLs from other installations may be stale.
    sessions: snapshot.sessions.map(session => ({ ...session, messages: session.messages.map(message => {
      const result = { id: message.id, role: message.role, content: message.content, ts: message.ts };
      if (typeof message.japanese === 'string') result.japanese = message.japanese;
      if (message.noVoice) result.noVoice = true;
      return result;
    }) })),
  };
}
