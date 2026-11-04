import { requestJson } from '../api.js';
import { normalizeLines } from '../session_data.js';
import { resolveTurnReplyIdentity } from './turn_reply_identity.js';

// Match the chat TTS job budget: model preparation, inference and queue/IO allowance.
const REPAIR_TIMEOUT_MS = 600000;

/** One explicit click sends one synthesis request; never replay a lost POST. */
export function createLineAudioRecovery({
  getSession,
  getVoiceProfiles,
  saveState,
  isSessionDeleting = () => false,
  request = requestJson,
  resolveIdentity = resolveTurnReplyIdentity,
  fetchHistory,
}) {
  const fetchHistoryFn = fetchHistory || ((sessionId, opts) => {
    const params = new URLSearchParams({ session_id: sessionId, limit: String(opts?.limit ?? 1) });
    if (Number.isInteger(opts?.beforeId) && opts.beforeId > 0) params.set('before_id', String(opts.beforeId));
    return request(`/api/chat/history?${params.toString()}`, { signal: opts?.signal });
  });

  return async ({ messageId, lineIndex = 0, signal }) => {
    const session = getSession();
    const message = session?.messages?.find(item => item.id === messageId && item.role === 'assistant');
    const isLegacy = message?.lines == null;
    let line = null, text = '';
    const originalLines = message?.lines;
    const originalContent = message?.content;
    const originalJapanese = message?.japanese;
    const originalAudioUrls = message?.audioUrls;
    const originalAudioChunks = message?.audioChunks;
    let previousAudio = null;

    if (isLegacy) {
      if (lineIndex !== 0) throw new Error('这句缺少日文台词，原记录未修改。');
      text = message?.japanese || '';
    } else {
      line = message.lines.find(item => item.index === lineIndex);
      if (!line) throw new Error('这句缺少日文台词，原记录未修改。');
      text = line.ja;
      previousAudio = line.audio;
    }

    const profileId = session?.settings?.voiceProfileId;
    const profile = getVoiceProfiles().find(item => item.id === profileId);
    if (!profile) throw new Error('请先在角色与会话中选择当前音色，再重新生成这句语音。');
    if (!text?.trim()) throw new Error('这句缺少日文台词，原记录未修改。');
    if (text.length > 2000) throw new Error('这句台词过长，暂时无法单句重新生成，原记录未修改。');
    const settings = { ...session.settings };
    const turnUserId = Number.isSafeInteger(message?.serverUserMessageId) && message.serverUserMessageId > 0
      ? message.serverUserMessageId : null;
    const turnConflict = Boolean(message?.turnConflict);
    if (turnConflict) throw new Error('回复身份存在冲突，无法恢复语音，原记录未修改。');
    let serverId = Number.isSafeInteger(message.serverMessageId) && message.serverMessageId > 0
      ? message.serverMessageId : null;
    const current = () => !signal?.aborted && getSession() === session && !isSessionDeleting(session.id)
      && session.messages.includes(message)
      && message.lines === originalLines
      && (isLegacy
        ? message.content === originalContent && message.japanese === originalJapanese
          && message.audioUrls === originalAudioUrls && message.audioChunks === originalAudioChunks
        : message.lines.includes(line) && line.ja === text && line.audio === previousAudio)
      && session.settings.voiceProfileId === profileId
      && (message.serverUserMessageId ?? null) === turnUserId && Boolean(message.turnConflict) === turnConflict
      && (Number.isSafeInteger(message.serverMessageId) && message.serverMessageId > 0 ? message.serverMessageId : null) === serverId;
    const checkCurrent = () => { if (!current()) throw new DOMException('操作已取消', 'AbortError'); };
    checkCurrent();
    if (serverId === null && turnUserId !== null) {
      const identity = await resolveIdentity({
        sessionId: session.id,
        userMessageId: turnUserId,
        signal,
        checkCurrent,
        request,
      });
      checkCurrent();
      if (!Number.isSafeInteger(identity?.messageId) || identity.messageId <= 0) {
        throw new Error('未获取到有效的回复编号，原记录未修改。');
      }
      message.serverMessageId = identity.messageId;
      serverId = identity.messageId;
      saveState();
    }
    const emotion = (isLegacy ? 'neutral' : line.emotion) || 'neutral';
    const body = {
      session_id: session.id, message_id: serverId, line_index: lineIndex, text,
      voice_profile_id: profileId, emotion, ai_adaptive_voice: settings.aiAdaptiveVoice !== false,
      options: { speed_factor: settings.ttsSpeed ?? 1, temperature: settings.ttsTemperature ?? 1,
        top_k: settings.ttsTopK ?? 15, top_p: settings.ttsTopP ?? 1 },
    };
    const validateAudio = (candidateAudio) => {
      const sampleLine = isLegacy
        ? { index: 0, zh: message.content || '', ja: text, emotion: 'neutral', act: {}, audio: candidateAudio }
        : { ...line, audio: candidateAudio };
      const normalized = normalizeLines([sampleLine])?.[0]?.audio;
      if (normalized?.status === 'ready' && normalized.chunks?.length === 1
          && normalized.chunks.every(chunk => chunk.line === lineIndex && chunk.sentence === text
            && typeof chunk.audio_url === 'string' && chunk.audio_url.startsWith('/audio/repair_'))
          && Number.isSafeInteger(normalized.generated_at) && normalized.voice_profile_id === profileId) {
        return normalized;
      }
      return null;
    };
    const applyAudio = (audioToApply, persisted, remoteUpdated = false) => {
      let canonicalLine;
      if (isLegacy) {
        canonicalLine = {
          index: 0,
          zh: message.content || '',
          ja: text,
          emotion: 'neutral',
          act: {},
          audio: audioToApply,
        };
        message.lines = [canonicalLine];
      } else {
        line.audio = audioToApply;
        canonicalLine = line;
      }
      message.audioChunks = message.lines.flatMap(item => (item.audio?.chunks || []).map(chunk => ({
        url: chunk.audio_url, sentence: chunk.sentence, emotion: item.emotion, line: item.index,
      }))).map((chunk, index) => ({ ...chunk, index }));
      message.audioUrls = message.audioChunks.map(chunk => chunk.url);
      const saved = saveState();
      const output = { audio: audioToApply, persisted, saved: saved !== false, line: canonicalLine };
      if (remoteUpdated) output.remoteUpdated = true;
      return output;
    };
    let result;
    try {
      result = await request('/api/voice/regenerate-line', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), signal,
      }, { timeoutMs: REPAIR_TIMEOUT_MS });
    } catch (error) {
      checkCurrent();
      if (error.code === 'REQUEST_TIMEOUT') throw new Error('语音生成结果尚未确认，原音频已保留。请先检查运行状态，再决定是否重试。');
      if (error.status === 409 && serverId !== null) {
        try {
          const history = await fetchHistoryFn(session.id, { beforeId: serverId + 1, limit: 1, signal });
          checkCurrent();
          if (history?.session_id === session.id) {
            const remoteMessage = (history?.messages || []).find(item =>
              item.id === serverId && item.role === 'assistant'
            );
            const remoteLine = remoteMessage?.lines?.find(item => item.index === lineIndex);
            if (remoteLine && remoteLine.ja === text && remoteLine.audio?.voice_profile_id === profileId) {
              const normalized = validateAudio(remoteLine.audio);
              if (normalized) {
                return applyAudio(normalized, true, true);
              }
            }
          }
        } catch (fetchError) {
          if (fetchError?.name === 'AbortError' || signal?.aborted) {
            throw (fetchError?.name === 'AbortError' ? fetchError : new DOMException('操作已取消', 'AbortError'));
          }
        }
      }
      throw error;
    }
    checkCurrent();
    const normalized = validateAudio(result?.audio);
    if (!normalized || result?.session_id !== session.id || result?.message_id !== serverId
        || result?.persisted !== (serverId !== null) || result?.line_index !== lineIndex
        || result?.voice_profile_id !== profileId) {
      throw new Error('服务返回的单句音频不完整，原音频记录已保留，请重新打开 LOG 检查。');
    }
    return applyAudio(normalized, result.persisted, false);
  };
}
