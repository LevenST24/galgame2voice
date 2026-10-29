/** Shared home/LOG rule, including partial failed audio and seven-day expiry. */
export function canReplayLine(line, msg, voiceOn = true, now = Date.now()) {
  return Boolean(voiceOn && ['ready', 'failed'].includes(line?.audio?.status)
    && line.audio.chunks?.length && now - (line.audio.generated_at ?? msg?.ts ?? now) <= 7 * 86400000);
}
