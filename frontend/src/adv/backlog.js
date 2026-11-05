/**
 * backlog.js - ADV dedicated LOG (backlog) overlay component.
 *
 * Implements §4.2, §4.4 and SPEC Part 1:
 * - One entry per logical line from active session messages
 * - Assistant lines with speaker, primary text, and subtitle (honoring advSubtitleLanguage)
 * - Legacy assistant messages with whole text
 * - User messages with advPlayerName and recording playback
 * - Failed user messages with inline reason, retry/settings and recording playback
 * - Per-line voice replay with exclusive playback and 7-day expiration checks
 * - Paging via loadOlderSessionHistory with scroll position preservation
 * - Live turn updates via controller subscriptions (turnstart, line, line_audio, done, error, cancelled); while the stage presents a turn, only its shown lines are listed
 * - Accessibility: role="dialog", aria-modal="true", accessible name, focus trap and focus return
 * - Keyboard & gesture controls: Esc / close button / wheel-down at bottom to close
 * - Text rendered strictly with textContent
 */

const SEVEN_DAYS_MS = 7 * 24 * 60 * 60 * 1000;
// A double-click's second hit lands on the button that just became「取消生成」.
const REGENERATE_CANCEL_GRACE_MS = 500;

export function resolveLogTexts(lineOrMsg, settings = {}) {
  const primaryLang = settings.advPrimaryLanguage || 'zh';
  const subtitleLang = settings.advSubtitleLanguage !== undefined ? settings.advSubtitleLanguage : 'ja';

  let primary = (lineOrMsg[primaryLang] ?? '').trim();
  const fallbackLang = primaryLang === 'zh' ? 'ja' : 'zh';
  if (!primary) {
    primary = (lineOrMsg[fallbackLang] ?? '').trim();
  }

  let subtitle = '';
  if (subtitleLang !== 'none' && subtitleLang !== primaryLang) {
    subtitle = (lineOrMsg[subtitleLang] ?? '').trim();
    if (subtitle === primary) {
      subtitle = '';
    }
  }

  return { primary, subtitle };
}

export function createBacklog(container, deps = {}) {
  const doc = container?.ownerDocument || globalThis.document;
  const disposers = [];

  const overlayEl = doc.createElement('div');
  overlayEl.className = 'adv-log-overlay';
  overlayEl.setAttribute('role', 'dialog');
  overlayEl.setAttribute('aria-modal', 'true');
  overlayEl.setAttribute('aria-label', 'LOG 历史记录');

  // Header
  const headerEl = doc.createElement('div');
  headerEl.className = 'adv-log-header';

  const titleEl = doc.createElement('span');
  titleEl.className = 'adv-log-title';
  titleEl.textContent = '对话记录';

  const closeBtn = doc.createElement('button');
  closeBtn.type = 'button';
  closeBtn.className = 'adv-log-close-btn';
  closeBtn.textContent = '关闭';
  closeBtn.setAttribute('aria-label', '关闭 LOG');

  headerEl.appendChild(titleEl);
  headerEl.appendChild(closeBtn);

  // List
  const listEl = doc.createElement('div');
  listEl.className = 'adv-log-list';

  // More button for older history paging
  const moreBtn = doc.createElement('button');
  moreBtn.type = 'button';
  moreBtn.className = 'adv-log-more-btn';
  moreBtn.textContent = '加载更早的记录';
  moreBtn.setAttribute('aria-label', '加载更早的记录');

  overlayEl.appendChild(headerEl);
  overlayEl.appendChild(listEl);

  let loadingOlder = false;
  let destroyed = false;
  let replaySequence = 0;
  let activeReplayButton = null;
  let activeRegeneration = null;
  const regenerationFeedback = new Map();
  const regenerationKey = (messageId, lineIndex) => `${messageId}:${lineIndex}`;
  const failedAudio = new Set();
  const loadNotes = new Map();
  const audioKey = line => JSON.stringify(line?.audio?.chunks || []);

  function extractLegacyChunks(msg) {
    if (Array.isArray(msg?.audioChunks) && msg.audioChunks.length > 0) {
      return msg.audioChunks.map((c, i) => {
        const url = typeof c === 'string' ? c : (c?.url || c?.audio_url || '');
        const sentence = typeof c === 'object' ? (c?.sentence || msg.japanese || '') : (msg.japanese || '');
        return { index: i, url, audio_url: url, sentence };
      }).filter(c => Boolean(c.url));
    }
    if (Array.isArray(msg?.audioUrls) && msg.audioUrls.length > 0) {
      return msg.audioUrls.map((url, i) => ({
        index: i,
        url: typeof url === 'string' ? url : (url?.url || url?.audio_url || ''),
        audio_url: typeof url === 'string' ? url : (url?.audio_url || url?.url || ''),
        sentence: i === 0 ? (msg.japanese || '') : '',
      })).filter(c => Boolean(c.url));
    }
    if (msg?.audio_url) {
      return [{
        index: 0,
        url: msg.audio_url,
        audio_url: msg.audio_url,
        sentence: msg.japanese || '',
      }];
    }
    if (Array.isArray(msg?.audio?.chunks) && msg.audio.chunks.length > 0) {
      return msg.audio.chunks.map((c, i) => {
        const url = typeof c === 'string' ? c : (c?.audio_url || c?.url || '');
        const sentence = typeof c === 'object' ? (c?.sentence || msg.japanese || '') : (msg.japanese || '');
        return { index: i, url, audio_url: url, sentence };
      }).filter(c => Boolean(c.url));
    }
    return [];
  }
  const loadRetryText = {
    timeout: '音频加载超时，请稍后在 LOG 里再点「语音」重试。',
    network: '音频加载失败，请稍后在 LOG 里再点「语音」重试。',
  };

  function stopReplay({ feedback } = {}) {
    ++replaySequence;
    if (activeRegeneration) {
      const pending = activeRegeneration;
      activeRegeneration = null;
      if (feedback) regenerationFeedback.set(pending.key, feedback);
      else regenerationFeedback.delete(pending.key);
      pending.controller.abort();
      for (const entry of listEl.querySelectorAll('.adv-log-entry')) entry._refreshAudioState?.();
    }
    activeReplayButton?.classList.remove('is-playing');
    activeReplayButton = null;
    deps.stopOtherVoice?.();
    deps.audio?.stopLine?.({ fadeMs: 0 });
  }

  function getNow() {
    return typeof deps.wallNow === 'function' ? deps.wallNow() : Date.now();
  }

  function isVoiceEnabled() {
    return typeof deps.voiceEnabled === 'function' ? Boolean(deps.voiceEnabled()) : true;
  }

  function isHiddenLine(msg, line) {
    const hidden = deps.getHiddenFrom?.();
    return Boolean(hidden)
      && (hidden.messageIds || []).some(id => String(id) === String(msg?.id))
      && Number(line?.index) > Number(hidden.after);
  }

  function updateReplayButtonState(btn, line, msg, statusSpan) {
    const chunks = line?.audio?.chunks || [];
    const status = line?.audio?.status;
    const hasPlayableChunks = Array.isArray(chunks) && chunks.length > 0 && (status === 'ready' || status === 'failed');
    const voiceOn = isVoiceEnabled();
    const now = getNow();
    const msgTs = line?.audio?.generated_at ?? msg?.ts ?? now;
    const isExpired = (now - msgTs) > SEVEN_DAYS_MS;

    const unavailable = line ? failedAudio.has(audioKey(line)) : false;
    const retryText = unavailable ? '' : (line ? (loadNotes.get(audioKey(line)) || '') : '');
    statusSpan.hidden = !unavailable && !retryText;
    statusSpan.textContent = unavailable ? '语音已过期或不可用' : retryText;
    if (unavailable) {
      btn.disabled = true;
      btn.title = '语音已过期或不可用';
      btn.setAttribute('aria-disabled', 'true');
      btn.setAttribute('aria-label', btn.title);
    } else if (!hasPlayableChunks) {
      btn.disabled = true;
      btn.title = '无语音';
      btn.setAttribute('aria-disabled', 'true');
      btn.setAttribute('aria-label', '无语音');
    } else if (!voiceOn) {
      btn.disabled = true;
      btn.title = '语音已关闭';
      btn.setAttribute('aria-disabled', 'true');
      btn.setAttribute('aria-label', '语音已关闭');
    } else if (isExpired) {
      btn.disabled = true;
      btn.title = '语音已过期（超过7天）';
      btn.setAttribute('aria-disabled', 'true');
      btn.setAttribute('aria-label', '语音已过期（超过7天）');
    } else {
      btn.disabled = false;
      btn.title = '播放语音';
      btn.removeAttribute('aria-disabled');
      btn.setAttribute('aria-label', '播放语音');
    }
  }

  function bindRegenerateControl({
    msg,
    lineIndex,
    getText,
    replayBtn,
    regenerateBtn,
    refreshAudioState,
    onSuccess,
  }) {
    const lineKey = regenerationKey(msg.id, lineIndex);

    function refreshRepairButton() {
      const generating = activeRegeneration?.key === lineKey;
      const isExplicitEmpty = Array.isArray(msg.lines) && msg.lines.length === 0;
      const needsRepair = !isExplicitEmpty && replayBtn.disabled && isVoiceEnabled();
      regenerateBtn.hidden = !deps.regenerateLineAudio || (!generating && !needsRepair);
      const voice = deps.getRecoveryVoice?.();
      regenerateBtn.textContent = generating ? '取消生成' : voice ? '用当前音色重新生成' : '选择音色';
      regenerateBtn.title = generating ? '取消本次语音生成' : voice ? `使用「${voice.name}」重新生成这一句` : '先为当前会话选择音色';
      regenerateBtn.setAttribute('aria-label', regenerateBtn.title);
      const text = getText();
      regenerateBtn.disabled = !generating && (isExplicitEmpty || Boolean(deps.controller?.busy)
        || !(deps.getSession?.()?.messages || []).includes(msg)
        || !text?.trim() || !/[\p{L}\p{N}]/u.test(text) || text.length > 2000);
      if (regenerateBtn.disabled && !generating) {
        regenerateBtn.title = deps.controller?.busy ? '请等待当前回复完成后再重新生成'
          : !text?.trim() || !/[\p{L}\p{N}]/u.test(text) ? '这句缺少可配音的日文台词'
          : text.length > 2000 ? '这句台词过长，暂时无法单句重新生成' : '请等待历史记录保存完成';
        regenerateBtn.setAttribute('aria-label', regenerateBtn.title);
      }
      if (generating) regenerateBtn.setAttribute('aria-busy', 'true');
      else regenerateBtn.removeAttribute('aria-busy');
    }

    regenerateBtn.onclick = async e => {
      e.stopPropagation();
      if (destroyed) return;
      if (activeRegeneration?.key === lineKey) {
        if (getNow() - activeRegeneration.startedAt < REGENERATE_CANCEL_GRACE_MS) return;
        stopReplay({ feedback: '已取消本次生成，原音频已保留。' });
        return;
      }
      refreshRepairButton();
      if (regenerateBtn.disabled || regenerateBtn.hidden) return;
      if (!deps.getRecoveryVoice?.()) { deps.onChooseVoice?.(); return; }
      stopReplay();
      const sequence = replaySequence, controller = new AbortController();
      const pending = { key: lineKey, controller, startedAt: getNow() };
      activeRegeneration = pending;
      regenerationFeedback.set(lineKey, '正在生成这句语音…');
      refreshAudioState();
      let feedback = '';
      try {
        const result = await deps.regenerateLineAudio({ messageId: msg.id, lineIndex, signal: controller.signal });
        if (destroyed || controller.signal.aborted || sequence !== replaySequence) return;
        onSuccess(result);
        feedback = result.remoteUpdated
          ? (result.saved === false
            ? '这一句已在另一网页修好；本地缓存未更新'
            : '这一句已在另一网页修好，已更新')
          : result.persisted === true && result.saved === false
          ? '语音已生成并保存到服务端；本地缓存未更新'
          : result.saved === false ? '语音已生成，但本地保存失败，请先备份聊天。'
          : result.persisted === false ? '语音已生成，修复记录保存在本浏览器；点语音播放。' : '语音已生成，点语音播放。';
      } catch (error) {
        if (destroyed || controller.signal.aborted || sequence !== replaySequence || error.name === 'AbortError') return;
        feedback = /[\u3400-\u9fff]/.test(error.message || '') ? error.message : '这句语音未能生成，原音频已保留，请检查语音服务后再试。';
      } finally {
        if (activeRegeneration === pending) {
          activeRegeneration = null;
          if (feedback) regenerationFeedback.set(lineKey, feedback);
          else regenerationFeedback.delete(lineKey);
          for (const entry of listEl.querySelectorAll('.adv-log-entry')) entry._refreshAudioState?.();
        }
      }
    };

    return { lineKey, refreshRepairButton };
  }

  function renderAssistantLineEntry(line, msg, settings) {
    const entryEl = doc.createElement('div');
    entryEl.className = 'adv-log-entry is-assistant';
    entryEl.dataset.lineIndex = String(line.index);
    entryEl.dataset.msgId = String(msg.id || '');

    const header = doc.createElement('div');
    header.className = 'adv-log-entry-header';

    const speakerEl = doc.createElement('span');
    speakerEl.className = 'adv-log-speaker';
    speakerEl.textContent = deps.getCharacter?.()?.name || '角色';

    const rightControls = doc.createElement('div');
    rightControls.className = 'adv-log-entry-controls';

    const statusSpan = doc.createElement('span');
    statusSpan.className = 'adv-log-audio-status';
    statusSpan.hidden = true;

    const replayBtn = doc.createElement('button');
    replayBtn.type = 'button';
    replayBtn.className = 'adv-log-replay-btn';
    replayBtn.textContent = '语音';

    const regenerateBtn = doc.createElement('button');
    regenerateBtn.type = 'button';
    regenerateBtn.className = 'adv-log-regenerate-btn';
    const hasVoice = !msg.noVoice && !line.noVoice;

    function refreshAudioState() {
      if (!hasVoice) return;
      updateReplayButtonState(replayBtn, line, msg, statusSpan);
      refreshRepairButton();
      const note = regenerationFeedback.get(lineKey);
      if (note) {
        statusSpan.hidden = false;
        statusSpan.textContent = note;
      }
    }

    const { lineKey, refreshRepairButton } = bindRegenerateControl({
      msg,
      lineIndex: line.index,
      getText: () => line.ja,
      replayBtn,
      regenerateBtn,
      refreshAudioState,
      onSuccess: result => {
        line = { ...line, audio: structuredClone(result.audio) };
      },
    });

    refreshAudioState();

    replayBtn.onclick = async (e) => {
      e.stopPropagation();
      if (destroyed) return;
      const key = audioKey(line);
      const hadRetryNote = loadNotes.delete(key);
      updateReplayButtonState(replayBtn, line, msg, statusSpan);
      if (hadRetryNote) {
        for (const entry of listEl.querySelectorAll('.adv-log-entry')) entry._refreshAudioState?.();
      }
      if (replayBtn.disabled) return;
      stopReplay();
      const thisReplay = replaySequence;
      activeReplayButton = replayBtn;

      replayBtn.classList.add('is-playing');
      let result;
      try {
        if (deps.audio?.playLine) {
          result = await deps.audio.playLine(line.index, line.audio.chunks, { msgId: msg.id });
        } else if (deps.voice?.play) {
          result = await deps.voice.play(line.index, line.audio.chunks);
        } else {
          result = 'ended';
        }
      } catch {
        result = 'failed';
      }

      if (!destroyed && replaySequence === thisReplay) {
        replayBtn.classList.remove('is-playing');
        activeReplayButton = null;
        if (result === 'failed') {
          failedAudio.add(key);
          loadNotes.delete(key);
          regenerationFeedback.delete(lineKey);
        } else if (loadRetryText[result]) {
          loadNotes.set(key, loadRetryText[result]);
          regenerationFeedback.delete(lineKey);
        }
        if (result === 'failed' || loadRetryText[result]) {
          updateReplayButtonState(replayBtn, line, msg, statusSpan);
          for (const entry of listEl.querySelectorAll('.adv-log-entry')) entry._refreshAudioState?.();
        }
      }
    };

    header.appendChild(speakerEl);
    if (hasVoice) {
      rightControls.appendChild(replayBtn);
      rightControls.appendChild(regenerateBtn);
      header.appendChild(rightControls);
    }

    const textWrap = doc.createElement('div');
    textWrap.className = 'adv-log-text-wrap';

    const { primary, subtitle } = resolveLogTexts(line, settings);

    const primaryEl = doc.createElement('div');
    primaryEl.className = 'adv-log-primary';
    primaryEl.textContent = primary;
    textWrap.appendChild(primaryEl);

    if (subtitle && settings.advSubtitleLanguage !== 'none') {
      const subtitleEl = doc.createElement('div');
      subtitleEl.className = 'adv-log-subtitle';
      subtitleEl.textContent = subtitle;
      textWrap.appendChild(subtitleEl);
    }

    textWrap.appendChild(statusSpan);

    entryEl.appendChild(header);
    entryEl.appendChild(textWrap);

    entryEl._updateLineAudio = (status, chunks) => {
      // The controller owns stored history; the view keeps its own audio snapshot.
      line = { ...line, audio: { status, chunks: chunks || [] } };
      refreshAudioState();
    };
    entryEl._refreshAudioState = refreshAudioState;

    return entryEl;
  }

  function renderAssistantLegacyEntry(msg, settings) {
    const entryEl = doc.createElement('div');
    entryEl.className = 'adv-log-entry is-assistant is-legacy';
    entryEl.dataset.msgId = String(msg.id || '');

    const header = doc.createElement('div');
    header.className = 'adv-log-entry-header';

    const speakerEl = doc.createElement('span');
    speakerEl.className = 'adv-log-speaker';
    speakerEl.textContent = deps.getCharacter?.()?.name || '角色';

    const rightControls = doc.createElement('div');
    rightControls.className = 'adv-log-entry-controls';

    const statusSpan = doc.createElement('span');
    statusSpan.className = 'adv-log-audio-status';
    statusSpan.hidden = true;

    const replayBtn = doc.createElement('button');
    replayBtn.type = 'button';
    replayBtn.className = 'adv-log-replay-btn';
    replayBtn.textContent = '语音';

    const regenerateBtn = doc.createElement('button');
    regenerateBtn.type = 'button';
    regenerateBtn.className = 'adv-log-regenerate-btn';

    const hasVoice = !msg.noVoice;

    let repairedLine = Array.isArray(msg.lines) && msg.lines.length > 0 ? msg.lines[0] : null;

    const { lineKey, refreshRepairButton } = bindRegenerateControl({
      msg,
      lineIndex: 0,
      getText: () => msg.japanese,
      replayBtn,
      regenerateBtn,
      refreshAudioState,
      onSuccess: result => {
        repairedLine = result.line || msg.lines?.[0] || {
          index: 0,
          zh: msg.content || '',
          ja: msg.japanese || '',
          emotion: 'neutral',
          act: {},
          audio: structuredClone(result.audio),
        };
        entryEl.dataset.lineIndex = '0';
      },
    });

    function getTargetLine() {
      if (repairedLine) return repairedLine;
      const chunks = extractLegacyChunks(msg);
      if (chunks.length > 0) {
        return {
          index: 0,
          zh: msg.content || '',
          ja: msg.japanese || '',
          emotion: 'neutral',
          act: {},
          audio: { status: 'ready', chunks },
        };
      }
      return null;
    }

    function refreshAudioState() {
      if (!hasVoice) return;
      if (!repairedLine && Array.isArray(msg.lines) && msg.lines.length > 0) {
        repairedLine = msg.lines[0];
        entryEl.dataset.lineIndex = '0';
      }
      const targetLine = getTargetLine();
      updateReplayButtonState(replayBtn, targetLine, msg, statusSpan);
      refreshRepairButton();
      const note = regenerationFeedback.get(lineKey);
      if (note) {
        statusSpan.hidden = false;
        statusSpan.textContent = note;
      }
    }

    refreshAudioState();

    replayBtn.onclick = async event => {
      event.stopPropagation();
      if (destroyed || replayBtn.disabled) return;
      const targetLine = getTargetLine();
      if (!targetLine) return;

      const key = audioKey(targetLine);
      const hadRetryNote = loadNotes.delete(key);
      updateReplayButtonState(replayBtn, targetLine, msg, statusSpan);
      if (hadRetryNote) {
        for (const entry of listEl.querySelectorAll('.adv-log-entry')) entry._refreshAudioState?.();
      }
      if (replayBtn.disabled) return;

      stopReplay();
      const thisReplay = replaySequence;
      activeReplayButton = replayBtn;
      replayBtn.classList.add('is-playing');

      let result;
      try {
        if (deps.audio?.playLine) {
          result = await deps.audio.playLine(targetLine.index, targetLine.audio.chunks, { msgId: msg.id });
        } else if (deps.voice?.play) {
          result = await deps.voice.play(targetLine.index, targetLine.audio.chunks);
        } else if (deps.playLegacyVoice && !repairedLine) {
          result = await new Promise(resolve => {
            deps.playLegacyVoice(msg, {
              setPlaying: playing => {
                if (!destroyed && thisReplay === replaySequence) {
                  replayBtn.classList.toggle('is-playing', Boolean(playing));
                  if (!playing) resolve('ended');
                }
              },
              setProgress() {},
              setLoading() {},
            });
          });
        } else {
          result = 'ended';
        }
      } catch {
        result = 'failed';
      }

      if (!destroyed && replaySequence === thisReplay) {
        replayBtn.classList.remove('is-playing');
        activeReplayButton = null;
        if (result === 'failed') {
          failedAudio.add(key);
          loadNotes.delete(key);
          regenerationFeedback.delete(lineKey);
        } else if (loadRetryText[result]) {
          loadNotes.set(key, loadRetryText[result]);
          regenerationFeedback.delete(lineKey);
        }
        if (result === 'failed' || loadRetryText[result]) {
          updateReplayButtonState(replayBtn, targetLine, msg, statusSpan);
          for (const entry of listEl.querySelectorAll('.adv-log-entry')) entry._refreshAudioState?.();
        }
      }
    };

    header.appendChild(speakerEl);
    if (hasVoice) {
      rightControls.appendChild(replayBtn);
      rightControls.appendChild(regenerateBtn);
      header.appendChild(rightControls);
    }

    const textWrap = doc.createElement('div');
    textWrap.className = 'adv-log-text-wrap';

    const legacyLine = { zh: msg.content || '', ja: msg.japanese || '' };
    const { primary, subtitle } = resolveLogTexts(legacyLine, settings);

    const primaryEl = doc.createElement('div');
    primaryEl.className = 'adv-log-primary';
    primaryEl.textContent = primary;
    textWrap.appendChild(primaryEl);

    if (subtitle && settings.advSubtitleLanguage !== 'none') {
      const subtitleEl = doc.createElement('div');
      subtitleEl.className = 'adv-log-subtitle';
      subtitleEl.textContent = subtitle;
      textWrap.appendChild(subtitleEl);
    }

    textWrap.appendChild(statusSpan);

    entryEl.appendChild(header);
    entryEl.appendChild(textWrap);

    entryEl._updateLineAudio = (status, chunks) => {
      if (repairedLine) {
        repairedLine = { ...repairedLine, audio: { status, chunks: chunks || [] } };
      }
      refreshAudioState();
    };
    entryEl._refreshAudioState = refreshAudioState;

    return entryEl;
  }

  function renderUserEntry(msg, settings) {
    const entryEl = doc.createElement('div');
    entryEl.className = 'adv-log-entry is-user';
    entryEl.dataset.msgId = String(msg.id || '');

    const header = doc.createElement('div');
    header.className = 'adv-log-entry-header';

    const speakerEl = doc.createElement('span');
    speakerEl.className = 'adv-log-speaker';
    speakerEl.textContent = settings.advPlayerName || '我';

    header.appendChild(speakerEl);

    const hasRecording = Boolean(msg.recording || msg.dur || msg.voiceKey || msg.audioUrl);
    if (hasRecording) {
      const recBtn = doc.createElement('button');
      recBtn.type = 'button';
      recBtn.className = 'adv-log-user-recording-btn';
      recBtn.textContent = '播放录音';
      recBtn.setAttribute('aria-label', '播放录音');
      recBtn.onclick = (e) => {
        e.stopPropagation();
        if (destroyed) return;
        stopReplay();
        const sequence = replaySequence;
        activeReplayButton = recBtn;
        deps.playUserVoice?.(msg, {
          setPlaying: (p) => {
            if (!destroyed && sequence === replaySequence) recBtn.classList.toggle('is-playing', Boolean(p));
          },
          setProgress: () => {},
        });
      };
      header.appendChild(recBtn);
    }

    const textWrap = doc.createElement('div');
    textWrap.className = 'adv-log-text-wrap';

    const contentEl = doc.createElement('div');
    contentEl.className = 'adv-log-primary';
    contentEl.textContent = msg.content || '';
    textWrap.appendChild(contentEl);

    if (msg.failed) {
      const failEl = doc.createElement('div');
      failEl.className = 'adv-log-failure';
      const failReason = typeof msg.failed === 'string'
        ? msg.failed
        : (msg.failed.error || '发送失败');
      failEl.textContent = `未收到回复：${failReason}`;
      textWrap.appendChild(failEl);
      const addAction = (label, run) => {
        const button = doc.createElement('button'); button.type = 'button';
        button.className = 'adv-error-btn'; button.textContent = label;
        button.onclick = event => { event.stopPropagation(); run(); };
        textWrap.appendChild(button);
      };
      if (deps.controller?.canRetry?.(msg)) addAction('重新发送', () => {
        deps.onClose?.(); deps.controller.retry(msg);
      });
      if (deps.onSettings) addAction('去设置', () => {
        deps.onClose?.(); deps.onSettings({ message: failReason, code: msg.failed.code });
      });
    }

    entryEl.appendChild(header);
    entryEl.appendChild(textWrap);
    return entryEl;
  }

  function buildEntriesForMessage(msg, settings) {
    if (msg.role === 'user') {
      return [renderUserEntry(msg, settings)];
    }
    if (msg.role === 'assistant') {
      if (Array.isArray(msg.lines) && msg.lines.length > 0) {
        return msg.lines
          .filter((line) => !isHiddenLine(msg, line))
          .map((line) => renderAssistantLineEntry(line, msg, settings));
      }
      return msg.content || msg.japanese ? [renderAssistantLegacyEntry(msg, settings)] : [];
    }
    return [];
  }

  function renderAll() {
    if (destroyed) return;
    const hadFocus = listEl.contains(doc.activeElement);
    listEl.replaceChildren();
    const session = deps.getSession?.();
    const settings = deps.getSettings?.() || {};

    if (session?.hasMoreHistory) {
      moreBtn.hidden = false;
      moreBtn.disabled = Boolean(session.loadingOlder || loadingOlder || deps.controller?.busy);
      moreBtn.textContent = session.loadingOlder || loadingOlder ? '正在加载更早的记录…'
        : deps.controller?.busy ? '回复完成后可查看更早的记录' : '加载更早的记录';
      listEl.appendChild(moreBtn);
    } else {
      moreBtn.hidden = true;
    }

    const messages = [...session?.messages || []];
    const live = deps.getLiveMessage?.();
    if (live && !messages.some(msg => msg.id === live.id)) messages.push(live);
    for (const msg of messages) {
      const entries = buildEntriesForMessage(msg, settings);
      for (const entry of entries) {
        listEl.appendChild(entry);
      }
    }
    if (hadFocus) closeBtn.focus();
  }

  // Paging action
  moreBtn.onclick = async () => {
    const session = deps.getSession?.();
    if (destroyed || !session || session.loadingOlder || loadingOlder || deps.controller?.busy) return;
    loadingOlder = true;
    moreBtn.disabled = true;
    moreBtn.textContent = '正在加载更早的记录…';

    const prevScrollHeight = listEl.scrollHeight;
    const prevScrollTop = listEl.scrollTop;

    try {
      await deps.loadOlderHistory?.(session);
    } catch (err) {
      console.warn('Failed to load older history:', err);
    } finally {
      loadingOlder = false;
      moreBtn.disabled = false;
      moreBtn.textContent = '加载更早的记录';
    }

    if (destroyed || deps.getSession?.() !== session) return;
    renderAll();
    const delta = listEl.scrollHeight - prevScrollHeight;
    listEl.scrollTop = Math.max(0, prevScrollTop + delta);
  };

  // Close handlers
  function handleClose() {
    stopReplay();
    deps.onClose?.();
  }

  closeBtn.onclick = handleClose;

  // Esc and focus trap
  function handleKeydown(e) {
    // Native dialog controls keep their defaults; stage shortcuts never own these events.
    e.stopPropagation();
    if (e.key === 'Escape') {
      e.preventDefault();
      e.stopPropagation();
      handleClose();
      return;
    }
    if (e.key === 'Tab') {
      const focusable = [
        ...overlayEl.querySelectorAll('button, input, select, textarea, [tabindex]'),
      ].filter((node) => !node.disabled && !node.hidden && node.isConnected && node.tabIndex >= 0);
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (!overlayEl.contains(doc.activeElement)) {
        e.preventDefault();
        first.focus();
      } else if (e.shiftKey && doc.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && doc.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    }
  }

  overlayEl.addEventListener('keydown', handleKeydown);
  disposers.push(() => overlayEl.removeEventListener('keydown', handleKeydown));
  const stopPointer = e => e.stopPropagation();
  overlayEl.addEventListener('pointerdown', stopPointer);
  disposers.push(() => overlayEl.removeEventListener('pointerdown', stopPointer));

  // Wheel down at bottom to close
  function handleWheel(e) {
    if (e.deltaY > 0) {
      const atBottom = listEl.scrollHeight - listEl.scrollTop - listEl.clientHeight <= 2;
      if (atBottom) {
        e.preventDefault();
        e.stopPropagation();
        handleClose();
      }
    }
  }

  listEl.addEventListener('wheel', handleWheel, { passive: false });
  disposers.push(() => listEl.removeEventListener('wheel', handleWheel));

  // Controller live turn subscriptions
  const controller = deps.controller;
  if (controller?.on) {
    disposers.push(
      controller.on('turnstart', (event) => {
        const session = deps.getSession?.();
        if (event.originId && session && event.originId !== session.id) return;
        renderAll();
        listEl.scrollTop = listEl.scrollHeight;
      })
    );

    disposers.push(
      controller.on('line', (event) => {
        const session = deps.getSession?.();
        const msg = session?.messages?.find(m => m.id === event.messageId) || deps.getLiveMessage?.();
        if (!msg || msg.id !== event.messageId) return;
        if (isHiddenLine(msg, event.line)) return;
        const settings = deps.getSettings?.() || {};
        const atBottom = listEl.scrollHeight - listEl.scrollTop - listEl.clientHeight <= 20;
        const existing = [...listEl.querySelectorAll('.adv-log-entry')].find(el =>
          el.dataset.msgId === String(event.messageId) &&
          (el.dataset.lineIndex === String(event.line.index) || el.classList.contains('is-legacy')));
        const entryEl = renderAssistantLineEntry(event.line, msg, settings);
        if (existing) existing.replaceWith(entryEl);
        else listEl.appendChild(entryEl);
        if (atBottom) listEl.scrollTop = listEl.scrollHeight;
      })
    );

    disposers.push(
      controller.on('line_audio', (event) => {
        const lineIdxStr = String(event.line);
        const match = [...listEl.querySelectorAll('.adv-log-entry.is-assistant')].find(
          (el) => el.dataset.msgId === String(event.messageId) && el.dataset.lineIndex === lineIdxStr
        );
        if (match?._updateLineAudio) {
          match._updateLineAudio(event.status, event.chunks);
        }
      })
    );

    for (const kind of ['done', 'error', 'cancelled']) {
      disposers.push(
        controller.on(kind, event => {
          if (event.originId && event.originId !== deps.getSession?.()?.id) return;
          // Reconcile open entries to reflect stored message snapshot
          const prevScrollHeight = listEl.scrollHeight;
          const prevScrollTop = listEl.scrollTop;
          const wasAtBottom = prevScrollHeight - prevScrollTop - listEl.clientHeight <= 20;

          renderAll();

          if (wasAtBottom) {
            listEl.scrollTop = listEl.scrollHeight;
          } else {
            const delta = listEl.scrollHeight - prevScrollHeight;
            listEl.scrollTop = Math.max(0, prevScrollTop + delta);
          }
        })
      );
    }
  }

  // Initial render
  renderAll();

  // Mount
  if (container) {
    container.appendChild(overlayEl);
  }

  // Open at the newest entry
  listEl.scrollTop = listEl.scrollHeight;

  // Initial focus into dialog
  closeBtn.focus?.();

  return {
    element: overlayEl,
    listElement: listEl,
    closeButton: closeBtn,
    moreButton: moreBtn,
    renderAll,
    stopReplay,
    destroy() {
      if (destroyed) return;
      destroyed = true;
      stopReplay();
      for (const off of disposers.splice(0)) off();
      overlayEl.remove();
    },
  };
}
