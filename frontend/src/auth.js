// 控制台访问令牌鉴权管理 (Console Token Auth)
// 令牌仅保存在当前浏览器的会话存储 (sessionStorage) 中，绝不写入 localStorage 或磁盘

const STORAGE_KEY = 'galgame_console_token';

let _pendingAuthPromise = null;

/**
 * 获取当前 sessionStorage 中的控制台令牌
 * @returns {string}
 */
export function getConsoleToken() {
  try {
    return (window.sessionStorage.getItem(STORAGE_KEY) || '').trim();
  } catch {
    return '';
  }
}

/**
 * 设置控制台令牌并保存至 sessionStorage
 * @param {string} token
 */
export function setConsoleToken(token) {
  try {
    if (token && token.trim()) {
      window.sessionStorage.setItem(STORAGE_KEY, token.trim());
    } else {
      window.sessionStorage.removeItem(STORAGE_KEY);
    }
  } catch (err) {
    console.warn('[auth] Failed writing to sessionStorage:', err);
  }
}

/**
 * 清除当前会话的控制台令牌
 */
export function clearConsoleToken() {
  try {
    window.sessionStorage.removeItem(STORAGE_KEY);
  } catch {}
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
  const input = document.getElementById('consoleTokenInput');
  const saveBtn = document.getElementById('tokenModalSave');

  if (!modal || !input || !saveBtn) {
    // 降级回退至原生 prompt
    const val = window.prompt(reason || '请输入控制台访问令牌 (Console Access Token):', getConsoleToken());
    if (val !== null) {
      setConsoleToken(val);
      return Promise.resolve(val.trim());
    }
    return Promise.resolve(null);
  }

  _pendingAuthPromise = new Promise((resolve) => {
    input.value = getConsoleToken();
    modal.hidden = false;
    modal.classList.add('active');

    const cleanup = () => {
      modal.hidden = true;
      modal.classList.remove('active');
      saveBtn.removeEventListener('click', onSave);
      modal.querySelectorAll('[data-close]').forEach((btn) => {
        btn.removeEventListener('click', onCancel);
      });
      _pendingAuthPromise = null;
    };

    const onSave = () => {
      const token = input.value.trim();
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

    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        onSave();
      } else if (e.key === 'Escape') {
        e.preventDefault();
        onCancel();
      }
    }, { once: true });

    setTimeout(() => input.focus(), 50);
  });

  return _pendingAuthPromise;
}

/**
 * 初始化全局 fetch 拦截器：自动注入 Bearer Token，并在 401 时自动弹窗重试
 */
export function initAuthInterceptor() {
  if (window._galgame_auth_initialized) return;
  window._galgame_auth_initialized = true;

  const nativeFetch = window.fetch.bind(window);

  window.fetch = async function (input, init = {}) {
    const url = typeof input === 'string' ? input : input?.url || '';
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
    const headers = new Headers(init.headers || (typeof input === 'object' && input?.headers ? input.headers : {}));
    const token = getConsoleToken();

    if (token && !headers.has('Authorization')) {
      headers.set('Authorization', `Bearer ${token}`);
    }

    const modifiedInit = { ...init, headers };

    const res = await nativeFetch(input, modifiedInit);

    // 捕获 401 Unauthorized：拦截并提示输入 Token，然后重试单次
    if (res.status === 401 && !url.includes('/api/providers/test')) {
      const newToken = await promptConsoleToken('服务需要访问令牌验证');
      if (newToken) {
        headers.set('Authorization', `Bearer ${newToken}`);
        return nativeFetch(input, { ...init, headers });
      }
    }

    return res;
  };
}
