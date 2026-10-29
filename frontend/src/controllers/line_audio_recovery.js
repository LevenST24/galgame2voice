import { requestJson } from '../api.js';
import { normalizeLines } from '../session_data.js';

// Match the chat TTS job budget: model preparation, inference and queue/IO allowance.
const REPAIR_TIMEOUT_MS = 600000;

/** One explicit click sends one synthesis request; never replay a lost POST. */
export function createLineAudioRecovery({ getSession, getVoiceProfiles, saveState, isSessionDeleting = () => false, request = requestJson }) {
  return async ({ messageId, lineIndex, signal }) => {
    const session = getSession();
    const message = session?.messages?.find(item => item.id === messageId && item.role === 'assistant');
    const line = message?.lines?.find(item => item.index === lineIndex);
    const profileId = session?.settings?.voiceProfileId;
    const profile = getVoiceProfiles().find(item => item.id === profileId);
    if (!profile) throw new Error('请先在角色与会话中选择当前音色，再重新生成这句语音。');
    if (!line || !line.ja?.trim()) throw new Error('这句缺少日文台词，原记录未修改。');
    if (line.ja.length > 2000) throw new Error('这句台词过长，暂时无法单句重新生成，原记录未修改。');
    const text = line.ja, settings = { ...session.settings }, previousAudio = line.audio;
    const current = () => !signal?.aborted && getSession() === session && !isSessionDeleting(session.id)
      && session.messages.includes(message) && message.lines.includes(line)
      && line.ja === text && line.audio === previousAudio && session.settings.voiceProfileId === profileId;
    const checkCurrent = () => { if (!current()) throw new DOMException('操作已取消', 'AbortError'); };
    checkCurrent();
    const serverId = Number.isSafeInteger(message.serverMessageId) && message.serverMessageId > 0
      ? message.serverMessageId : null;
    const body = {
      session_id: session.id, message_id: serverId, line_index: lineIndex, text,
      voice_profile_id: profileId, emotion: line.emotion || 'neutral', ai_adaptive_voice: settings.aiAdaptiveVoice !== false,
      options: { speed_factor: settings.ttsSpeed ?? 1, temperature: settings.ttsTemperature ?? 1,
        top_k: settings.ttsTopK ?? 15, top_p: settings.ttsTopP ?? 1 },
    };
    let result;
    try {
      result = await request('/api/voice/regenerate-line', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), signal,
      }, { timeoutMs: REPAIR_TIMEOUT_MS });
    } catch (error) {
      checkCurrent();
      if (error.code === 'REQUEST_TIMEOUT') throw new Error('语音生成结果尚未确认，原音频已保留。请先检查运行状态，再决定是否重试。');
      throw error;
    }
    checkCurrent();
    const normalized = normalizeLines([{ ...line, audio: result?.audio }])?.[0]?.audio;
    if (result?.session_id !== session.id || result?.message_id !== serverId || result?.persisted !== (serverId !== null)
        || result?.line_index !== lineIndex || result?.voice_profile_id !== profileId
        || normalized?.status !== 'ready' || normalized.chunks.length !== 1
        || !normalized.chunks.every(chunk => chunk.line === lineIndex && chunk.sentence === text
          && typeof chunk.audio_url === 'string' && chunk.audio_url.startsWith('/audio/repair_'))
        || !Number.isSafeInteger(normalized.generated_at) || normalized.voice_profile_id !== profileId) {
      throw new Error('服务返回的单句音频不完整，原音频记录已保留，请重新打开 LOG 检查。');
    }
    line.audio = normalized;
    message.audioChunks = message.lines.flatMap(item => (item.audio?.chunks || []).map(chunk => ({
      url: chunk.audio_url, sentence: chunk.sentence, emotion: item.emotion, line: item.index,
    }))).map((chunk, index) => ({ ...chunk, index }));
    message.audioUrls = message.audioChunks.map(chunk => chunk.url);
    const saved = saveState();
    return { audio: normalized, persisted: result.persisted, saved: saved !== false };
  };
}
