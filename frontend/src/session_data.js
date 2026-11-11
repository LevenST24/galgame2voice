// @ts-check
// Strictly validate portable chat data before changing the current sessions.

/**
 * @typedef {object} LineAudioSpec
 * @property {'ready' | 'skipped' | 'failed' | 'pending' | string} status
 * @property {Array<Record<string, any>>} chunks
 * @property {number} [generated_at]
 * @property {number} [voice_profile_id]
 * @property {string} [voice_name]
 */

/**
 * @typedef {object} DialogueLine
 * @property {number} index
 * @property {string} zh
 * @property {string} ja
 * @property {string} emotion
 * @property {Record<string, any>} act
 * @property {LineAudioSpec} audio
 */

/**
 * @typedef {object} ChatMessage
 * @property {string} id
 * @property {'assistant' | 'user' | 'system' | string} role
 * @property {string} content
 * @property {number} ts
 * @property {string} [japanese]
 * @property {boolean} [noVoice]
 * @property {boolean} [failed]
 * @property {DialogueLine[]} [lines]
 * @property {number} [serverUserMessageId]
 * @property {number} [serverMessageId]
 * @property {boolean} [turnConflict]
 * @property {string[]} [audioUrls]
 */

/**
 * @typedef {object} SessionSettings
 * @property {string} systemPrompt
 * @property {number | null} voiceProfileId
 * @property {number} temperature
 * @property {number} topP
 * @property {number} maxTokens
 * @property {number} freqPenalty
 * @property {number} presPenalty
 * @property {number} maxContext
 * @property {boolean} aiAdaptiveVoice
 * @property {'lines' | 'legacy' | string} advReplyFormat
 * @property {number} ttsSpeed
 * @property {number} ttsTopK
 * @property {number} ttsTopP
 * @property {number} ttsTemperature
 * @property {string} advPlayerName
 * @property {'zh' | 'ja' | string} advPrimaryLanguage
 * @property {'zh' | 'ja' | 'none' | string} advSubtitleLanguage
 * @property {number} advWindowOpacity
 * @property {number} advTextIntervalMs
 * @property {boolean} advInterruptVoice
 * @property {number} advAutoBaseMs
 * @property {number} advAutoPerCharMs
 * @property {'normal' | 'voice-priority' | string} advAutoMode
 * @property {string} advOutfit
 * @property {string} advBackground
 * @property {'auto' | 'day' | 'evening' | 'night' | string} advBgTime
 * @property {boolean} advBlink
 * @property {boolean} advLipSync
 */

/**
 * @typedef {object} GlobalSettings
 * @property {boolean} voiceMode
 * @property {string} ttsPreset
 */

/**
 * @typedef {object} ChatSession
 * @property {string} id
 * @property {string} title
 * @property {ChatMessage[]} messages
 * @property {number} createdAt
 * @property {number} updatedAt
 * @property {SessionSettings} settings
 * @property {string} [lastLine]
 * @property {number} [unconfirmedSettingsVersion]
 */

/**
 * @typedef {object} PortableChatSnapshot
 * @property {ChatSession[]} sessions
 * @property {string | null} activeId
 * @property {GlobalSettings} global
 */

/** @param {any} value @returns {value is Record<string, any>} */
const record = (value) => value && typeof value === 'object' && !Array.isArray(value);
/** @param {any} value @returns {value is string} */
const validId = (value) => typeof value === 'string' && value.length > 0 && value.length <= 200;
/** @param {any} value @returns {number} */
const timestamp = (value) => Number.isFinite(value) && value >= 0 ? value : Date.now();

// SQLite CURRENT_TIMESTAMP is UTC even though it has no explicit zone suffix.
/**
 * @param {any} value
 * @param {number} [fallback]
 * @returns {number}
 */
export function serverTimestamp(value, fallback = Date.now()) {
  if (typeof value !== 'string' || !value) return fallback;
  const iso = value.replace(' ', 'T');
  const parsed = Date.parse(/(?:Z|[+-]\d{2}:?\d{2})$/i.test(iso) ? iso : `${iso}Z`);
  return Number.isFinite(parsed) ? parsed : fallback;
}

/**
 * @template {Record<string, any>} T
 * @param {any} value
 * @param {T} defaults
 * @returns {T}
 */
export function pickSettings(value, defaults) {
  /** @type {Record<string, any>} */
  const result = { ...defaults };
  if (!record(value)) return /** @type {T} */ (result);
  for (const key of Object.keys(defaults)) {
    const candidate = value[key];
    if (key.startsWith('adv')) {
      if (validAdvSetting(key, candidate)) result[key] = key === 'advPlayerName' && candidate === 'Player' ? '我' : candidate;
    } else if (key === 'voiceProfileId') {
      if (candidate === null || (Number.isInteger(candidate) && candidate > 0)) result[key] = candidate;
    } else if (typeof candidate === typeof defaults[key] &&
               (typeof candidate !== 'number' || Number.isFinite(candidate))) {
      result[key] = candidate;
    }
  }
  if ('advPrimaryLanguage' in defaults && 'advSubtitleLanguage' in defaults &&
      result.advPrimaryLanguage === result.advSubtitleLanguage) {
    result.advSubtitleLanguage = result.advPrimaryLanguage === 'zh' ? 'ja' : 'zh';
  }
  return /** @type {T} */ (result);
}

// advReplyFormat 'legacy' is the explicit opt-out. 'old' was the stored default
// before per-line replies became the default, so it now falls back to 'lines'.
/** @type {Record<string, string[]>} */
const advEnums = {
  advPrimaryLanguage: ['zh', 'ja'], advSubtitleLanguage: ['zh', 'ja', 'none'],
  advAutoMode: ['normal', 'voice-priority'], advReplyFormat: ['lines', 'legacy'],
  advBgTime: ['auto', 'day', 'evening', 'night'],
};
/** @type {Record<string, number>} */
const advRanges = { advWindowOpacity: 100, advTextIntervalMs: 100, advAutoBaseMs: 6000, advAutoPerCharMs: 200 };

/**
 * @param {string} key
 * @param {any} value
 * @returns {boolean}
 */
export function validAdvSetting(key, value) {
  if (key === 'advInterruptVoice' || key === 'advBlink' || key === 'advLipSync') return typeof value === 'boolean';
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
  // advBackground is "" (follow the character's work), "none" (plain sky) or a global "<work>/<id>" key.
  if (key === 'advBackground') {
    if (typeof value !== 'string') return false;
    if (value === '' || value === 'none') return true;
    const parts = value.split('/');
    return parts.length === 2 && parts.every(part => /^[A-Za-z0-9_-]{1,64}$/.test(part));
  }
  return false;
}

/**
 * @param {any} lines
 * @returns {DialogueLine[] | undefined}
 */
export function normalizeLines(lines) {
  if (!Array.isArray(lines)) return undefined;
  /** @type {DialogueLine[]} */
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

    /** @type {LineAudioSpec} */
    let audio = { status: 'skipped', chunks: [] };
    if (item.audio && typeof item.audio === 'object' && !Array.isArray(item.audio)) {
      const raw = item.audio.status;
      const status = raw === 'pending' ? 'failed' : KNOWN_STATUSES.has(raw) ? raw : 'skipped';
      const chunks = Array.isArray(item.audio.chunks)
        ? item.audio.chunks.filter(c => c && typeof c === 'object' && !Array.isArray(c)).map(c => ({ ...c }))
        : [];
      audio = { status, chunks };
      if (Number.isSafeInteger(item.audio.generated_at) && item.audio.generated_at >= 0) audio.generated_at = item.audio.generated_at;
      if (Number.isSafeInteger(item.audio.voice_profile_id) && item.audio.voice_profile_id > 0) audio.voice_profile_id = item.audio.voice_profile_id;
      if (typeof item.audio.voice_name === 'string' && item.audio.voice_name.length <= 100) audio.voice_name = item.audio.voice_name;
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

/**
 * @param {any} data
 * @param {SessionSettings} sessionDefaults
 * @param {GlobalSettings} globalDefaults
 * @returns {PortableChatSnapshot}
 */
export function normalizeSnapshot(data, sessionDefaults, globalDefaults) {
  if (!record(data) || !Array.isArray(data.sessions)) throw new Error('文件中没有有效的聊天记录。');
  const ids = new Set();
  /** @type {ChatSession[]} */
  const sessions = data.sessions.map((session) => {
    if (!record(session) || !validId(session.id) || ids.has(session.id) ||
        typeof session.title !== 'string' || !Array.isArray(session.messages)) {
      throw new Error('会话格式不完整或编号重复，当前聊天记录未修改。');
    }
    ids.add(session.id);
    const messageIds = new Set();
    /** @type {ChatMessage[]} */
    const messages = session.messages.map((message) => {
      if (!record(message) || !validId(message.id) || messageIds.has(message.id) ||
          !['assistant', 'user', 'system'].includes(message.role) || typeof message.content !== 'string') {
        throw new Error('消息格式不完整或编号重复，当前聊天记录未修改。');
      }
      messageIds.add(message.id);
      // JSON is inert data. Preserve voice metadata for existing local playback.
      const normalizedMsg = /** @type {ChatMessage} */ ({ ...message, ts: timestamp(message.ts) });
      if (Array.isArray(message.lines)) {
        normalizedMsg.lines = normalizeLines(message.lines);
      } else {
        delete normalizedMsg.lines;
      }
      if (!Number.isSafeInteger(normalizedMsg.serverUserMessageId) || /** @type {number} */ (normalizedMsg.serverUserMessageId) <= 0) {
        delete normalizedMsg.serverUserMessageId;
      }
      if (!Number.isSafeInteger(normalizedMsg.serverMessageId) || /** @type {number} */ (normalizedMsg.serverMessageId) <= 0) {
        delete normalizedMsg.serverMessageId;
      }
      if (normalizedMsg.turnConflict !== true) {
        delete normalizedMsg.turnConflict;
      }
      return normalizedMsg;
    });
    /** @type {ChatSession} */
    const normalizedSession = {
      id: session.id, title: session.title, messages,
      createdAt: timestamp(session.createdAt), updatedAt: timestamp(session.updatedAt),
      settings: pickSettings(session.settings, sessionDefaults),
    };
    if (typeof session.lastLine === 'string' && session.lastLine) {
      normalizedSession.lastLine = session.lastLine;
    }
    if (typeof session.unconfirmedSettingsVersion === 'number' && session.unconfirmedSettingsVersion > 0) {
      normalizedSession.unconfirmedSettingsVersion = session.unconfirmedSettingsVersion;
    }
    return normalizedSession;
  });
  return {
    sessions,
    activeId: ids.has(data.activeId) ? data.activeId : sessions[0]?.id ?? null,
    global: pickSettings(data.global, globalDefaults),
  };
}

/**
 * @param {PortableChatSnapshot} snapshot
 * @returns {PortableChatSnapshot}
 */
export function portableSnapshot(snapshot) {
  return {
    ...snapshot,
    // Generated audio is a separate cache. Browser recording blobs cannot be
    // restored from JSON, and URLs from other installations may be stale.
    sessions: snapshot.sessions.map(session => {
      const { unconfirmedSettingsVersion: _dropped, lastLine: _droppedLastLine, ...rest } = session;
      return {
        ...rest,
        messages: session.messages.map(message => {
          /** @type {ChatMessage} */
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
        }),
      };
    }),
  };
}
