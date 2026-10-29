import { activateProvider, requestJson, saveProvider } from '../api.js';

function uncertain(error) {
  return error?.name !== 'AbortError' && (error?.code === 'REQUEST_TIMEOUT' || !error?.status);
}

function matches(provider, payload) {
  return provider && Object.entries(payload)
    .filter(([key, value]) => key !== 'api_key' && value !== undefined)
    .every(([key, value]) => provider[key] === value);
}

function unconfirmed(action) {
  const error = new Error(`模型${action}结果尚未确认，当前输入已保留。请刷新模型列表核对后再决定是否重新保存。`);
  error.code = 'PROVIDER_UNCONFIRMED';
  return error;
}

async function readConfirmedProvider(payload) {
  const current = await requestJson(`/api/providers/${encodeURIComponent(payload.id)}`,
    { cache: 'no-store' }, { timeoutMs: 10000 }).catch(() => null);
  return matches(current?.provider, payload) ? current.provider : null;
}

export async function saveConfirmedProvider(payload) {
  try {
    return await saveProvider(payload);
  } catch (error) {
    if (!uncertain(error)) throw error;
    // Masked keys cannot prove that the newly entered credential was stored.
    // Only check non-secret edits; never retrieve plaintext or repeat the write.
    if (!payload.api_key) {
      const provider = await readConfirmedProvider(payload);
      if (provider) return { provider, confirmed_after_disconnect: true };
    }
    throw unconfirmed('保存');
  }
}

export async function activateConfirmedProvider(payload) {
  const expected = { ...payload, is_active: true };
  try {
    const result = await activateProvider(payload.id);
    if (matches(result?.active_provider, expected)) return result;
    throw unconfirmed('启用');
  } catch (error) {
    if (!uncertain(error)) throw error;
    // The save was already acknowledged, so checking the active configuration
    // here does not need to compare the key again.
    const provider = await readConfirmedProvider(expected);
    if (provider) return { active_provider: provider, confirmed_after_disconnect: true };
    throw unconfirmed('启用');
  }
}
