// 对接 Galgame2Voice 本地后端的流式聊天客户端
// SSE 事件：text（增量中文）→ audio_chunk（逐句 GPT-SoVITS 音频 URL）→ done / error
import { getErrorMessage, connectionMessage } from './request.js';

/**
 * 流式请求本地后端
 * @returns {() => void} cancel
 */
export function streamChat({ prompt, sessionId, settings, preset, onChunk, onAudio, onEnd, onEvent, fetch: fetchRequest = globalThis.fetch, idleTimeoutMs = 120000 }) {
  const controller = new AbortController();
  let acc = '';
  const audioUrls = [];
  // 分句元数据（url + 句子 + 情绪）：流式切片时与 audioUrls 同步追加，done 事件的整段音频兜底只填 audioUrls；重播时靠它逐句切换立绘
  const audioChunks = [];
  const jaSentences = [];
  let fullJapanese = '';
  let doneMeta = null;
  let settled = false;
  let reader = null;
  let timedOut = false;
  let audioError = null;
  let errorCode = null;
  const withDeadline = async (operation) => {
    let timer;
    let abort;
    const cancelled = new Promise((_, reject) => {
      abort = () => reject(new DOMException('操作已取消', 'AbortError'));
      if (controller.signal.aborted) abort();
      else controller.signal.addEventListener('abort', abort, { once: true });
    });
    try {
      return await Promise.race([operation, cancelled, new Promise((_, reject) => {
        timer = setTimeout(() => {
          timedOut = true;
          reject(new Error('回复等待超时，已保留收到的内容。请检查网络或模型服务，稍后重新发送上一条消息。'));
          controller.abort();
        }, idleTimeoutMs);
      })]);
    } finally {
      clearTimeout(timer);
      controller.signal.removeEventListener('abort', abort);
    }
  };

  const finish = (cancelled, error) => {
    if (settled) return;
    settled = true;
    // 统一用一个对象回传。之前是位置参数 onEnd(acc, cancelled, error, urls, meta)，
    // 而调用方按 (finalText, meta, error, cancelled) 接，meta 实际收到布尔值，
    // 日文原文/情绪/音频列表整批静默丢失，cancelled 恒为真导致收尾逻辑不执行。
    onEnd(acc, {
      cancelled: Boolean(cancelled),
      truncated: Boolean(doneMeta && doneMeta.truncated),
      error: error || null,
      errorCode: error ? errorCode : null,
      audioError,
      japanese: fullJapanese || jaSentences.join(''),
      audioUrls: [...audioUrls],
      chunks: audioChunks.map((c) => ({ ...c })),
      emotion: (doneMeta && doneMeta.emotion) || null,
      affection: (doneMeta && doneMeta.affection) || null,
      ttsParams: (doneMeta && doneMeta.tts_params) || null,
      lines: (doneMeta && Array.isArray(doneMeta.lines)) ? doneMeta.lines : null,
    });
  };

  const body = { prompt, session_id: sessionId, stream: true };
  if (settings) {
    if (settings.voiceProfileId) {
      body.voice_profile_id = settings.voiceProfileId;
    }
    // 人设提示词由后端按当前角色解析（权威源是角色包），前端不再随请求带上
    // 一份会话私有副本 —— 那份副本会压过角色值，造成同一角色设定不一致。
    if (typeof settings.temperature === 'number') body.temperature = settings.temperature;
    if (typeof settings.maxContext === 'number') body.max_context = Math.round(settings.maxContext);
    if (typeof settings.topP === 'number') body.top_p = settings.topP;
    if (typeof settings.maxTokens === 'number') body.max_tokens = Math.round(settings.maxTokens);
    if (typeof settings.freqPenalty === 'number') body.frequency_penalty = settings.freqPenalty;
    if (typeof settings.presPenalty === 'number') body.presence_penalty = settings.presPenalty;
    const ttsOpts = {};
    // voice_profile_id 走 ChatRequest 顶层字段；塞进 tts_options 会被后端
    // ChatTtsOptions(extra="forbid") 的白名单判为非法参数并返回 422。
    if (typeof settings.ttsSpeed === 'number') ttsOpts.speed = settings.ttsSpeed;
    if (typeof settings.ttsTopK === 'number') ttsOpts.top_k = Math.round(settings.ttsTopK);
    if (typeof settings.ttsTopP === 'number') ttsOpts.top_p = settings.ttsTopP;
    if (typeof settings.ttsTemperature === 'number') ttsOpts.temperature = settings.ttsTemperature;
    if (typeof settings.aiAdaptiveVoice === 'boolean') {
      ttsOpts.ai_adaptive_voice = settings.aiAdaptiveVoice;
      body.ai_adaptive_voice = settings.aiAdaptiveVoice;
    }
    if (Object.keys(ttsOpts).length) body.tts_options = ttsOpts;
  }
  if (preset) body.preset = preset;

  (async () => {
    try {
      const res = await withDeadline(fetchRequest('/api/chat/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
        signal: controller.signal,
      }));
      if (settled) return;
      if (!res.ok) {
        let msg = `请求失败 (${res.status})`;
        try {
          const data = await withDeadline(res.json());
          msg = getErrorMessage(data, `HTTP ${res.status}`);
        } catch (error) {
          if (timedOut || controller.signal.aborted) throw error;
          /* 忽略非 JSON 错误体 */
        }
        finish(false, msg);
        return;
      }
      if (!res.body) {
        finish(false, '当前环境不支持流式响应');
        return;
      }
      reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      let eventName = 'message';
      while (!settled) {
        const { done, value } = await withDeadline(reader.read());
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop() || '';
        for (const line of lines) {
          if (settled) return;
          const trimmed = line.trim();
          if (!trimmed) continue;
          if (trimmed.startsWith('event: ')) {
            eventName = trimmed.slice(7).trim();
            continue;
          }
          if (!trimmed.startsWith('data: ')) continue;
          const payload = trimmed.slice(6);
          let json;
          try {
            json = JSON.parse(payload);
          } catch {
            continue;
          }
          if (eventName === 'text') {
            const delta = json.delta_chinese || '';
            if (delta) {
              acc += delta;
              onChunk(acc, delta);
            }
          } else if (eventName === 'audio_chunk') {
            onEvent?.(eventName, json);
            if (settled) return;
            if (json.sentence) {
              jaSentences.push(json.sentence);
            }
            if (json.audio_url) {
              audioUrls.push(json.audio_url);
              const chunkObj = {
                url: json.audio_url,
                sentence: json.sentence || '',
                emotion: json.emotion || '',
              };
              if (typeof json.line === 'number') chunkObj.line = json.line;
              if (typeof json.index === 'number') chunkObj.index = json.index;
              audioChunks.push(chunkObj);
              if (typeof onAudio === 'function') {
                onAudio(json.audio_url, audioUrls.length - 1, json.sentence || '', {
                  emotion: json.emotion || '',
                  line: json.line,
                  chunkIndex: json.index,
                });
              }
            }
          } else if (eventName === 'audio_chunk_error') {
            onEvent?.(eventName, json);
            audioError ||= getErrorMessage(json, '本轮角色语音暂不可用，文字回复已保留。请在「全局设置 → 运行状态」启动引擎，或切换 CPU 稳定模式后重试。');
            console.warn('[streamChat] audio chunk error:', json.error, json.sentence);
          } else if (eventName === 'error') {
            errorCode = typeof json.error_code === 'string' ? json.error_code : null;
            finish(false, getErrorMessage(json, '对话暂时未完成，请检查「全局设置」中的模型地址、密钥与模型名称，再重新发送消息。'));
            return;
          } else if (eventName === 'done') {
            doneMeta = json;
            if (json.japanese) {
              fullJapanese = json.japanese;
            }
            if (Array.isArray(json.chunks) && json.chunks.length > audioUrls.length) {
              // The final list is ordered. Repeated sentences share cache URLs
              // but still occupy separate positions in the reply.
              for (const c of json.chunks.slice(audioUrls.length)) {
                if (c && c.audio_url) {
                  audioUrls.push(c.audio_url);
                  const chunkObj = {
                    url: c.audio_url,
                    sentence: c.sentence || '',
                    emotion: c.emotion || '',
                  };
                  if (typeof c.line === 'number') chunkObj.line = c.line;
                  if (typeof c.index === 'number') chunkObj.index = c.index;
                  audioChunks.push(chunkObj);
                }
              }
            } else if (audioUrls.length === 0) {
              if (json.audio_url || json.total_audio_url) {
                audioUrls.push(json.audio_url || json.total_audio_url);
              }
            }
            // 以服务器保存的终稿为准：流式阶段的开场白或解析残片不能留在气泡和本地记录里。
            if (typeof json.chinese === 'string' && json.chinese && json.chinese !== acc) {
              acc = json.chinese;
              onChunk(acc, '');
            }
            finish(false, null);
            return;
          } else {
            onEvent?.(eventName, json);
          }
        }
      }
      finish(false, '回复连接提前结束，已保留收到的内容。请检查服务与网络，稍后重新发送上一条消息。');
    } catch (err) {
      if (err && err.name === 'AbortError' && !timedOut) {
        finish(true, null);
        return;
      }
      finish(false, connectionMessage(err));
    } finally {
      if (reader) {
        try { await reader.cancel(); } catch { /* Already closed by abort. */ }
        try { reader.releaseLock(); } catch { /* Pending native cancellation. */ }
      }
    }
  })();

  return () => { controller.abort(); finish(true, null); };
}
