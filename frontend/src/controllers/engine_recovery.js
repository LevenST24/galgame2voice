import { requestJson } from '../api.js';

export async function waitForEngine({ timeoutMs = 180000, intervalMs = 1000, onProgress = () => {} } = {}) {
  const deadline = performance.now() + timeoutMs;
  let lastState;
  const checking = { ready: false, state: 'checking',
    message: '暂时无法读取引擎状态，正在重新检查；无需再次重启。文字聊天可继续使用。' };
  while (performance.now() < deadline) {
    let data;
    try {
      data = await requestJson('/api/system/engine-status', { cache: 'no-store' }, {
        timeoutMs: Math.max(1, Math.min(10000, deadline - performance.now())),
      });
    } catch (error) {
      // Retry only reads. Authentication/configuration refusals and explicit
      // cancellation need user action; a disconnected read proves no engine failure.
      if (error.name === 'AbortError' || (error.status && error.status < 500 && error.status !== 429)) throw error;
      data = checking;
    }
    const valid = data && typeof data.ready === 'boolean'
      && ['ready', 'starting', 'loading_slow', 'checking', 'failed', 'unavailable'].includes(data.state)
      && data.ready === (data.state === 'ready');
    if (!valid) data = checking;
    lastState = data.state;
    onProgress(data);
    if (data.ready === true && data.state === 'ready') return data;
    if (data.state === 'failed' || data.state === 'unavailable') {
      const error = new Error(data.message);
      error.code = 'ENGINE_NOT_READY';
      error.engineState = data.state;
      throw error;
    }
    const remaining = deadline - performance.now();
    if (remaining > 0) await new Promise(resolve => setTimeout(resolve, Math.min(intervalMs, remaining)));
  }
  if (lastState === 'checking') return { ...checking,
    message: '暂时无法确认语音引擎状态。请保留启动窗口，连接恢复后点击「刷新状态」核对，无需反复重启。' };
  return { ready: false, state: 'loading_slow', message: '引擎加载时间较长，尚未确认就绪。请稍后点击「刷新状态」，无需反复重启；若持续没有进展，可提供 logs/gpt_sovits.log 排查。文字聊天可继续使用。' };
}
