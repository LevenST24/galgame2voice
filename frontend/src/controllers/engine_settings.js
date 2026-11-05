// Engine Settings Controller: Restart, precision toggle/auto-save, audio cache actions
import { requestJson } from '../api.js';
import { waitForEngine } from './engine_recovery.js';
import { runSettingsMutation, saveConfirmedConfig } from './config_recovery.js';
import { clearMemAudioCache } from '../cache.js';

export const UNCONFIRMED_RESTART_MESSAGE = '重启请求结果未确认；当前引擎已在线。计算方式以「启动计算方式」显示的状态为准。';

export class EngineSettingsController {
  constructor(dom = {}, coordinator = {}) {
    this.dom = dom || {};
    this.coordinator = coordinator || {};
    this.restarting = false;
    this.engineRestartUnconfirmed = false;
    this.precisionRevision = 0;
    this.engineStatusRevision = 0;
    this.engineWaitController = null;
    this.cacheClearing = false;
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
    if (this.dom.gBtnRestartSovits) {
      this._bind(this.dom.gBtnRestartSovits, 'click', () => this.handleRestartSovits());
    }
    if (this.dom.gBtnTogglePrecision) {
      this._bind(this.dom.gBtnTogglePrecision, 'click', () => this.handleTogglePrecision());
    }
    if (this.dom.gPrecision) {
      this._bind(this.dom.gPrecision, 'change', () => this.onPrecisionChange());
    }
    if (this.dom.gBtnClearCache) {
      this._bind(this.dom.gBtnClearCache, 'click', () => this.handleClearCache());
    }

    const btnGoPrecision = typeof document !== 'undefined' ? (this.dom.gBtnGoPrecision || document.getElementById?.('gBtnGoPrecision')) : null;
    if (btnGoPrecision) {
      this._bind(btnGoPrecision, 'click', () => this.coordinator.goToInferenceTab?.());
    }

    const btnGoSovitsSetup = typeof document !== 'undefined' ? (this.dom.gBtnGoSovitsSetup || document.getElementById?.('gBtnGoSovitsSetup')) : null;
    if (btnGoSovitsSetup) {
      this._bind(btnGoSovitsSetup, 'click', () => {
        this.coordinator.goToInferenceTab?.();
        this.dom.gSovitsDirectory?.focus?.();
      });
    }
  }

  dispose() {
    if (this.disposed) return;
    this.disposed = true;
    if (this.engineWaitController) {
      this.engineWaitController.abort();
      this.engineWaitController = null;
    }
    this.precisionRevision += 1;
    this.engineStatusRevision += 1;
    if (this.restarting || this.dom.gBtnRestartSovits?.disabled) {
      if (this.dom.gBtnRestartSovits) {
        this.dom.gBtnRestartSovits.disabled = false;
        this.dom.gBtnRestartSovits.innerHTML = '<svg class="icon"><use href="#i-play"></use></svg><span>重启 SoVITS 引擎</span>';
      }
      if (this.dom.gBtnTogglePrecision) {
        this.dom.gBtnTogglePrecision.disabled = false;
      }
      this.restarting = false;
    }
    if (this.cacheClearing || this.dom.gBtnClearCache?.disabled) {
      if (this.dom.gBtnClearCache) {
        this.dom.gBtnClearCache.disabled = false;
      }
      this.cacheClearing = false;
    }
    for (const cleanup of this.cleanups) {
      try { cleanup(); } catch (_) {}
    }
    this.cleanups = [];
    this.dom = {};
    this.coordinator = {};
  }

  isRestarting() {
    return this.restarting;
  }

  isEngineRestartUnconfirmed() {
    return this.engineRestartUnconfirmed;
  }

  getPrecisionRevision() {
    return this.precisionRevision;
  }

  async onPrecisionChange() {
    if (this.disposed || !this.dom.gPrecision) return;
    const revision = ++this.precisionRevision;
    const val = this.dom.gPrecision.value;
    try {
      await saveConfirmedConfig({ inference_precision: val });
      if (this.disposed || revision !== this.precisionRevision) return;
      const labelMap = {
        auto: '自动判定 (Auto)',
        fp16: 'FP16 半精度',
        fp32: 'FP32 单精度',
        cpu: 'CPU 稳定模式 (免显存)',
      };
      this.coordinator.showToast?.(`已保存推理设置: ${labelMap[val] || val}。请点击【重启 SoVITS 引擎】应用。`, 'info');
      this.coordinator.fetchSystemTelemetry?.();
    } catch (err) {
      if (this.disposed || revision !== this.precisionRevision) return;
      this.coordinator.showToast?.(`推理设置尚未保存：${err.message}。当前选择已保留。`, 'error');
    }
  }

  async handleRestartSovits(precision) {
    if (this.disposed || this.restarting) return;
    this.restarting = true;
    this.engineStatusRevision += 1;
    this.coordinator.renderEngineStatus?.({
      state: 'starting',
      ready: false,
      message: '正在确认设置并准备语音引擎；文字聊天可继续使用。',
    });
    if (this.dom.gBtnRestartSovits) {
      this.dom.gBtnRestartSovits.disabled = true;
      this.dom.gBtnRestartSovits.innerHTML = '<svg class="icon"><use href="#i-play"></use></svg><span>正在准备引擎…</span>';
    }
    if (this.dom.gBtnTogglePrecision) this.dom.gBtnTogglePrecision.disabled = true;

    this.engineWaitController?.abort();
    const waitController = new AbortController();
    this.engineWaitController = waitController;

    try {
      const payload = {};
      const configLoaded = this.coordinator.isConfigLoaded ? this.coordinator.isConfigLoaded() : false;
      const selected = typeof precision === 'string' ? precision : (configLoaded ? this.dom.gPrecision?.value : '');
      const revision = this.precisionRevision;
      if (selected) payload.precision = selected;
      let sent = false;
      let acknowledged = true;
      let data;
      try {
        data = await runSettingsMutation(() => {
          if (this.disposed) {
            return { skipped: true, message: '操作已在发送前取消' };
          }
          sent = true;
          return requestJson('/api/system/restart_sovits', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
          });
        });
      } catch (error) {
        if (this.disposed) return;
        if (!sent || error.name === 'AbortError' || (error.status && error.status < 500)) throw error;
        acknowledged = false;
        this.engineRestartUnconfirmed = true;
        data = { message: '重启请求结果未确认，正在核对当前引擎状态；请稍候，无需再次点击重启。' };
        this.coordinator.renderEngineStatus?.({ state: 'checking', ready: false, message: data.message });
      }
      if (this.disposed || data?.skipped) return;
      if (acknowledged) this.engineRestartUnconfirmed = false;
      if (acknowledged && this.dom.gPrecision && selected && revision === this.precisionRevision) {
        this.dom.gPrecision.value = selected;
      }
      if (acknowledged) this.coordinator.showToast?.(data.message || '引擎开始加载，请稍候。', 'info');

      let result = await waitForEngine({
        signal: waitController.signal,
        onProgress: (status) => {
          if (this.disposed) return;
          this.coordinator.renderEngineStatus?.(status);
          if (this.dom.gBtnRestartSovits) {
            this.dom.gBtnRestartSovits.innerHTML = `<svg class="icon"><use href="#i-play"></use></svg><span>${status.ready ? '引擎已就绪' : status.state === 'checking' ? '正在核对状态…' : status.state === 'loading_slow' ? '加载时间较长…' : '正在加载模型…'}</span>`;
          }
        },
      });
      if (this.disposed) return;
      if (!acknowledged && result.ready) result = { ...result, message: UNCONFIRMED_RESTART_MESSAGE };
      this.coordinator.renderEngineStatus?.(result);
      this.coordinator.showToast?.(result.message, result.ready && acknowledged ? 'success' : 'info');
    } catch (err) {
      if (this.disposed || err.name === 'AbortError') return;
      const unconfirmed = err.code === 'CONFIG_WRITE_SKIPPED' || [401, 403, 404, 429].includes(err.status);
      this.coordinator.renderEngineStatus?.({
        state: err.engineState || (unconfirmed ? 'checking' : 'failed'),
        ready: false,
        message: err.message,
      });
      const needDir = /引擎目录|选择文件夹|api_v2\.py|运行环境/i.test(err.message || '');
      this.coordinator.showToast?.(err.message, 'error', needDir ? {
        actionLabel: '选择引擎文件夹',
        onAction: () => {
          this.coordinator.goToInferenceTab?.();
          this.dom.gSovitsDirectory?.focus?.();
        },
      } : null);
    } finally {
      if (this.engineWaitController === waitController) {
        this.engineWaitController = null;
      }
      if (!this.disposed) {
        this.restarting = false;
        if (this.dom.gBtnRestartSovits) {
          this.dom.gBtnRestartSovits.disabled = false;
          this.dom.gBtnRestartSovits.innerHTML = '<svg class="icon"><use href="#i-play"></use></svg><span>重启 SoVITS 引擎</span>';
        }
        if (this.dom.gBtnTogglePrecision) this.dom.gBtnTogglePrecision.disabled = false;
        this.coordinator.fetchSystemTelemetry?.();
      }
    }
  }

  async handleTogglePrecision() {
    if (this.disposed) return;
    const badgeText = this.dom.gDashPrecisionBadge?.textContent.trim().toUpperCase();
    if (!['CPU', 'FP16', 'FP32'].includes(badgeText)) {
      await this.handleRestartSovits();
      return;
    }
    let targetPrec = 'fp16';
    if (badgeText === 'FP16') {
      targetPrec = 'fp32';
    } else if (badgeText === 'FP32') {
      targetPrec = 'cpu';
    } else {
      targetPrec = 'fp16';
    }

    await this.handleRestartSovits(targetPrec);
  }

  async handleClearCache() {
    if (this.disposed || this.cacheClearing || this.dom.gBtnClearCache?.disabled) return;
    if (typeof confirm === 'function' && !confirm('确定要清空全部 TTS 离线音频缓存吗？清空后新请求将重新合成。')) return;
    this.cacheClearing = true;
    if (this.dom.gBtnClearCache) this.dom.gBtnClearCache.disabled = true;
    try {
      clearMemAudioCache();
      if (this.coordinator.revokeAllCachedAudioUrls) {
        this.coordinator.revokeAllCachedAudioUrls();
      }
      if (typeof window !== 'undefined' && 'caches' in window) {
        await caches.delete('gal2voice-audio-v1').catch(() => {});
      }
      const data = await requestJson('/api/cache/clear', { method: 'POST' });
      if (this.disposed) return;
      this.coordinator.showToast?.(`缓存已清空：删除 ${data.deleted_files || 0} 个文件，释放 ${data.freed_mb || 0} MB`, 'success');
      this.coordinator.fetchSystemTelemetry?.();
    } catch (err) {
      if (this.disposed) return;
      this.coordinator.showToast?.(`清空缓存失败: ${err.message}`, 'error');
    } finally {
      if (!this.disposed) {
        this.cacheClearing = false;
        if (this.dom.gBtnClearCache) this.dom.gBtnClearCache.disabled = false;
      }
    }
  }
}

export function createEngineSettingsController(dom, coordinator) {
  return new EngineSettingsController(dom, coordinator);
}
