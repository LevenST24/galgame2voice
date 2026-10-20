// DOM 渲染工具：会话列表、消息、语音条、空状态、Toast
import { audioStore, estimateDuration } from './voice.js';
import { GREETING } from './store.js';



export function escapeHtml(text) {
  return String(text)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

export function formatContent(text) {
  return escapeHtml(text)
    .replace(/`([^`\n]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>')
    .replace(/\n/g, '<br>');
}

export function timeLabel(ts) {
  const d = new Date(ts);
  const now = new Date();
  const pad = (n) => String(n).padStart(2, '0');
  const sameDay =
    d.getFullYear() === now.getFullYear() &&
    d.getMonth() === now.getMonth() &&
    d.getDate() === now.getDate();
  if (sameDay) return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  if (d.getFullYear() === now.getFullYear()) return `${d.getMonth() + 1}月${d.getDate()}日`;
  return `${d.getFullYear()}/${pad(d.getMonth() + 1)}/${pad(d.getDate())}`;
}

export function showToast(message, type = 'info', options = null) {
  const root = document.getElementById('toastRoot');
  if (!root) return;
  const action = options && typeof options === 'object' && typeof options.onAction === 'function'
    ? { label: options.actionLabel || '前往处理', onAction: options.onAction }
    : null;
  // 同文案 toast 去重：已存在则只重置消失计时，避免堆叠闪烁
  const existing = [...root.querySelectorAll('.toast')].find((t) => t.dataset.msg === message && !t.classList.contains('leaving'));
  const duration = action ? 10000 : (type === 'error' ? 8000 : Math.min(7000, Math.max(3200, message.length * 80)));
  if (existing) {
    clearTimeout(existing._timer);
    existing._timer = setTimeout(() => {
      existing.classList.add('leaving');
      setTimeout(() => existing.remove(), 260);
    }, duration);
    return;
  }
  const el = document.createElement('div');
  el.className = `toast toast-${type}${action ? ' toast-with-action' : ''}`;
  el.dataset.msg = message;
  if (action) {
    const span = document.createElement('span');
    span.className = 'toast-text';
    span.textContent = message;
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'toast-action-btn';
    btn.textContent = action.label;
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      clearTimeout(el._timer);
      el.remove();
      action.onAction();
    });
    el.append(span, btn);
  } else {
    el.textContent = message;
  }
  root.appendChild(el);
  el._timer = setTimeout(() => {
    el.classList.add('leaving');
    setTimeout(() => el.remove(), 260);
  }, duration);
}

/** Toast 停在输入区上方：输入框随内容长高时同步避让，不遮挡输入框也不拦截点击。 */
export function keepToastsAboveComposer(composerArea) {
  if (!composerArea || typeof ResizeObserver !== 'function') return;
  const apply = () => document.documentElement.style.setProperty(
    '--composer-clearance', `${Math.ceil(composerArea.getBoundingClientRect().height)}px`,
  );
  new ResizeObserver(apply).observe(composerArea);
  apply();
}

export function renderSessionList(container, { sessions, activeId, onSelect, onDelete }) {
  container.innerHTML = '';
  const frag = document.createDocumentFragment();
  sessions.forEach((s, idx) => {
    const item = document.createElement('div');
    item.className = `session-item animate-in${s.id === activeId ? ' active' : ''}`;
    item.style.setProperty('--i', Math.min(idx, 10));
    item.dataset.id = s.id;
    item.tabIndex = 0;
    item.setAttribute('role', 'button');

    const last = s.messages[s.messages.length - 1];
    const preview = last
      ? `${last.role === 'user' ? '你：' : ''}${last.content.replace(/[*`#\n]/g, ' ').slice(0, 26)}`
      : '暂无消息';

    item.innerHTML = `
      <div class="si-main">
        <div class="si-title"></div>
        <div class="si-meta">
          <span class="si-time">${timeLabel(s.updatedAt)}</span>
          <span class="si-preview"></span>
        </div>
      </div>
      <button class="si-del" title="删除对话" aria-label="删除对话">
        <svg class="icon"><use href="#i-trash"></use></svg>
      </button>`;
    item.querySelector('.si-title').textContent = s.title;
    item.querySelector('.si-title').title = s.title;
    item.querySelector('.si-preview').textContent = preview;

    item.addEventListener('click', () => onSelect(s.id));
    item.addEventListener('keydown', (e) => {
      if (e.target === item && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); onSelect(s.id); }
    });
    item.querySelector('.si-del').addEventListener('click', (e) => {
      e.stopPropagation();
      onDelete(s.id);
    });
    frag.appendChild(item);
  });
  container.appendChild(frag);
}

// 由消息 id 生成确定性伪随机波形：同一消息每次渲染波形一致，像真实录音
function waveformBars(seedStr, count) {
  let x = 2166136261;
  for (let i = 0; i < seedStr.length; i++) {
    x ^= seedStr.charCodeAt(i);
    x = Math.imul(x, 16777619) >>> 0;
  }
  const bars = [];
  for (let i = 0; i < count; i++) {
    x = (Math.imul(x, 1103515245) + 12345) >>> 0;
    bars.push(0.18 + ((x >>> 16) % 1000) / 1000 * 0.82);
  }
  return bars;
}

function fmtDur(d) {
  const s = Math.max(1, Math.round(d));
  return s >= 60 ? `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}` : `${s}″`;
}

/**
 * 语音消息条：播放按钮 + 渐进点亮波形 + 时长
 * onToggle 收到控制器 { setPlaying, setProgress }，由播放器上报进度
 */
function buildVoiceBar({ dur, kind, seed, onToggle }) {
  const bar = document.createElement('div');
  bar.className = `voice-bar kind-${kind}`;
  bar.setAttribute('role', 'button');
  bar.setAttribute('tabindex', '0');
  bar.setAttribute('aria-label', '播放 / 暂停语音');
  // AI 回复条更窄（只是个播放入口）；用户录音条按录音时长加宽
  const barW = kind === 'ai'
    ? Math.round(Math.min(190, Math.max(118, 84 + dur * 1.6)))
    : Math.round(Math.min(232, Math.max(150, 104 + dur * 3)));
  bar.style.setProperty('--vb-w', `${barW}px`);

  const btn = document.createElement('span');
  btn.className = 'vb-play';
  btn.setAttribute('aria-hidden', 'true');
  btn.innerHTML =
    '<svg class="icon vb-ico-play"><use href="#i-play"></use></svg><svg class="icon vb-ico-pause"><use href="#i-pause"></use></svg>';

  // 柱数跟随条宽估算（每根约占 5px），避免波形溢出穿模
  const count = Math.max(14, Math.min(40, Math.round((barW - 92) / 5)));
  const waves = document.createElement('span');
  waves.className = 'vb-waves';
  waveformBars(seed || `${kind}-${dur}`, count).forEach((h) => {
    const i = document.createElement('i');
    i.style.height = `${Math.round(h * 100)}%`;
    waves.appendChild(i);
  });

  const durEl = document.createElement('span');
  durEl.className = 'vb-dur';
  durEl.textContent = `${kind === 'ai' ? '≈' : ''}${fmtDur(dur)}`;
  if (kind === 'ai') durEl.title = '预计语音时长';

  bar.append(btn, waves, durEl);

  let playing = false;
  let progress = 0;
  let loading = false;
  const paint = () => {
    const lit = Math.round(progress * count);
    [...waves.children].forEach((el, idx) => el.classList.toggle('on', idx < lit));
    bar.classList.toggle('playing', playing);
    bar.classList.toggle('loading', loading);
    const label = playing
      ? '点击暂停语音'
      : (loading ? '正在合成语音，点击取消' : (progress > 0 && progress < 1 ? '点击继续播放语音' : '点击播放语音'));
    bar.title = label;
    bar.setAttribute('aria-label', label.replace('点击', ''));
  };

  const control = {
    setPlaying: (v) => {
      playing = Boolean(v);
      paint();
    },
    setProgress: (p) => {
      progress = Math.min(1, Math.max(0, p || 0));
      paint();
    },
    setLoading: (v) => {
      loading = Boolean(v);
      paint();
    },
  };
  bar.voiceControl = control;
  const triggerToggle = (e) => {
    if (e) {
      e.preventDefault();
      e.stopPropagation();
    }
    onToggle(control);
  };

  bar.addEventListener('click', triggerToggle);
  bar.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') {
      triggerToggle(e);
    }
  });

  paint();
  return bar;
}

/** 失败是否需要去「对话模型」处理：后端给出错误码时按码判断，频率超限只需稍后重试。 */
export function needsModelSettings(error, code = null) {
  if (code) return !/RATE_LIMIT$/.test(code);
  return /模型|密钥|Key|Provider|401|403|未配置/i.test(error || '');
}

/** 未收到回复的用户消息：把原因留在消息下方，Toast 消失后仍能看到并重新发送。 */
function buildFailureNote(msg, { onRetry, onOpenModels } = {}) {
  const note = document.createElement('div');
  note.className = 'msg-failure';
  note.setAttribute('role', 'status');
  const text = document.createElement('p');
  text.className = 'msg-failure-text';
  text.textContent = `未收到回复：${msg.failed.error}`;
  note.appendChild(text);
  const actions = document.createElement('div');
  actions.className = 'msg-failure-actions';
  const addAction = (label, onClick) => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'msg-failure-btn';
    btn.textContent = label;
    btn.addEventListener('click', onClick);
    actions.appendChild(btn);
  };
  if (typeof onRetry === 'function') addAction('重新发送', () => onRetry(msg));
  if (typeof onOpenModels === 'function' && needsModelSettings(msg.failed.error, msg.failed.code)) {
    const label = msg.failed?.code === 'empty_reply' ? '去设置' : '配置对话模型';
    addAction(label, () => onOpenModels());
  }
  if (actions.children.length) note.appendChild(actions);
  return note;
}

export function createMessageEl(msg, { streaming = false, onPlayVoice, onResolveJapanese, autoTranslate = false, onRetry, onOpenModels } = {}) {
  const el = document.createElement('article');
  el.className = `msg ${msg.role}`;

  if (msg.role === 'assistant') {
    const av = document.createElement('div');
    av.className = 'msg-avatar';
    av.innerHTML = '<svg class="icon"><use href="#i-nib"></use></svg>';
    el.appendChild(av);
  }

  const bubble = document.createElement('div');
  bubble.className = 'msg-bubble';
  bubble.title = timeLabel(msg.ts || Date.now());
  const content = document.createElement('div');
  content.className = `msg-content${streaming ? ' streaming' : ''}`;
  content.innerHTML = formatContent(msg.content);
  bubble.append(content);

  const isGuideOrNoVoice = Boolean(
    msg.noVoice ||
    msg.isGuide ||
    msg.content === GREETING ||
    (typeof msg.content === 'string' && (
      msg.content.includes('你好，我是 **Gal2Voice**') ||
      msg.content.startsWith('（请求失败') ||
      msg.content.startsWith('（流式传输中断')
    ))
  );

  if (msg.role === 'assistant' && !streaming && msg.content && !isGuideOrNoVoice) {
    const actions = document.createElement('div');
    actions.className = 'msg-actions';

    if (typeof onPlayVoice === 'function') {
      actions.appendChild(
        buildVoiceBar({
          dur: estimateDuration(msg.content),
          kind: 'ai',
          seed: msg.id || msg.content.slice(0, 16),
          onToggle: (ctl) => onPlayVoice(msg, ctl),
        })
      );
    }

    const transBtn = document.createElement('button');
    transBtn.className = 'msg-trans-btn';
    transBtn.type = 'button';
    transBtn.title = '查看翻译';
    transBtn.innerHTML = '<svg class="icon"><use href="#i-translate"></use></svg><span>翻译</span>';
    actions.appendChild(transBtn);

    const jaBox = document.createElement('div');
    jaBox.className = 'msg-ja-box hidden';

    const renderJapaneseText = (jaText) => {
      jaBox.dataset.loaded = 'true';
      jaBox.innerHTML = `
        <button class="msg-ja-copy" type="button" title="复制文本">
          <svg class="icon"><use href="#i-copy"></use></svg>
          <span>复制</span>
        </button>
        <div class="msg-ja-text"></div>`;
      jaBox.querySelector('.msg-ja-text').textContent = jaText;

      const copyBtn = jaBox.querySelector('.msg-ja-copy');
      copyBtn.addEventListener('click', async (e) => {
        e.stopPropagation();
        try {
          await navigator.clipboard.writeText(jaText);
          copyBtn.innerHTML = '<svg class="icon"><use href="#i-check"></use></svg><span>已复制</span>';
          copyBtn.classList.add('copied');
          setTimeout(() => {
            copyBtn.innerHTML = '<svg class="icon"><use href="#i-copy"></use></svg><span>复制</span>';
            copyBtn.classList.remove('copied');
          }, 1800);
        } catch {
          showToast('复制失败，请手动选中复制', 'error');
        }
      });
    };

    const expandJaBox = async () => {
      transBtn.classList.add('active');
      jaBox.classList.remove('hidden');

      if (jaBox.dataset.loaded === 'true') return;

      let jaText = (msg.japanese || '').trim();
      if (!jaText && typeof onResolveJapanese === 'function') {
        jaBox.innerHTML = '<div class="msg-ja-loading">正在获取翻译…</div>';
        jaText = (await onResolveJapanese(msg) || '').trim();
      }

      if (!jaText) {
        jaBox.innerHTML = '<div class="msg-ja-empty">未获取到翻译内容</div>';
        return;
      }

      renderJapaneseText(jaText);
    };

    const collapseJaBox = () => {
      jaBox.classList.add('hidden');
      transBtn.classList.remove('active');
    };

    transBtn.addEventListener('click', () => {
      const isShowing = !jaBox.classList.contains('hidden');
      if (isShowing) {
        collapseJaBox();
      } else {
        expandJaBox();
      }
    });

    if (autoTranslate) {
      expandJaBox();
    }

    bubble.appendChild(actions);
    bubble.appendChild(jaBox);
  }
  if (msg.role === 'user' && typeof onPlayVoice === 'function') {
    const rec = audioStore.get(msg.id);
    const dur = (rec && rec.dur) || msg.dur;
    if (rec || msg.dur) {
      bubble.prepend(
        buildVoiceBar({ dur: dur || 3, kind: 'user', seed: msg.id, onToggle: (ctl) => onPlayVoice(msg, ctl) })
      );
    }
  }
  el.appendChild(bubble);
  if (msg.role === 'user' && typeof msg.failed?.error === 'string') {
    el.classList.add('has-failure');
    el.appendChild(buildFailureNote(msg, { onRetry, onOpenModels }));
  }

  return { el, contentEl: content, voiceCtl: bubble.querySelector('.voice-bar')?.voiceControl || null };
}

export function createTypingEl() {
  const el = document.createElement('article');
  el.className = 'msg assistant';
  el.innerHTML = `
    <div class="msg-avatar"><svg class="icon"><use href="#i-nib"></use></svg></div>
    <div class="msg-bubble">
      <div class="typing-dots"><span></span><span></span><span></span></div>
    </div>`;
  return el;
}

function buildEmptyState({ onOpenModels, onOpenVoice } = {}) {
  const empty = document.createElement('div');
  empty.className = 'empty animate-in';
  empty.innerHTML = `
    <span class="animate-quill"><svg class="icon"><use href="#i-quill"></use></svg></span>
    <p class="empty-kicker">Gal2Voice · 与声音相伴</p>
    <h2 class="empty-title">今天想聊点<em>什么</em>？</h2>
    <p class="empty-sub">从一句问候开始吧。<br />在会话设置中选择角色，开启属于你们的聊天。</p>`;
  if (typeof onOpenModels === 'function' || typeof onOpenVoice === 'function') {
    const actions = document.createElement('div');
    actions.className = 'chips empty-quick-actions';
    if (typeof onOpenModels === 'function') {
      const mBtn = document.createElement('button');
      mBtn.type = 'button';
      mBtn.className = 'chip';
      mBtn.textContent = '1. 配置对话模型';
      mBtn.addEventListener('click', onOpenModels);
      actions.appendChild(mBtn);
    }
    if (typeof onOpenVoice === 'function') {
      const vBtn = document.createElement('button');
      vBtn.type = 'button';
      vBtn.className = 'chip';
      vBtn.textContent = '2. 选择或创建角色音色';
      vBtn.addEventListener('click', onOpenVoice);
      actions.appendChild(vBtn);
    }
    empty.appendChild(actions);
  }
  return empty;
}

export function renderMessages(container, session, { animate = false, onPick, onPlayVoice, onResolveJapanese, onLoadOlder, onOpenModels, onOpenVoice, onRetry, autoTranslate = false, historyBlocked = false } = {}) {
  container.innerHTML = '';
  const inner = document.createElement('div');
  inner.className = 'messages-inner';

  if (!session || !session.messages.length) {
    inner.classList.add('is-empty');
    inner.appendChild(buildEmptyState({ onPick, onOpenModels, onOpenVoice }));
  } else {
    const frag = document.createDocumentFragment();
    if (session.hasMoreHistory && typeof onLoadOlder === 'function') {
      const moreWrap = document.createElement('div');
      moreWrap.className = 'history-more-wrap';
      const moreBtn = document.createElement('button');
      moreBtn.type = 'button';
      moreBtn.className = 'btn ghost history-more-btn';
      moreBtn.disabled = historyBlocked || Boolean(session.loadingOlder);
      moreBtn.textContent = session.loadingOlder ? '正在加载更早的聊天记录…'
        : historyBlocked ? '回复完成后可查看更早的聊天记录' : '↑ 查看更早的聊天记录';
      moreBtn.addEventListener('click', () => onLoadOlder(session));
      moreWrap.appendChild(moreBtn);
      frag.appendChild(moreWrap);
    }
    const lastIdx = session.messages.length - 1;
    session.messages.forEach((m, idx) => {
      // Only the latest message can be resent; older failures stay as a record.
      const { el } = createMessageEl(m, {
        onPlayVoice, onResolveJapanese, autoTranslate, onOpenModels,
        onRetry: idx === lastIdx ? onRetry : undefined,
      });
      if (animate) {
        el.classList.add('animate-in');
        el.style.setProperty('--i', Math.min(idx, 10));
      }
      frag.appendChild(el);
    });
    inner.appendChild(frag);
  }
  container.appendChild(inner);
  return inner;
}
