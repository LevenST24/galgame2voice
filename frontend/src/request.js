// Bounded JSON requests and actionable messages shared by settings controllers.
export function statusMessage(status) {
  if (status === 401) return '需要访问令牌，请在弹出的验证窗口中输入启动目录 data/.console_token 中的令牌。';
  if (status === 403) return '当前操作未获授权，请检查访问令牌或服务地址。';
  if (status === 404) return '此配置或功能已不存在，请刷新列表后重新选择。';
  if (status === 422 || status === 400) return '配置不完整或数值超出范围，请检查填写内容后重新保存。';
  if (status === 429) return '请求过于频繁，请稍候再试。';
  return '此次操作未完成，请稍后重试；若仍失败，请打开「状态诊断」检查服务。';
}

export function getErrorMessage(data, fallback = '请求失败') {
  const detail = data?.detail ?? data?.message ?? data?.error;
  const message = typeof detail === 'string' ? detail
    : (!Array.isArray(detail) && detail && (detail.message || detail.error));
  if (typeof message === 'string' && /[\u3400-\u9fff]/.test(message)) return message;
  if (Array.isArray(detail)) return '配置不完整或数值超出范围，请检查填写内容后重新保存。';
  const http = /^HTTP (\d+)$/.exec(fallback);
  return http ? statusMessage(Number(http[1])) : fallback;
}

export function connectionMessage(error) {
  if (error?.code === 'REQUEST_TIMEOUT') return error.message;
  if (error?.name === 'TypeError' || /failed to fetch|network|load failed|fetch failed/i.test(error?.message || '')) {
    return '无法连接本机服务。请保留启动窗口；若已关闭，请双击「启动.bat」，然后刷新网页再试。';
  }
  return /[\u3400-\u9fff]/.test(error?.message || '') ? error.message : '连接中断，请检查服务与网络后再试。';
}

export async function requestJson(url, options = {}, { timeoutMs = 30000 } = {}) {
  const controller = new AbortController();
  const external = options.signal;
  if (external?.aborted) throw new DOMException('操作已取消', 'AbortError');
  let timer;
  let cancel;
  const interrupted = new Promise((_, reject) => {
    cancel = () => {
      controller.abort();
      reject(new DOMException('操作已取消', 'AbortError'));
    };
    if (external?.aborted) cancel();
    else external?.addEventListener('abort', cancel, { once: true });
    if (timeoutMs > 0) timer = setTimeout(() => {
      const error = new Error('等待服务响应超时。请打开「状态诊断」检查；保存结果可能尚未确认，请刷新配置后再决定是否重试。');
      error.code = 'REQUEST_TIMEOUT';
      reject(error);
      controller.abort();
    }, timeoutMs);
  });
  try {
    const operation = (async () => {
      const response = await fetch(url, { ...options, signal: controller.signal });
      let data;
      try { data = await response.json(); } catch {
        if (response.ok) throw new Error('服务返回了无法读取的结果，请刷新网页后重试；若仍失败，请重新启动程序。');
      }
      if (!response.ok) {
        const error = new Error(getErrorMessage(data, `HTTP ${response.status}`));
        error.status = response.status;
        error.data = data;
        throw error;
      }
      return data;
    })();
    return await Promise.race([operation, interrupted]);
  } catch (error) {
    if (error.name === 'AbortError' || error.status || error.code) throw error;
    throw new Error(connectionMessage(error));
  } finally {
    clearTimeout(timer);
    external?.removeEventListener('abort', cancel);
  }
}
