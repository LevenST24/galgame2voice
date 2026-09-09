// DOM 渲染工具：会话列表、消息、语音条、空状态、Toast
import { audioStore, estimateDuration } from './voice.js';
import { GREETING } from './store.js';



export function escapeHtml(text) {
  return String(text)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
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

export function showToast(message, type = 'info') {
  const root = document.getElementById('toastRoot');
  if (!root) return;
  // 同文案 toast 去重：已存在则只重置消失计时，避免堆叠闪烁
  const existing = [...root.querySelectorAll('.toast')].find((t) => t.textContent === message && !t.classList.contains('leaving'));
  if (existing) {
    clearTimeout(existing._timer);
    existing._timer = setTimeout(() => {
      existing.classList.add('leaving');
      setTimeout(() => existing.remove(), 260);
    }, 2600);
    return;
  }
  const el = document.createElement('div');
  el.className = `toast toast-${type}`;
  el.textContent = message;
  root.appendChild(el);
  el._timer = setTimeout(() => {
    el.classList.add('leaving');
    setTimeout(() => el.remove(), 260);
  }, 2600);
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
    const persona = s.settings && s.settings.systemPrompt ? ' · 已设人设' : '';

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
    item.querySelector('.si-preview').textContent = preview + persona;

    item.addEventListener('click', () => onSelect(s.id));
    item.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onSelect(s.id); }
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
  durEl.textContent = fmtDur(dur);

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

  const triggerToggle = (e) => {
    if (e) {
      e.preventDefault();
      e.stopPropagation();
    }
    onToggle({
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
    });
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

export function createMessageEl(msg, { streaming = false, onPlayVoice, onResolveJapanese, autoTranslate = false } = {}) {
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

  return { el, contentEl: content };
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

function buildEmptyState() {
  const empty = document.createElement('div');
  empty.className = 'empty animate-in';
  empty.innerHTML = `
    <span class="animate-quill"><svg class="icon"><use href="#i-quill"></use></svg></span>
    <p class="empty-kicker">Gal2Voice · Session Ready</p>
    <h2 class="empty-title">今天想聊点<em>什么</em>？</h2>
    <p class="empty-sub">这是一个全新的会话，拥有独立的上下文与人设配置。<br />在下方输入框发送消息，或点击麦克风开始畅聊。</p>`;
  return empty;
}

export function renderMessages(container, session, { animate = false, onPick, onPlayVoice, onResolveJapanese, autoTranslate = false } = {}) {
  container.innerHTML = '';
  const inner = document.createElement('div');
  inner.className = 'messages-inner';

  if (!session || !session.messages.length) {
    inner.classList.add('is-empty');
    inner.appendChild(buildEmptyState(onPick));
  } else {
    session.messages.forEach((m, idx) => {
      const { el } = createMessageEl(m, { onPlayVoice, onResolveJapanese, autoTranslate });
      if (animate) {
        el.classList.add('animate-in');
        el.style.setProperty('--i', Math.min(idx, 10));
      }
      inner.appendChild(el);
    });
  }
  container.appendChild(inner);
  return inner;
}
