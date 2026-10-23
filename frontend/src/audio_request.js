// Include body reads and decoding in the deadline, not just HTTP headers.
export async function withAudioDeadline(operation, { signal, timeoutMs = 15000 } = {}) {
  const controller = new AbortController();
  if (signal?.aborted) throw new DOMException('播放已取消', 'AbortError');
  let timer;
  let cancel;
  const interrupted = new Promise((_, reject) => {
    cancel = () => {
      reject(new DOMException('播放已取消', 'AbortError'));
      controller.abort();
    };
    signal?.addEventListener('abort', cancel, { once: true });
    timer = setTimeout(() => {
      const error = new Error('音频加载超时，请稍后在 LOG 里再点「语音」重试。');
      error.code = 'AUDIO_TIMEOUT';
      reject(error);
      controller.abort();
    }, timeoutMs);
  });
  try {
    return await Promise.race([operation(controller.signal), interrupted]);
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener('abort', cancel);
  }
}
