// 对接 Galgame2Voice 本地后端的流式聊天客户端
// SSE 事件：text（增量中文）→ audio_chunk（逐句 GPT-SoVITS 音频 URL）→ done / error
import { getErrorMessage, connectionMessage } from './request.js';

const MAX_VOICE_WAIT_MS = 600000;
const KEPT_LINES = '已收到的台词保留在 LOG 里。';
// Received reply text is saved with the turn. Do not tell the user to resend it.
const idleTimeoutMessage = receivedReply => (receivedReply
  ? `回复等待超时，${KEPT_LINES}请在「设置中心」检查网络、模型与语音服务。`
  : '回复等待超时，已保留收到的内容。请在「设置中心」检查网络、模型与语音服务，稍后重新发送上一条消息。');
const streamClosedMessage = receivedReply => (receivedReply
  ? `回复连接提前结束，${KEPT_LINES}请检查服务与网络。`
  : '回复连接提前结束，已保留收到的内容。请检查服务与网络，稍后重新发送上一条消息。');

function isReplyProgress(event, data) {
  if (!data || typeof data !== 'object' || Array.isArray(data)) return false;
  const text = value => typeof value === 'string' && value.trim().length > 0;
  if (event === 'text' || event === 'message') return text(data.delta_chinese);
  if (event === 'line') return Number.isInteger(data.index) && data.index >= 0 && (text(data.zh) || text(data.ja));
  if (event === 'audio_chunk') return text(data.audio_url);
  if (event === 'audio_chunk_error') return text(data.error);
  return event === 'line_audio' && Number.isInteger(data.line) && data.line >= 0
    && ['ready', 'failed', 'skipped'].includes(data.status);
}

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
  let receivedReply = false;
  // Old servers cannot announce slow voice work. Keep a finite compatibility
  // window while retaining the original transport idle timeout per read.
  let progressPolicy = false;
  let voiceBudgetMs = MAX_VOICE_WAIT_MS;
  let progressDeadline = performance.now() + Math.max(idleTimeoutMs, MAX_VOICE_WAIT_MS);
  const voiceJobs = new Map();
  const seenVoiceJobs = new Set();
  const progressBudget = () => progressPolicy ? idleTimeoutMs : Math.max(idleTimeoutMs, MAX_VOICE_WAIT_MS);
  const updateVoiceJob = data => {
    if (!progressPolicy || !Number.isInteger(data?.index) || data.index < 0) return;
    if (data.status === 'started' && !seenVoiceJobs.has(data.index)) {
      seenVoiceJobs.add(data.index);
      voiceJobs.set(data.index, performance.now() + voiceBudgetMs);
      progressDeadline = performance.now() + idleTimeoutMs;
    } else if (data.status === 'finished' && voiceJobs.delete(data.index)) {
      progressDeadline = performance.now() + idleTimeoutMs;
    }
  };
  const idleTimeoutError = () => {
    timedOut = true;
    errorCode = 'stream_idle_timeout';
    return new Error(idleTimeoutMessage(receivedReply));
  };
  const withDeadline = async (operation) => {
    if (controller.signal.aborted) throw new DOMException('操作已取消', 'AbortError');
    const deadline = Math.max(progressDeadline, ...voiceJobs.values());
    const remaining = Math.min(idleTimeoutMs, deadline - performance.now());
    if (remaining <= 0) {
      const error = idleTimeoutError();
      controller.abort();
      throw error;
    }
    let timer;
    let abort;
    const cancelled = new Promise((_, reject) => {
      abort = () => reject(new DOMException('操作已取消', 'AbortError'));
      if (controller.signal.aborted) abort();
      else controller.signal.addEventListener('abort', abort, { once: true });
    });
    try {
      return await Promise.race([operation(), cancelled, new Promise((_, reject) => {
        timer = setTimeout(() => {
          reject(idleTimeoutError());
          controller.abort();
        }, remaining);
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
      ...(Number.isSafeInteger(doneMeta?.message_id) && doneMeta.message_id > 0 ? { messageId: doneMeta.message_id } : {}),
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
    // Only the explicit 'legacy' opt-out asks for the old whole-reply format.
    body.reply_format = settings.advReplyFormat === 'legacy' ? 'old' : 'lines';
    if (typeof settings.advOutfit === 'string' && settings.advOutfit) {
      body.outfit = settings.advOutfit;
    }
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
      const res = await withDeadline(() => fetchRequest('/api/chat/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
        signal: controller.signal,
      }));
      if (settled) return;
      if (!res.ok) {
        let msg = `请求失败 (${res.status})`;
        try {
          const data = await withDeadline(() => res.json());
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
      const advertisedBudget = Number(res.headers?.get('X-G2V-Voice-Job-Timeout-Ms'));
      progressPolicy = res.headers?.get('X-G2V-Progress-Policy') === '1'
        && Number.isFinite(advertisedBudget) && advertisedBudget > 0 && advertisedBudget <= MAX_VOICE_WAIT_MS;
      if (progressPolicy) {
        voiceBudgetMs = advertisedBudget;
        progressDeadline = performance.now() + idleTimeoutMs;
      }
      reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      let eventName = 'text';
      while (!settled) {
        const { done, value } = await withDeadline(() => reader.read());
        if (done) {
          buffer += decoder.decode();
        } else {
          buffer += decoder.decode(value, { stream: true });
        }
        const lines = buffer.split(/\r?\n/);
        buffer = done ? '' : (lines.pop() || '');
        for (const rawLine of lines) {
          if (settled) return;
          const line = rawLine.replace(/\r$/, '');
          if (!line.trim()) {
            eventName = 'text';
            continue;
          }
          const eventMatch = line.match(/^event:\s*(.*)$/);
          if (eventMatch) {
            eventName = eventMatch[1].trim() || 'text';
            continue;
          }
          const dataMatch = line.match(/^data:\s*(.*)$/);
          if (!dataMatch) continue;
          const payload = dataMatch[1];
          let json;
          try {
            json = JSON.parse(payload);
          } catch {
            eventName = 'text';
            continue;
          }
          const currentEvent = eventName;
          eventName = 'text';
          // Transport heartbeats and partial/malformed frames do not prove
          // that the model or an audio job has made progress.
          if (currentEvent === 'voice_job') updateVoiceJob(json);
          if (isReplyProgress(currentEvent, json)) {
            if (currentEvent === 'text' || currentEvent === 'message' || currentEvent === 'line') receivedReply = true;
            progressDeadline = performance.now() + progressBudget();
          }
          if (currentEvent === 'text' || currentEvent === 'message') {
            const delta = json.delta_chinese || '';
            if (delta) {
              acc += delta;
              onChunk?.(acc, delta);
            }
          } else if (currentEvent === 'audio_chunk') {
            onEvent?.(currentEvent, json);
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
          } else if (currentEvent === 'audio_chunk_error') {
            onEvent?.(currentEvent, json);
            audioError ||= getErrorMessage(json, '本轮角色语音暂不可用，文字回复已保留。请在「设置中心 → 运行状态」启动引擎，或切换 CPU 稳定模式后重试。');
            console.warn('[streamChat] audio chunk error:', json.error, json.sentence);
          } else if (currentEvent === 'error') {
            errorCode = typeof json.error_code === 'string' ? json.error_code : null;
            const fallback = receivedReply
              ? `对话暂时未完成，${KEPT_LINES}请检查「设置中心」中的模型地址、密钥与模型名称。`
              : '对话暂时未完成，请检查「设置中心」中的模型地址、密钥与模型名称，再重新发送消息。';
            finish(false, getErrorMessage(json, fallback));
            return;
          } else if (currentEvent === 'done') {
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
              onChunk?.(acc, '');
            }
            finish(false, null);
            return;
          } else {
            onEvent?.(currentEvent, json);
          }
        }
        if (done) break;
      }
      finish(false, streamClosedMessage(receivedReply));
    } catch (err) {
      if (timedOut) {
        finish(false, idleTimeoutMessage(receivedReply));
        return;
      }
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
