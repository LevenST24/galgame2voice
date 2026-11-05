// Telegram Settings Controller: bot configuration, proxy, field visibility, validation, connection test
import { requestJson } from '../api.js';

export const TELEGRAM_FIELDS = [
  'gTgEnabled',
  'gTgToken',
  'gTgChatId',
  'gTgProxyHost',
  'gTgProxyPort',
  'gTgProxyEnabled',
];

export class TelegramSettingsController {
  constructor(dom = {}, coordinator = {}) {
    this.dom = dom || {};
    this.coordinator = coordinator || {};
    this.telegramTestRevision = 0;
    this.telegramTestController = null;
    this.telegramTesting = false;
    this.disposed = false;
    this.cleanups = [];

    this._bindEvents();
  }

  _bind(element, type, handler) {
    if (!element || typeof element.addEventListener !== 'function') return;
    const bound = (...args) => {
      if (this.disposed) return;
      return handler(...args);
    };
    element.addEventListener(type, bound);
    this.cleanups.push(() => {
      if (typeof element.removeEventListener === 'function') {
        element.removeEventListener(type, bound);
      }
    });
  }

  _bindEvents() {
    if (this.dom.gTgEnabled) {
      this._bind(this.dom.gTgEnabled, 'change', () => this.updateTelegramFieldsVisibility());
    }
    if (this.dom.gTgTest) {
      this._bind(this.dom.gTgTest, 'click', () => this.testTelegram());
    }
    // Any change to Telegram configuration invalidates an in-flight connection test
    for (const field of TELEGRAM_FIELDS) {
      for (const event of ['input', 'change']) {
        if (this.dom[field]) {
          this._bind(this.dom[field], event, () => this.invalidateActiveTelegramTest());
        }
      }
    }
  }

  dispose() {
    if (this.disposed) return;
    this.disposed = true;
    this.telegramTestRevision += 1;
    if (this.telegramTestController) {
      this.telegramTestController.abort();
      this.telegramTestController = null;
    }
    const hadTesting = this.telegramTesting;
    this.telegramTesting = false;
    if (this.dom.gTgTest && (hadTesting || this.dom.gTgTest.disabled)) {
      this.dom.gTgTest.disabled = false;
    }
    for (const cleanup of this.cleanups) {
      try { cleanup(); } catch (_) {}
    }
    this.cleanups = [];
    this.dom = {};
    this.coordinator = {};
  }

  invalidateActiveTelegramTest() {
    this.telegramTestRevision += 1;
    if (this.telegramTestController) {
      this.telegramTestController.abort();
      this.telegramTestController = null;
    }
    this.telegramTesting = false;
    if (this.dom.gTgTest && !this.disposed) {
      this.dom.gTgTest.disabled = false;
    }
  }

  isTesting() {
    return this.telegramTesting;
  }

  updateTelegramFieldsVisibility() {
    if (this.disposed) return;
    const enabled = this.dom.gTgEnabled ? this.dom.gTgEnabled.checked : false;
    if (this.dom.gTgFieldsGroup) {
      this.dom.gTgFieldsGroup.style.opacity = enabled ? '1' : '0.45';
      this.dom.gTgFieldsGroup.style.pointerEvents = enabled ? 'auto' : 'none';
      this.dom.gTgFieldsGroup.inert = !enabled;
    }
  }

  validateTelegram() {
    const enabled = this.dom.gTgEnabled ? this.dom.gTgEnabled.checked : false;
    if (enabled && this.dom.gTgChatId && !this.dom.gTgChatId.value.trim()) {
      return {
        valid: false,
        message: '填写允许访问的用户ID，或先关闭Telegram再保存',
      };
    }
    return { valid: true };
  }

  getTelegramPayload() {
    const payload = {
      telegram_enabled: this.dom.gTgEnabled ? this.dom.gTgEnabled.checked : false,
      telegram_chat_id: this.dom.gTgChatId ? this.dom.gTgChatId.value.trim() : undefined,
      telegram_admin_ids: this.dom.gTgChatId ? this.dom.gTgChatId.value.trim() : undefined,
      telegram_proxy_enabled: this.dom.gTgProxyEnabled ? this.dom.gTgProxyEnabled.checked : false,
      telegram_proxy_host: this.dom.gTgProxyHost ? this.dom.gTgProxyHost.value.trim() || '127.0.0.1' : '127.0.0.1',
      telegram_proxy_port: this.dom.gTgProxyPort ? Number(this.dom.gTgProxyPort.value) || 10809 : 10809,
    };
    const token = this.dom.gTgToken ? this.dom.gTgToken.value.trim() : '';
    if (token) payload.telegram_bot_token = token;
    return payload;
  }

  fillTelegramConfig(s = {}, dirtyConfigFields = new Set()) {
    if (this.disposed) return;
    const fill = (field, apply) => {
      if (this.dom[field] && !dirtyConfigFields.has(field)) apply(this.dom[field]);
    };

    fill('gTgEnabled', (el) => {
      el.checked = Boolean(s.telegram_enabled);
      this.updateTelegramFieldsVisibility();
    });
    const token = s.telegram_bot_token || '';
    const masked = token.includes('****');
    fill('gTgToken', (el) => {
      el.value = '';
      el.placeholder = masked ? '已保存（输入新 Token 可覆盖）' : '未配置，如 123456:ABC-DEF…';
    });
    fill('gTgChatId', (el) => { el.value = s.telegram_chat_id || s.telegram_admin_ids || ''; });
    fill('gTgProxyHost', (el) => { el.value = s.telegram_proxy_host || ''; });
    fill('gTgProxyPort', (el) => { el.value = s.telegram_proxy_port || ''; });
    fill('gTgProxyEnabled', (el) => { el.checked = Boolean(s.telegram_proxy_enabled); });
  }

  async testTelegram() {
    if (this.disposed || this.telegramTesting || this.dom.gTgTest?.disabled) return;

    this.invalidateActiveTelegramTest();
    const controller = new AbortController();
    this.telegramTestController = controller;
    const currentRevision = ++this.telegramTestRevision;
    const testButton = this.dom.gTgTest;
    this.telegramTesting = true;
    if (testButton) testButton.disabled = true;

    try {
      const payload = {
        proxy_enabled: this.dom.gTgProxyEnabled ? this.dom.gTgProxyEnabled.checked : false,
        proxy_host: this.dom.gTgProxyHost ? this.dom.gTgProxyHost.value.trim() || '127.0.0.1' : '127.0.0.1',
        proxy_port: this.dom.gTgProxyPort ? Number(this.dom.gTgProxyPort.value) || 10809 : 10809,
      };
      const token = this.dom.gTgToken ? this.dom.gTgToken.value.trim() : '';
      if (token) payload.token = token;

      const data = await requestJson('/api/telegram/test', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
        signal: controller.signal,
      }, { timeoutMs: 60000 });

      if (this.disposed || currentRevision !== this.telegramTestRevision) return;
      this.coordinator.showToast?.(
        data.message || (data.success ? '连接成功' : '连接失败'),
        data.success ? 'success' : 'error'
      );
    } catch (e) {
      if (this.disposed || currentRevision !== this.telegramTestRevision || e.name === 'AbortError') return;
      this.coordinator.showToast?.(`测试失败: ${e.message || e}`, 'error');
    } finally {
      if (this.telegramTestController === controller) {
        this.telegramTestController = null;
      }
      if (!this.disposed && currentRevision === this.telegramTestRevision) {
        this.telegramTesting = false;
        if (testButton) testButton.disabled = false;
      }
    }
  }
}

export function createTelegramSettingsController(dom, coordinator) {
  return new TelegramSettingsController(dom, coordinator);
}
