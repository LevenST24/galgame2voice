import { requestJson } from '../api.js';

export async function saveConfirmedConfig(payload) {
  try {
    return await requestJson('/api/config', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
    });
  } catch (error) {
    // A lost response does not prove a failed write. Verify once through GET;
    // never resend the write or retrieve plaintext credentials to compare them.
    if (!payload.telegram_bot_token && error.name !== 'AbortError'
      && (error.code === 'REQUEST_TIMEOUT' || !error.status)) {
      const current = await requestJson('/api/config', {}, { timeoutMs: 10000 }).catch(() => null);
      const entries = Object.entries(payload).filter(([, value]) => value !== undefined);
      if (current?.settings && entries.length
        && entries.every(([key, value]) => current.settings[key] === value)) {
        return { ...current, confirmed_after_disconnect: true };
      }
    }
    throw error;
  }
}
