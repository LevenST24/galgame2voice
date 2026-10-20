// Strictly validate portable chat data before changing the current sessions.
const record = (value) => value && typeof value === 'object' && !Array.isArray(value);
const validId = (value) => typeof value === 'string' && value.length > 0 && value.length <= 200;
const timestamp = (value) => Number.isFinite(value) && value >= 0 ? value : Date.now();

// SQLite CURRENT_TIMESTAMP is UTC even though it has no explicit zone suffix.
export function serverTimestamp(value, fallback = Date.now()) {
  if (typeof value !== 'string' || !value) return fallback;
  const iso = value.replace(' ', 'T');
  const parsed = Date.parse(/(?:Z|[+-]\d{2}:?\d{2})$/i.test(iso) ? iso : `${iso}Z`);
  return Number.isFinite(parsed) ? parsed : fallback;
}

export function pickSettings(value, defaults) {
  const result = { ...defaults };
  if (!record(value)) return result;
  for (const key of Object.keys(defaults)) {
    const candidate = value[key];
    if (key.startsWith('adv')) {
      if (validAdvSetting(key, candidate)) result[key] = candidate;
    } else if (key === 'voiceProfileId') {
      if (candidate === null || (Number.isInteger(candidate) && candidate > 0)) result[key] = candidate;
    } else if (typeof candidate === typeof defaults[key] &&
               (typeof candidate !== 'number' || Number.isFinite(candidate))) {
      result[key] = candidate;
    }
  }
  if (result.advPrimaryLanguage === result.advSubtitleLanguage) {
    result.advSubtitleLanguage = result.advPrimaryLanguage === 'zh' ? 'ja' : 'zh';
  }
  return result;
}

const advEnums = {
  advPrimaryLanguage: ['zh', 'ja'], advSubtitleLanguage: ['zh', 'ja', 'none'],
  advAutoMode: ['normal', 'voice-priority'], advReplyFormat: ['old', 'lines'],
};
const advRanges = { advWindowOpacity: 100, advTextIntervalMs: 100, advAutoBaseMs: 6000, advAutoPerCharMs: 200 };
export function validAdvSetting(key, value) {
  if (key === 'advEnabled' || key === 'advInterruptVoice') return typeof value === 'boolean';
  if (advEnums[key]) return advEnums[key].includes(value);
  if (key in advRanges) return Number.isInteger(value) && value >= 0 && value <= advRanges[key];
  if (key === 'advPlayerName') {
    if (typeof value !== 'string' || !value.trim()) return false;
    const count = typeof Intl.Segmenter === 'function'
      ? [...new Intl.Segmenter(undefined, { granularity: 'grapheme' }).segment(value)].length : [...value].length;
    return count <= 40;
  }
  // Outfit IDs are manifest keys, never paths. Missing installed IDs remain portable.
  if (key === 'advOutfit') return typeof value === 'string' && (value === '' || /^[\p{L}\p{N}_-]{1,200}$/u.test(value));
  return false;
}

export function normalizeLines(lines) {
  if (!Array.isArray(lines)) return undefined;
  const result = [];
  // Stored and final lines are settled: audio still 'pending' never arrived.
  const KNOWN_STATUSES = new Set(['ready', 'skipped', 'failed']);
  for (const item of lines) {
    if (!item || typeof item !== 'object' || Array.isArray(item)) continue;
    if (!Number.isInteger(item.index) || item.index < 0) continue;
    if (typeof item.zh !== 'string') continue;
    if (typeof item.ja !== 'string') continue;
    if (typeof item.emotion !== 'string') continue;
    const act = (item.act && typeof item.act === 'object' && !Array.isArray(item.act))
      ? { ...item.act }
      : {};

    let audio = { status: 'skipped', chunks: [] };
    if (item.audio && typeof item.audio === 'object' && !Array.isArray(item.audio)) {
      const raw = item.audio.status;
      const status = raw === 'pending' ? 'failed' : KNOWN_STATUSES.has(raw) ? raw : 'skipped';
      const chunks = Array.isArray(item.audio.chunks)
        ? item.audio.chunks.filter(c => c && typeof c === 'object' && !Array.isArray(c)).map(c => ({ ...c }))
        : [];
      audio = { status, chunks };
    }

    result.push({
      index: item.index,
      zh: item.zh,
      ja: item.ja,
      emotion: item.emotion,
      act,
      audio,
    });
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
      const normalizedMsg = { ...message, ts: timestamp(message.ts) };
      if (Array.isArray(message.lines)) {
        normalizedMsg.lines = normalizeLines(message.lines);
      } else {
        delete normalizedMsg.lines;
      }
      return normalizedMsg;
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
      if (Array.isArray(message.lines)) {
        result.lines = message.lines.map(line => ({
          index: line.index,
          zh: line.zh,
          ja: line.ja,
          emotion: line.emotion,
          act: { ...(line.act || {}) },
          audio: { status: 'skipped', chunks: [] },
        }));
      }
      return result;
    }) })),
  };
}
