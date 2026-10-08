import { requestJson } from '../api.js';

export async function waitForEngine({ timeoutMs = 180000, intervalMs = 1000, onProgress = () => {} } = {}) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const data = await requestJson('/api/system/engine-status', {}, { timeoutMs: 10000 });
    onProgress(data);
    if (data.ready === true && data.state === 'ready') return data;
    if (data.state === 'failed' || data.state === 'unavailable') throw new Error(data.message);
    await new Promise(resolve => setTimeout(resolve, intervalMs));
  }
  return { ready: false, state: 'starting', message: '引擎仍在加载，CPU 首次启动可能较慢。请稍后点击「刷新状态」，无需反复重启；文字聊天可继续使用。' };
}
