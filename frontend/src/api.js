// Settings operations have deadlines and never retry writes automatically.
import { requestJson } from './request.js';
export { getErrorMessage, requestJson } from './request.js';

const post = (body) => ({
  method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
});
export const fetchProviders = () => requestJson('/api/providers');
export const saveProvider = (payload) => requestJson('/api/providers', post(payload));
export const activateProvider = (id) => requestJson(`/api/providers/${encodeURIComponent(id)}/activate`, { method: 'POST' });
export const fetchConfig = () => requestJson('/api/config');
export const saveConfig = (payload) => requestJson('/api/config', post(payload));
export const fetchSystemVersion = (checkRemote = true) => requestJson(`/api/system/version?check_remote=${checkRemote ? 'true' : 'false'}`, {}, { timeoutMs: 60000 });

/** 把连通性测试的 HTTP 错误还原成界面用的结果，并保留服务端诊断字段。没有 HTTP 状态时原样抛出。 */
export function providerTestResultFromError(error) {
  if (!error.status) throw error;
  const data = error.data || {};
  return {
    success: false,
    message: error.message,
    error: data.error || error.message,
    error_code: data.error_code || null,
    error_title: data.error_title || data.error || null,
    diagnostic: data.diagnostic || null,
    status_code: data.status_code ?? error.status,
    latency_ms: 0,
    diagnostic_code: data.diagnostic_code ?? null,
    summary_zh: data.summary_zh ?? null,
    action_zh: data.action_zh ?? null,
    retryable: data.retryable ?? null,
  };
}

export async function testProviderConnection(payload, { signal = null } = {}) {
  try {
    const options = post(payload);
    if (signal) options.signal = signal;
    return await requestJson('/api/providers/test', options, { timeoutMs: 60000 });
  } catch (error) {
    return providerTestResultFromError(error);
  }
}

export { getConsoleToken, setConsoleToken, clearConsoleToken, promptConsoleToken } from './auth.js';
