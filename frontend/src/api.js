// Settings operations have deadlines and never retry writes automatically.
import { requestJson } from './request.js';
export { getErrorMessage, requestJson } from './request.js';

const post = (body) => ({
  method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
});
export const fetchProviders = () => requestJson('/api/providers');
export const fetchProvider = (id) => requestJson(`/api/providers/${encodeURIComponent(id)}`);
export const saveProvider = (payload) => requestJson('/api/providers', post(payload));
export const activateProvider = (id) => requestJson(`/api/providers/${encodeURIComponent(id)}/activate`, { method: 'POST' });
export const fetchConfig = () => requestJson('/api/config');
export const saveConfig = (payload) => requestJson('/api/config', post(payload));
export const fetchSystemVersion = (checkRemote = true) => requestJson(`/api/system/version?check_remote=${checkRemote ? 'true' : 'false'}`, {}, { timeoutMs: 60000 });
export const checkSystemUpdate = () => requestJson('/api/system/update/check', {}, { timeoutMs: 60000 });
export const applySystemUpdate = (payload = {}) => requestJson('/api/system/update/apply', post(payload), { timeoutMs: 120000 });

export async function testProviderConnection(payload) {
  try {
    return await requestJson('/api/providers/test', post(payload), { timeoutMs: 60000 });
  } catch (error) {
    if (!error.status) throw error;
    return { success: false, message: error.message, error: error.message,
      diagnostic: error.data?.diagnostic, latency_ms: 0 };
  }
}

export { getConsoleToken, setConsoleToken, clearConsoleToken, promptConsoleToken } from './auth.js';
