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

/** 失败是否需要去「对话模型」处理：后端给出错误码时按码判断，频率超限只需稍后重试。 */
export function needsModelSettings(error, code = null) {
  if (/^VOICE_PROFILE_/.test(code || '')) return false;
  if (code) return !/RATE_LIMIT$/.test(code);
  return /模型|密钥|Key|Provider|401|403|未配置/i.test(error || '');
}
