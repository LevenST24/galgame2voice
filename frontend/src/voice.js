// 语音能力：麦克风录音（MediaRecorder）+ 语音转写（SpeechRecognition）+ 语音回复播放（SpeechSynthesis）
let recog = null;
let listening = false;
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
 * 开始录音 + 并行转写（转写不可用时仅录音）
 * @returns {{ok: boolean, reason?: string}}
 */
export function startListening({ onInterim, onText, onRecorded, onError }) {
  if (listening) return { ok: true };
  let recOk = false;
  let recogOk = false;

  // 1) 录音（可选能力，失败不阻断转写）
  if (recorderSupported()) {
    navigator.mediaDevices
      .getUserMedia({ audio: true })
      .then((stream) => {
        if (!listening) {
          stream.getTracks().forEach((t) => t.stop());
          return;
        }
        recStream = stream;
        recChunks = [];
        recStartTs = Date.now();
        const mime = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4'].find(
          (m) => MediaRecorder.isTypeSupported && MediaRecorder.isTypeSupported(m)
        );
        recorder = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
        recorder.ondataavailable = (e) => {
          if (e.data && e.data.size) recChunks.push(e.data);
        };
        recorder.onstop = () => {
          const dur = Math.max(1, Math.round((Date.now() - recStartTs) / 1000));
          const blob = recChunks.length ? new Blob(recChunks, { type: recorder.mimeType || 'audio/webm' }) : null;
          cleanupRecorder();
          if (blob && typeof onRecorded === 'function') onRecorded(blob, dur);
        };
        recorder.start();
        recOk = true;
      })
      .catch(() => {
        if (!recogOk && typeof onError === 'function') onError('无法访问麦克风，请在浏览器中允许麦克风权限');
      });
  }

  // 2) 转写（可选能力）
  if (voiceSupported()) {
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    recog = new SR();
    recog.lang = 'zh-CN';
    recog.interimResults = true;
    recog.continuous = false;
    recog.maxAlternatives = 1;
    listening = true;
    recog.onresult = (e) => {
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
    recog.onerror = (e) => {
      listening = false;
      if (e.error === 'not-allowed' || e.error === 'service-not-allowed') {
        if (typeof onError === 'function') onError('麦克风权限被拒绝，请在浏览器地址栏允许使用麦克风');
      } else if (e.error === 'no-speech') {
        if (typeof onError === 'function') onError('没有听到说话，请再试一次');
      }
      //其他错误静默：由停止流程兜底
    };
    recog.onend = () => {
      listening = false;
    };
    try {
      recog.start();
      recogOk = true;
    } catch {
      listening = false;
      recog = null;
    }
  }

  if (!recOk && !recogOk) {
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

/* ---------- AI 语音回复（浏览器 TTS 备用方案，可反复播放） ---------- */
let currentUtter = null;
let currentSession = null; // 会话令牌：stopSpeaking 后旧的 onend 不再续播

/** 去掉 markdown 标记的纯文本（用于 TTS 合成与时长估算） */
export function plainSpeechText(text) {
  return String(text)
    .replace(/`([^`\n]+)`/g, '$1')
    .replace(/\*\*([^*\n]+)\*\*/g, '$1')
    .replace(/[#*>|]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

function plainForSpeech(text) {
  return plainSpeechText(text).slice(0, 1200);
}

/** 估算语音条展示时长（秒） */
export function estimateDuration(text) {
  const n = plainForSpeech(text).replace(/\s/g, '').length;
  return Math.min(300, Math.max(1, Math.round(n / 4)));
}

/**
 * 朗读文本；同一时间只允许一段语音
 * @returns {boolean} 是否成功启动
 */
export function speakText(text, { onEnd } = {}) {
  if (!('speechSynthesis' in window)) return false;
  const plain = plainForSpeech(text);
  if (!plain) return false;
  window.speechSynthesis.cancel();
  // 分句成 ≤100 字的块排队播放，规避 Chrome 长文本约 15 秒截断的已知问题
  const parts = plain.match(/[^。！？；!?;\n]+[。！？；!?;\n]?/g) || [plain];
  const queue = [];
  let buf = '';
  for (const p of parts) {
    if ((buf + p).length > 100 && buf) {
      queue.push(buf);
      buf = p;
    } else {
      buf += p;
    }
  }
  if (buf) queue.push(buf);

  const session = {
    paused: false,
    idx: 0,
    speakNext: null,
  };
  currentSession = session;
  const speakNext = () => {
    if (currentSession !== session || session.paused) return; // 已被打断或暂停
    if (session.idx >= queue.length) {
      currentUtter = null;
      if (typeof onEnd === 'function') onEnd();
      return;
    }
    const u = new SpeechSynthesisUtterance(queue[session.idx++]);
    u.lang = 'zh-CN';
    u.rate = 1.05;
    const zh = window.speechSynthesis.getVoices().find((v) => v.lang && v.lang.startsWith('zh'));
    if (zh) u.voice = zh;
    u.onend = speakNext;
    u.onerror = speakNext;
    currentUtter = u;
    window.speechSynthesis.speak(u);
  };
  session.speakNext = speakNext;
  speakNext();
  return true;
}

export function pauseSpeaking() {
  if (currentSession) {
    currentSession.paused = true;
    if (currentSession.idx > 0) {
      currentSession.idx--; // 续播时从该句重新朗读
    }
  }
  if ('speechSynthesis' in window) window.speechSynthesis.cancel();
  currentUtter = null;
}

export function resumeSpeaking() {
  if (currentSession && currentSession.paused) {
    currentSession.paused = false;
    currentSession.speakNext();
  }
}

export function stopSpeaking() {
  if (currentSession) {
    currentSession.paused = true;
  }
  currentSession = null;
  if ('speechSynthesis' in window) window.speechSynthesis.cancel();
  currentUtter = null;
}

/** 语音消息的内存暂存（不进 localStorage，刷新后回退为纯文字） */
export const audioStore = new Map();
