// 前端通用 API 交互封装：提供商管理、连通性测试、系统配置

/**
 * Unpacks error messages from structured backend responses.
 * Prevents displaying [object Object] when detail is an object (e.g. {code, message}).
 * @param {any} errData
 * @param {string} fallback
 * @returns {string}
 */
export function getErrorMessage(errData, fallback = '请求失败') {
  if (!errData) return fallback;
  const detail = errData.detail !== undefined ? errData.detail : errData.message;
  if (typeof detail === 'string') return detail;
  if (detail && typeof detail === 'object') {
    if (typeof detail.message === 'string') return detail.message;
    if (typeof detail.error === 'string') return detail.error;
    try {
      return JSON.stringify(detail);
    } catch (_) {}
  }
  return fallback;
}

/**
 * 获取所有已配置提供商与官方预设
 * @returns {Promise<{ providers: Array, presets: Array }>}
 */
export async function fetchProviders() {
  const res = await fetch('/api/providers');
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(getErrorMessage(err, `HTTP ${res.status}`));
  }
  return res.json();
}

/**
 * 获取单个提供商详情
 * @param {string} providerId
 * @returns {Promise<{ provider: Object }>}
 */
export async function fetchProvider(providerId) {
  const res = await fetch(`/api/providers/${encodeURIComponent(providerId)}`);
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(getErrorMessage(err, `HTTP ${res.status}`));
  }
  return res.json();
}

/**
 * 保存 / 更新提供商配置 (包含自定义端点、API Key、Chat Model 等)
 * @param {Object} payload
 * @returns {Promise<Object>}
 */
export async function saveProvider(payload) {
  const res = await fetch('/api/providers', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(getErrorMessage(data, `HTTP ${res.status}`));
  }
  return data;
}

/**
 * 激活指定提供商为全局生效模型
 * @param {string} providerId
 * @returns {Promise<Object>}
 */
export async function activateProvider(providerId) {
  const res = await fetch(`/api/providers/${encodeURIComponent(providerId)}/activate`, {
    method: 'POST',
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(getErrorMessage(data, `HTTP ${res.status}`));
  }
  return data;
}

/**
 * 发起即时连通性测试 (In-flight test)
 * @param {Object} payload { id, api_base_url, api_key, chat_model }
 * @returns {Promise<{ success: boolean, latency_ms: number, message?: string, error?: string, diagnostic?: string, models?: Array }>}
 */
export async function testProviderConnection(payload) {
  const res = await fetch('/api/providers/test', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    // 处理 HTTP 异常状态码响应 (如 400, 422 等)
    const errMsg = getErrorMessage(data, `HTTP ${res.status}`);
    return {
      success: false,
      message: errMsg,
      error: errMsg,
      diagnostic: data.diagnostic,
      latency_ms: 0,
    };
  }
  return data;
}

/**
 * 获取全局设置 (包含 STT 引擎、Telegram、TTS 等)
 * @returns {Promise<Object>}
 */
export async function fetchConfig() {
  const res = await fetch('/api/config');
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(getErrorMessage(err, `HTTP ${res.status}`));
  }
  return res.json();
}

/**
 * 保存全局设置
 * @param {Object} payload
 * @returns {Promise<Object>}
 */
export async function saveConfig(payload) {
  const res = await fetch('/api/config', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(getErrorMessage(data, `HTTP ${res.status}`));
  }
  return data;
}

/**
 * 获取系统当前版本与 Git 远程更新状态
 * @param {boolean} checkRemote 是否检查远程仓库新提交
 * @returns {Promise<Object>}
 */
export async function fetchSystemVersion(checkRemote = true) {
  const res = await fetch(`/api/system/version?check_remote=${checkRemote ? 'true' : 'false'}`);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(getErrorMessage(data, `HTTP ${res.status}`));
  }
  return data;
}

/**
 * 检查 GitHub 远程是否有可用更新
 * @returns {Promise<Object>}
 */
export async function checkSystemUpdate() {
  const res = await fetch('/api/system/update/check');
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(getErrorMessage(data, `HTTP ${res.status}`));
  }
  return data;
}

/**
 * 一键拉取并应用 GitHub 更新
 * @param {Object} [payload]
 * @returns {Promise<Object>}
 */
export async function applySystemUpdate(payload = {}) {
  const res = await fetch('/api/system/update/apply', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(getErrorMessage(data, data.output || `HTTP ${res.status}`));
  }
  return data;
}

export { getConsoleToken, setConsoleToken, clearConsoleToken, promptConsoleToken } from './auth.js';
