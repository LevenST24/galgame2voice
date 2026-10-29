import { requestJson } from '../api.js';

let pendingMutations = 0;
let mutationTail = Promise.resolve();

export function runSettingsMutation(operation) {
  // Reserve order when the user acts, before awaiting another operation (such
  // as saving a provider). A failed batch must not dispatch queued mutations.
  const idle = pendingMutations === 0;
  const previous = mutationTail;
  pendingMutations += 1;
  let complete, fail;
  const execution = new Promise((resolve, reject) => { complete = resolve; fail = reject; });
  const result = execution.finally(() => { pendingMutations -= 1; });
  mutationTail = result;
  const start = () => {
    try { complete(operation(writeConfirmedConfig)); } catch (error) { fail(error); }
  };
  // Reserve the tail before invoking the operation, including an idle write.
  if (idle) start();
  else previous.then(start, cause => {
    const error = new Error('此前的设置保存尚未完成，当前操作没有发送。输入已保留，请检查服务并核对配置后重新保存或重启。');
    error.code = 'CONFIG_WRITE_SKIPPED';
    error.cause = cause;
    fail(error);
  });
  // Callers still receive the rejection; observing it also covers a queued UI
  // action whose caller has already gone away.
  result.catch(() => {});
  return result;
}

export async function saveConfirmedConfig(payload) {
  const snapshot = JSON.parse(JSON.stringify(payload));
  return runSettingsMutation(save => save(snapshot));
}

async function writeConfirmedConfig(payload) {
  try {
    return await requestJson('/api/config', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
    });
  } catch (error) {
    // A lost response does not prove a failed write. Verify once through GET;
    // never resend the write or retrieve plaintext credentials to compare them.
    if (!payload.telegram_bot_token && error.name !== 'AbortError'
      && (error.code === 'REQUEST_TIMEOUT' || !error.status || error.status >= 500)) {
      const current = await requestJson('/api/config', { cache: 'no-store' }, { timeoutMs: 10000 }).catch(() => null);
      const entries = Object.entries(payload).filter(([, value]) => value !== undefined);
      if (current?.settings && entries.length
        && entries.every(([key, value]) => current.settings[key] === value)) {
        return { ...current, confirmed_after_disconnect: !error.status, confirmed_after_readback: true };
      }
    }
    throw error;
  }
}
