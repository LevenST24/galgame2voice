import { requestJson } from '../request.js';

const DEFAULT_TOTAL_BUDGET_MS = 10000;
const DEFAULT_MAX_ATTEMPTS = 3;
const DEFAULT_SINGLE_TIMEOUT_MS = 2500;
const DEFAULT_DELAY_MS = 300;

/**
 * 具有时限与退避的有界中断回复身份查询
 * 按固定协议处理 ready/pending/unavailable/gone/ambiguous
 *
 * @param {object} params
 * @param {string} params.sessionId
 * @param {number} params.userMessageId
 * @param {AbortSignal} [params.signal]
 * @param {Function} [params.checkCurrent] 每次响应与写回前的所有权核验钩子
 * @param {Function} [params.request]
 * @param {number} [params.totalBudgetMs]
 * @param {number} [params.maxAttempts]
 * @param {number} [params.singleTimeoutMs]
 * @param {number} [params.delayMs]
 * @returns {Promise<{ status: 'ready', messageId: number, data: object }>}
 */
export async function resolveTurnReplyIdentity({
  sessionId,
  userMessageId,
  signal,
  checkCurrent,
  request = requestJson,
  totalBudgetMs = DEFAULT_TOTAL_BUDGET_MS,
  maxAttempts = DEFAULT_MAX_ATTEMPTS,
  singleTimeoutMs = DEFAULT_SINGLE_TIMEOUT_MS,
  delayMs = DEFAULT_DELAY_MS,
}) {
  if (typeof sessionId !== 'string' || !sessionId.trim()) {
    throw new Error('无效的会话标识，原记录未修改。');
  }
  if (!Number.isSafeInteger(userMessageId) || userMessageId <= 0) {
    throw new Error('无效的用户消息标识，原记录未修改。');
  }
  if (signal?.aborted) throw new DOMException('操作已取消', 'AbortError');
  checkCurrent?.();

  const deadline = performance.now() + totalBudgetMs;
  let attempt = 0;
  let lastStatus = null;

  while (attempt < maxAttempts) {
    attempt++;
    checkCurrent?.();
    const remainingMs = deadline - performance.now();
    if (remainingMs <= 0) break;

    const requestTimeoutMs = Math.min(singleTimeoutMs, Math.max(50, Math.floor(remainingMs)));
    const query = new URLSearchParams({
      session_id: sessionId,
      user_message_id: String(userMessageId),
    });
    const url = `/api/chat/turn-reply?${query.toString()}`;

    let data;
    try {
      data = await request(url, { method: 'GET', signal }, { timeoutMs: requestTimeoutMs });
    } catch (err) {
      if (err?.name === 'AbortError' || signal?.aborted) {
        throw new DOMException('操作已取消', 'AbortError');
      }
      if (err?.code === 'REQUEST_TIMEOUT') {
        lastStatus = 'timeout';
      } else if (err?.status === 401 || err?.status === 403) {
        throw err;
      } else {
        lastStatus = 'unavailable';
      }
    }

    if (data && typeof data === 'object') {
      // 校验回显键
      if (data.session_id !== sessionId || data.user_message_id !== userMessageId) {
        throw new Error('服务返回的会话或消息标识不匹配，原记录未修改。');
      }

      if (data.status === 'ready') {
        if (!Number.isSafeInteger(data.message_id) || data.message_id <= 0) {
          throw new Error('服务未返回有效的回复编号，原记录未修改。');
        }
        checkCurrent?.();
        return {
          status: 'ready',
          messageId: data.message_id,
          data,
        };
      }

      if (data.status === 'gone') {
        throw new Error('对话已被删除或清空，无法关联中断回复，原音频记录已保留。');
      }

      if (data.status === 'ambiguous') {
        throw new Error('存在多条匹配的回复记录，无法确认身份，原记录未修改。');
      }

      if (data.status === 'pending') {
        lastStatus = 'pending';
      } else if (data.status === 'unavailable') {
        lastStatus = 'unavailable';
      } else {
        throw new Error('服务返回了未知的回复状态，原记录未修改。');
      }
    }

    if (attempt >= maxAttempts) break;

    const sleepBudget = deadline - performance.now();
    if (sleepBudget <= 0) break;
    const sleepMs = Math.min(delayMs, sleepBudget);

    await new Promise((resolve, reject) => {
      let t;
      const onAbort = () => {
        clearTimeout(t);
        reject(new DOMException('操作已取消', 'AbortError'));
      };
      t = setTimeout(() => {
        signal?.removeEventListener('abort', onAbort);
        resolve();
      }, sleepMs);
      if (signal?.aborted) onAbort();
      else signal?.addEventListener('abort', onAbort, { once: true });
    });
  }

  if (signal?.aborted) throw new DOMException('操作已取消', 'AbortError');
  checkCurrent?.();

  if (lastStatus === 'pending') {
    throw new Error('中断回复尚未就绪，原记录已保留，请稍后重试。');
  }
  if (lastStatus === 'unavailable') {
    throw new Error('服务暂不可读，原记录已保留，请稍后重试。');
  }
  throw new Error('查询中断回复超时，原记录已保留，请稍后重试。');
}
