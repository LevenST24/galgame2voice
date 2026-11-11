// 语音能力：麦克风录音（MediaRecorder）+ 语音转写（SpeechRecognition）+ 音频内存缓存（BoundedAudioStore）
let recog = null;
let listening = false;
let listenGen = 0;
let recorder = null;
let recStream = null;
let recChunks = [];
let recStartTs = 0;

/** 语音转写是否可用 */
export function voiceSupported() {
  return Boolean(window.SpeechRecognition || window.webkitSpeechRecognition);
}

/** 录音是否可用 */
export function recorderSupported() {
  return Boolean(navigator.mediaDevices && navigator.mediaDevices.getUserMedia && window.MediaRecorder);
}

/** 当前是否正在聆听 */
export function isListening() {
  return listening;
}

/**
 * 开始转写 + 并行录音（转写必需；录音不可用时仅转写）
 * @returns {{ok: boolean, reason?: string}}
 */
export function startListening({ onInterim, onText, onRecorded, onError }) {
  if (listening) return { ok: true };
  const hasRecorder = recorderSupported();
  const hasRecognition = voiceSupported();

  // 发出去的是转写文字，录音只是附件；没有转写时录了也发不出去。
  if (!hasRecognition) {
    return {
      ok: false,
      reason: '当前浏览器不支持语音输入，建议使用 Chrome / Edge',
    };
  }

  const myGen = ++listenGen;
  let recogOk = false;
  let recorderActive = hasRecorder;

  // 1) 录音（可选能力，失败不阻断转写）
  if (hasRecorder) {
    navigator.mediaDevices
      .getUserMedia({ audio: true })
      .then((stream) => {
        if (!listening || myGen !== listenGen) {
          stream.getTracks().forEach((t) => t.stop());
          return;
        }
        if (recStream && recStream !== stream) {
          recStream.getTracks().forEach((t) => t.stop());
        }
        recStream = stream;
        const myChunks = [];
        recChunks = myChunks;
        const myStartTs = Date.now();
        recStartTs = myStartTs;
        const mime = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4'].find(
          (m) => MediaRecorder.isTypeSupported && MediaRecorder.isTypeSupported(m)
        );
        const myRecorder = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
        recorder = myRecorder;
        myRecorder.ondataavailable = (e) => {
          if (e.data && e.data.size) myChunks.push(e.data);
        };
        myRecorder.onstop = () => {
          const dur = Math.max(1, Math.round((Date.now() - myStartTs) / 1000));
          const mimeType = myRecorder?.mimeType || mime || 'audio/webm';
          const blob = myChunks.length ? new Blob(myChunks, { type: mimeType }) : null;
          try {
            stream.getTracks().forEach((t) => t.stop());
          } catch (_) {}
          if (recorder === myRecorder) {
            cleanupRecorder();
          }
          if (blob && typeof onRecorded === 'function') onRecorded(blob, dur);
        };
        myRecorder.start();
      })
      .catch(() => {
        recorderActive = false;
        if (myGen !== listenGen) return;
        if (!recogOk) listening = false;
        if (!recogOk && typeof onError === 'function') onError('无法访问麦克风，请在浏览器中允许麦克风权限');
      });
  }

  // 2) 转写（可选能力）
  if (hasRecognition) {
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    const myRecog = new SR();
    recog = myRecog;
    myRecog.lang = 'zh-CN';
    myRecog.interimResults = true;
    myRecog.continuous = false;
    myRecog.maxAlternatives = 1;
    listening = true;
    myRecog.onresult = (e) => {
      if (myGen !== listenGen) return;
      let interim = '';
      let finalText = '';
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const r = e.results[i];
        if (r.isFinal) finalText += r[0].transcript;
        else interim += r[0].transcript;
      }
      if (interim && typeof onInterim === 'function') onInterim(interim);
      if (finalText && typeof onText === 'function') {
        onText(finalText.trim());
      }
    };
    myRecog.onerror = (e) => {
      if (myGen !== listenGen) return;
      recogOk = false;
      if (!recorderActive) listening = false;
      if (e.error === 'not-allowed' || e.error === 'service-not-allowed') {
        if (typeof onError === 'function') onError('麦克风权限被拒绝，请在浏览器地址栏允许使用麦克风');
      } else if (e.error === 'no-speech') {
        if (typeof onError === 'function') onError('没有听到说话，请再试一次');
      }
      //其他错误静默：由停止流程兜底
    };
    myRecog.onend = () => {
      if (myGen !== listenGen) return;
      recogOk = false;
      if (!recorderActive) listening = false;
    };
    try {
      myRecog.start();
      recogOk = true;
    } catch {
      if (!recorderActive) listening = false;
      recog = null;
    }
  }

  if (!recogOk) {
    // 换代并清掉聆听状态，让还在等麦克风权限的录音拿到流后立即关掉。
    ++listenGen;
    listening = false;
    return {
      ok: false,
      reason: '当前浏览器不支持语音输入，建议使用 Chrome / Edge',
    };
  }
  listening = true;
  return { ok: true };
}

/** 停止聆听；转写结果与录音 blob 通过 startListening 的回调返回 */
export function stopListening() {
  // 这里不换代：浏览器在 stop() 之后才送来最终识别结果，换代会把它丢掉。
  // 下一次 startListening 会换代，挡住上一轮迟到的回调。
  listening = false;
  if (recog) {
    try {
      recog.stop();
    } catch {
      /* 忽略未启动时的停止 */
    }
  }
  if (recorder && recorder.state !== 'inactive') {
    try {
      recorder.stop();
    } catch {
      cleanupRecorder();
    }
  } else {
    cleanupRecorder();
  }
}

function cleanupRecorder() {
  if (recStream) {
    recStream.getTracks().forEach((t) => t.stop());
    recStream = null;
  }
  recorder = null;
}

/** 去掉 markdown 标记的纯文本（用于 TTS 合成与时长估算） */
export function plainSpeechText(text) {
  return String(text)
    .replace(/`([^`\n]+)`/g, '$1')
    .replace(/\*\*([^*\n]+)\*\*/g, '$1')
    .replace(/[#*>|]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

/** 最大内存音频条目数（严格限制，防止长时间多轮对话产生 Blob URL 内存泄漏） */
export const MAX_AUDIO_STORE_ENTRIES = 30;

function safeRevokeUrl(url) {
  if (url && typeof url === 'string' && url.startsWith('blob:')) {
    try {
      URL.revokeObjectURL(url);
    } catch (_) {}
  }
}

/**
 * BoundedAudioStore — 具备 30 条硬上限、LRU 淘汰机制与自动 URL.revokeObjectURL 的安全存储
 */
export class BoundedAudioStore extends Map {
  get(key) {
    if (this.has(key)) {
      const val = super.get(key);
      super.delete(key);
      super.set(key, val);
      return val;
    }
    return undefined;
  }

  set(key, value) {
    if (this.has(key)) {
      const old = super.get(key);
      if (old && old.url && old.url !== value?.url) {
        safeRevokeUrl(old.url);
      }
      super.delete(key);
    }
    super.set(key, value);
    // LRU 淘汰：超出容量时驱逐最早访问的条目并释放 Blob URL
    if (this.size > MAX_AUDIO_STORE_ENTRIES) {
      const oldestKey = this.keys().next().value;
      if (oldestKey !== undefined) {
        const evicted = super.get(oldestKey);
        if (evicted && evicted.url) {
          safeRevokeUrl(evicted.url);
        }
        super.delete(oldestKey);
      }
    }
    return this;
  }

  delete(key) {
    if (this.has(key)) {
      const item = super.get(key);
      if (item && item.url) {
        safeRevokeUrl(item.url);
      }
    }
    return super.delete(key);
  }

  clear() {
    for (const item of this.values()) {
      if (item && item.url) {
        safeRevokeUrl(item.url);
      }
    }
    super.clear();
  }
}

/** 语音消息的内存暂存（严格受控 LRU 30 项上限） */
export const audioStore = new BoundedAudioStore();

/**
 * 辅助存储方法：设置录音并自动受控于 LRU 队列
 */
export function setAudioStore(msgId, rec) {
  audioStore.set(msgId, rec);
}
