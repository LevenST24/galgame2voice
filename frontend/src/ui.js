// Shared session drawer and toast rendering.
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

const TOAST_BATCH_MS = 1000;

// 新提示出现时撤掉已显示超过 1 秒的其他提示。1 秒内连着出现的几条留在同一批。
function dropToastsOlderThanBatch(root, now) {
  for (const toast of [...root.querySelectorAll('.toast')]) {
    if (!(now - toast._shownAt > TOAST_BATCH_MS)) continue;
    clearTimeout(toast._timer);
    toast.remove();
  }
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
  const now = Date.now();
  dropToastsOlderThanBatch(root, now);
  const el = document.createElement('div');
  el.className = `toast toast-${type}${action ? ' toast-with-action' : ''}`;
  el.dataset.msg = message;
  el._shownAt = now;
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
    const lastLine = last?.lines?.findLast(item => String(item.zh || item.ja || '').trim());
    const lastText = last && (last.content || lastLine?.zh || lastLine?.ja || last.japanese);
    const preview = lastText
      ? `${last.role === 'user' ? '你：' : ''}${lastText.replace(/[*`#\n]/g, ' ').slice(0, 26)}`
      : (s.lastLine ? s.lastLine.replace(/[*`#\n]/g, ' ').slice(0, 26) : '暂无消息');

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

const TRANSIENT_DIAGNOSTIC_CODES = new Set([
  'NETWORK_TIMEOUT',
  'CONNECTION_RESET',
  'NETWORK_CONNECTION_FAILED',
  'BAD_GATEWAY',
  'SERVER_OVERLOADED',
  'REQUEST_TIMEOUT',
]);

/** 失败是否需要去「对话模型」处理。只看错误码，不根据错误原文猜测。 */
export function needsModelSettings(_error, code = null) {
  const normalized = String(code ?? '').trim().toUpperCase();
  if (!normalized || normalized.startsWith('VOICE_PROFILE_')) return false;
  return !TRANSIENT_DIAGNOSTIC_CODES.has(normalized) && !normalized.endsWith('RATE_LIMIT');
}
