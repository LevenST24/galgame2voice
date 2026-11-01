import { requestJson } from './api.js';

const writes = new Map();
const deletions = new Map();
const deleted = new Set();

export const isSessionDeleting = id => deletions.has(id);

// Serialize before yielding so later UI edits cannot change an in-flight save.
export function persistSession(session) {
  if (deletions.has(session.id) || deleted.has(session.id)) {
    return Promise.reject(new Error('该对话正在删除或已删除，请新建对话后保存。'));
  }
  const options = {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ id: session.id, title: session.title,
      voice_profile_id: session.settings?.voiceProfileId ?? null, settings: session.settings }),
  };
  const pending = writes.get(session.id);
  const write = pending
    ? pending.catch(() => {}).then(() => requestJson('/api/chat/sessions', options))
    : requestJson('/api/chat/sessions', options);
  writes.set(session.id, write);
  const cleanup = () => { if (writes.get(session.id) === write) writes.delete(session.id); };
  write.then(cleanup, cleanup);
  return write;
}

export function deletePersistedSession(id) {
  if (deletions.has(id)) return deletions.get(id);
  // Let earlier saves finish first; a delayed upsert must not recreate a deletion.
  const removal = (writes.get(id) || Promise.resolve()).catch(() => {}).then(async () => {
    const result = await requestJson(`/api/chat/sessions/${encodeURIComponent(id)}`, { method: 'DELETE' });
    deleted.add(id);
    return result;
  });
  deletions.set(id, removal);
  const cleanup = () => { deletions.delete(id); };
  removal.then(cleanup, cleanup);
  return removal;
}

// A confirmed import brings these ids back on the server, so an earlier deletion no longer applies.
export function forgetDeletedSessions(ids) {
  for (const id of ids) deleted.delete(id);
}

export async function fetchAllSessions() {
  const sessions = [];
  for (let offset = 0; ; offset += 200) {
    const page = await requestJson(`/api/chat/sessions?limit=200&offset=${offset}`);
    if (!Array.isArray(page.sessions)) throw new Error('对话列表无法读取，请重试。');
    sessions.push(...page.sessions);
    if (!page.has_more) return sessions;
  }
}

export async function fetchSessionHistoryPage(sessionId, { limit = 100, beforeId = null } = {}) {
  const params = new URLSearchParams({
    session_id: sessionId,
    limit: String(limit),
  });
  if (Number.isInteger(beforeId) && beforeId > 0) {
    params.set('before_id', String(beforeId));
  }
  return requestJson(`/api/chat/history?${params.toString()}`);
}

export function earliestServerMessageId(messages) {
  let oldest = null;
  for (const m of messages || []) {
    const match = typeof m?.id === 'string' ? /^m_(\d+)$/.exec(m.id) : null;
    if (match) {
      const num = Number(match[1]);
      if (Number.isInteger(num) && num > 0 && (oldest === null || num < oldest)) {
        oldest = num;
      }
    }
  }
  return oldest;
}
