// 前端通用 API 交互封装：提供商管理、连通性测试、系统配置

/**
 * 获取所有已配置提供商与官方预设
 * @returns {Promise<{ providers: Array, presets: Array }>}
 */
export async function fetchProviders() {
  const res = await fetch('/api/providers');
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || `HTTP ${res.status}`);
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
    throw new Error(err.detail || `HTTP ${res.status}`);
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
    throw new Error(data.detail || `HTTP ${res.status}`);
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
    throw new Error(data.detail || `HTTP ${res.status}`);
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
    return {
      success: false,
      message: data.detail || `HTTP ${res.status}`,
      error: data.detail || `HTTP ${res.status}`,
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
    throw new Error(err.detail || `HTTP ${res.status}`);
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
    throw new Error(data.detail || `HTTP ${res.status}`);
  }
  return data;
}
