// 对接 Galgame2Voice 本地后端的流式聊天客户端
// SSE 事件：text（增量中文）→ audio_chunk（逐句 GPT-SoVITS 音频 URL）→ done / error

/**
 * 流式请求本地后端
 * @returns {() => void} cancel
 */
export function streamChat({ prompt, sessionId, settings, preset, onChunk, onAudio, onEnd }) {
  const controller = new AbortController();
  let acc = '';
  const audioUrls = [];
  // 与 audioUrls 一一对应的分句元数据，重播时靠它驱动立绘逐句切换
  const audioChunks = [];
  const jaSentences = [];
  let fullJapanese = '';
  let doneMeta = null;
  let settled = false;

  const finish = (cancelled, error) => {
    if (settled) return;
    settled = true;
    // 统一用一个对象回传。之前是位置参数 onEnd(acc, cancelled, error, urls, meta)，
    // 而调用方按 (finalText, meta, error, cancelled) 接，meta 实际收到布尔值，
    // 日文原文/情绪/音频列表整批静默丢失，cancelled 恒为真导致收尾逻辑不执行。
    onEnd(acc, {
      cancelled: Boolean(cancelled),
      error: error || null,
      japanese: fullJapanese || jaSentences.join(''),
      audioUrls: [...audioUrls],
      chunks: audioChunks.map((c) => ({ ...c })),
      emotion: (doneMeta && doneMeta.emotion) || null,
      affection: (doneMeta && doneMeta.affection) || null,
      ttsParams: (doneMeta && doneMeta.tts_params) || null,
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
      const res = await fetch('/api/chat/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
        signal: controller.signal,
      });
      if (!res.ok) {
        let msg = `请求失败 (${res.status})`;
        try {
          const data = await res.json();
          if (data && data.detail) msg = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail);
        } catch {
          /* 忽略非 JSON 错误体 */
        }
        finish(false, msg);
        return;
      }
      if (!res.body) {
        finish(false, '当前环境不支持流式响应');
        return;
      }
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      let eventName = 'message';
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop() || '';
        for (const line of lines) {
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
              onChunk(acc);
            }
          } else if (eventName === 'audio_chunk') {
            if (json.sentence) {
              jaSentences.push(json.sentence);
            }
            if (json.audio_url) {
              audioUrls.push(json.audio_url);
              audioChunks.push({
                url: json.audio_url,
                sentence: json.sentence || '',
                emotion: json.emotion || '',
              });
              if (typeof onAudio === 'function') {
                onAudio(json.audio_url, audioUrls.length - 1, json.sentence || '', { emotion: json.emotion || '' });
              }
            }
          } else if (eventName === 'audio_chunk_error') {
            console.warn('[streamChat] audio chunk error:', json.error, json.sentence);
          } else if (eventName === 'error') {
            finish(false, json.error || '服务流式处理出错');
            return;
          } else if (eventName === 'done') {
            doneMeta = json;
            if (json.japanese) {
              fullJapanese = json.japanese;
            }
            if (Array.isArray(json.chunks) && json.chunks.length > audioUrls.length) {
              for (const c of json.chunks) {
                if (c && c.audio_url && !audioUrls.includes(c.audio_url)) {
                  audioUrls.push(c.audio_url);
                  audioChunks.push({
                    url: c.audio_url,
                    sentence: c.sentence || '',
                    emotion: c.emotion || '',
                  });
                }
              }
            } else if (audioUrls.length === 0) {
              if (json.audio_url || json.total_audio_url) {
                audioUrls.push(json.audio_url || json.total_audio_url);
              }
            }
            if (!acc && json.chinese) {
              acc = json.chinese;
              onChunk(acc);
            }
            finish(false, null);
            return;
          }
        }
      }
      finish(false, null);
    } catch (err) {
      if (err && err.name === 'AbortError') {
        finish(true, null);
        return;
      }
      finish(false, err instanceof Error ? err.message : '网络异常');
    }
  })();

  return () => controller.abort();
}
