// @ts-check
// 控制台访问令牌鉴权管理 (Console Token Auth)
// 令牌仅保存在当前浏览器的会话存储 (sessionStorage) 中，绝不写入 localStorage 或磁盘

import { openModal, closeModal } from './settings_ui.js';

const STORAGE_KEY = 'galgame_console_token';

let _pendingAuthPromise = null;

const TOKEN_INVALID_MESSAGE = '令牌只含英文字母、数字和符号。请切换到英文输入法，或重新复制令牌后再试。';

/**
 * Undo what an IME or a copy can add to a pasted token: full-width letters become plain
 * ASCII and invisible characters (zero-width spaces, BOM) are dropped.
 * @param {string} raw
 * @returns {string}
 */
export function normalizeConsoleToken(raw) {
  return String(raw || '').normalize('NFKC').replace(/\p{Cf}/gu, '').trim();
}

/**
 * A token the server can ever accept is printable ASCII; anything else cannot be sent in a
 * header (the browser throws) or matched, so it must not be attached to a request.
 * @param {string} token
 */
export function isUsableConsoleToken(token) {
  return /^[\x21-\x7e]+$/.test(token);
}

/**
 * 获取当前 sessionStorage 中的控制台令牌
 * @returns {string}
 */
export function getConsoleToken() {
  try {
    return normalizeConsoleToken(window.sessionStorage.getItem(STORAGE_KEY) || '');
  } catch {
    return '';
  }
}

/**
 * 设置控制台令牌并保存至 sessionStorage
 * @param {string} token
 */
function setConsoleToken(token) {
  try {
    const normalized = normalizeConsoleToken(token);
    if (normalized) {
      window.sessionStorage.setItem(STORAGE_KEY, normalized);
    } else {
      window.sessionStorage.removeItem(STORAGE_KEY);
    }
  } catch (err) {
    console.warn('[auth] Failed writing to sessionStorage:', err);
  }
}

/**
 * 弹出控制台令牌输入模态框，返回 Promise<string|null>
 * @param {string} [reason] 触发原因
 * @returns {Promise<string|null>}
 */
export function promptConsoleToken(reason = '') {
  if (_pendingAuthPromise) {
    return _pendingAuthPromise;
  }

  const modal = document.getElementById('tokenModal');
  const input = /** @type {HTMLInputElement | null} */ (document.getElementById('consoleTokenInput'));
  const saveBtn = document.getElementById('tokenModalSave');

  if (!modal || !input || !saveBtn) {
    // 降级回退至原生 prompt
    const val = window.prompt(reason || '请输入控制台访问令牌 (Console Access Token):', getConsoleToken());
    if (val !== null) {
      const token = normalizeConsoleToken(val);
      if (token && !isUsableConsoleToken(token)) {
        window.alert?.(TOKEN_INVALID_MESSAGE);
        return Promise.resolve(null);
      }
      setConsoleToken(token);
      return Promise.resolve(token || null);
    }
    return Promise.resolve(null);
  }

  const reasonEl = document.getElementById('tokenModalReason');
  _pendingAuthPromise = new Promise((resolve) => {
    input.value = getConsoleToken();
    if (reasonEl) {
      reasonEl.textContent = reason || '';
      reasonEl.hidden = !reason;
    }
    openModal(modal, { focus: input });

    const cleanup = () => {
      closeModal(modal);
      saveBtn.removeEventListener('click', onSave);
      input.removeEventListener('keydown', onKeydown);
      input.removeEventListener('input', onInput);
      input.setCustomValidity?.('');
      modal.querySelectorAll('[data-close]').forEach((btn) => {
        btn.removeEventListener('click', onCancel);
      });
      _pendingAuthPromise = null;
    };

    const onInput = () => input.setCustomValidity?.('');
    const onSave = () => {
      const token = normalizeConsoleToken(input.value);
      if (token && !isUsableConsoleToken(token)) {
        // Keep the dialog open: saving this token would only fail again on every request.
        input.setCustomValidity?.(TOKEN_INVALID_MESSAGE);
        input.reportValidity?.();
        return;
      }
      setConsoleToken(token);
      cleanup();
      resolve(token || null);
    };

    const onCancel = () => {
      cleanup();
      resolve(null);
    };

    saveBtn.addEventListener('click', onSave);
    modal.querySelectorAll('[data-close]').forEach((btn) => {
      btn.addEventListener('click', onCancel);
    });

    const onKeydown = (e) => {
      if (e.isComposing || e.keyCode === 229) return;
      if (e.key === 'Enter') {
        e.preventDefault();
        onSave();
      } else if (e.key === 'Escape') {
        e.preventDefault();
        onCancel();
      }
    };
    input.addEventListener('keydown', onKeydown);
    input.addEventListener('input', onInput);
  });

  return _pendingAuthPromise;
}

/**
 * 初始化全局 fetch 拦截器：自动注入 Bearer Token，并在 401 时自动弹窗重试
 */
export function initAuthInterceptor() {
  if (/** @type {any} */ (window)._galgame_auth_initialized) return;
  /** @type {any} */ (window)._galgame_auth_initialized = true;

  const nativeFetch = window.fetch.bind(window);

  window.fetch = async function (input, init = {}) {
    const url = typeof input === 'string' ? input : /** @type {any} */ (input)?.url || '';
    let parsedUrl = null;
    try {
      parsedUrl = new URL(url, location.origin);
    } catch (_) {
      parsedUrl = null;
    }
    const isApiRequest =
      parsedUrl !== null &&
      parsedUrl.origin === location.origin &&
      parsedUrl.pathname.startsWith('/api/');

    if (!isApiRequest) {
      return nativeFetch(input, init);
    }

    // 复制或构造 Headers 对象
    const headers = new Headers(init.headers || (typeof input === 'object' && /** @type {any} */ (input)?.headers ? /** @type {any} */ (input).headers : {}));
    const token = getConsoleToken();

    // An unusable stored token is left off, so the server answers 401 and the dialog reopens.
    if (token && isUsableConsoleToken(token) && !headers.has('Authorization')) {
      headers.set('Authorization', `Bearer ${token}`);
    }

    const modifiedInit = { ...init, headers };

    let res = await nativeFetch(input, modifiedInit);
    let reason = '';

    // 捕获 401 Unauthorized：拦截并提示输入 Token，输错清空并带原因重弹，点取消立即结束
    while (res.status === 401 && !url.includes('/api/providers/test')) {
      const newToken = await promptConsoleToken(reason);
      if (!newToken || !isUsableConsoleToken(newToken)) break;
      headers.set('Authorization', `Bearer ${newToken}`);
      res = await nativeFetch(input, { ...init, headers });
      if (res.status === 401) {
        if (getConsoleToken() === newToken) setConsoleToken('');
        reason = '令牌不对，请对照电脑上 data\\.console_token 里的内容重新输入。';
      }
    }

    return res;
  };
}
